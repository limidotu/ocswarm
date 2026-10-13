import json
import time
from free_gateway import FREE_MODELS


MODELS = {
	"nvidia": ["nvidia/kimi-k3", "nvidia/nemotron-3-ultra", "nvidia/deepseek-v4.1-flash", "nvidia/glm-5.3"],
	"groq": ["groq/gpt-oss-120b"],
	"cloudflare": ["cloudflare/gpt-oss-120b"],
}
INITIAL = {"nvidia": 6, "groq": 1, "cloudflare": 2}
MAXIMUM = {"nvidia": 12, "groq": 2, "cloudflare": 6}


class ProviderRoutes:
	def __init__(self, core, gateway):
		self.core, self.gateway = core, gateway
		self.models = [m for p, models in MODELS.items() if gateway.providers.get(p) for m in models]
		self.states = {m: {"hits": 0, "until": 0, "retry_at": "", "disabled": False} for m in self.models}
		self.limits = {p: INITIAL[p] for p in MODELS if gateway.providers.get(p)}
		self.selected = self.models[0]
		self.cursor = 0
		self.cached = {}
		self.cached_at = 0
		self.proven_responses = {}
		self.response_credit = 0

	@property
	def limit(self):
		return min(self.core.WORKER_COUNT, sum(self.limits.values()))

	def status(self):
		if time.monotonic() - self.cached_at > 1 or not self.cached:
			self.cached = self.gateway.status()
			self.cached_at = time.monotonic()
			for upstream, count in self.cached.get("nvidia", {}).get("successes", {}).items():
				alias = next((m for m in self.models if m.startswith("nvidia/") and FREE_MODELS.get(m.split("/")[-1]) == upstream), None)
				new = max(0, count - self.proven_responses.get(upstream, 0))
				self.proven_responses[upstream] = count
				if alias and not self.states[alias]["disabled"] and self.states[alias]["until"] <= time.monotonic():
					self.response_credit += new
			if self.response_credit >= 4 and "nvidia" in self.limits:
				self.limits["nvidia"] = min(MAXIMUM["nvidia"], self.limits["nvidia"] + self.response_credit // 4)
				self.response_credit %= 4
		return self.cached

	def model(self, role="worker", slots=None, exclude=(), advance=True):
		status = self.status()
		providers = list(self.limits)
		if role == "orchestrator":
			providers = ["nvidia"]
		elif role == "reviewer":
			providers = ["groq", "nvidia"]
		else:
			providers = providers[self.cursor % len(providers):] + providers[:self.cursor % len(providers)]
		active = {p: 0 for p in self.limits}
		for slot in (slots or {}).values():
			if slot.get("proc") and slot.get("role") == "worker":
				provider = slot.get("model", "").split("/")[0]
				active[provider] = active.get(provider, 0) + 1
		for provider in providers:
			if provider not in self.limits or not status.get(provider, {}).get("ready"):
				continue
			if role == "worker" and slots is not None and active[provider] >= self.limits[provider]:
				continue
			for model in MODELS[provider]:
				state = self.states.get(model, {})
				if model in exclude or state.get("disabled", True) or state.get("until", 0) > time.monotonic():
					continue
				self.selected = model
				if role == "worker" and slots is not None and advance:
					self.cursor = list(self.limits).index(provider) + 1
				return model
		return None

	def reject(self, model, kind, log):
		if model not in self.states or kind == "context":
			return False
		state = self.states[model]
		stamp = time.monotonic()
		if state["until"] > stamp or state["disabled"]:
			return False
		state["hits"] += 1
		provider = model.split("/")[0]
		error = self.core.log_error(log)
		local = "LocalRateLimitError" in json.dumps(error)
		if not local:
			self.limits[provider] = max(1, self.limits[provider] // 2)
		delay = max(self.core.retry_after(log), min(3600, 90 * 2 ** (state["hits"] - 1)))
		state.update(until=stamp + delay, retry_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delay)), disabled=kind == "free")
		self.cached_at = 0
		return True

	def success(self, model):
		if model not in self.states:
			return
		state = self.states[model]
		if state["until"] > time.monotonic() or state["disabled"]:
			return
		state["hits"] = 0
		provider = model.split("/")[0]
		self.limits[provider] = min(MAXIMUM[provider], self.limits[provider] + 1)

	def exhausted(self):
		return all(state["disabled"] for state in self.states.values())

	def message(self):
		return "Free providers wait for a quota reset or cooldown. Local evidence and candidates remain saved."

	def snapshot(self, slots):
		rows = self.status()
		result = {}
		for provider, limit in self.limits.items():
			result[provider] = {**rows.get(provider, {}), "live_limit": limit, "active": sum(bool(s.get("proc")) and s.get("role") == "worker" and s.get("model", "").startswith(provider + "/") for s in slots.values()), "models": {m: s for m, s in self.states.items() if m.startswith(provider + "/")}}
			available = [s for s in result[provider]["models"].values() if not s["disabled"]]
			if not any(s["until"] <= time.monotonic() for s in available):
				if result[provider].get("ready"):
					result[provider]["reason"] = "Model cooldown."
				result[provider]["ready"] = False
				result[provider]["retry_after"] = max(result[provider].get("retry_after", 0), int(min((s["until"] for s in available), default=time.monotonic() + 3600) - time.monotonic()) + 1)
		return result
