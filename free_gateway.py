import datetime
import contextlib
import http.server
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from nvidia_auth import load_key


FREE_MODELS = {
	"kimi-k3": "moonshotai/kimi-k3",
	"glm-5.3": "z-ai/glm-5.3",
	"deepseek-v4.1-flash": "deepseek-ai/deepseek-v4.1-flash",
	"nemotron-3-ultra": "nvidia/nemotron-3-ultra-550b-a55b",
}
EXTRA_MODELS = {"groq": "openai/gpt-oss-120b", "cloudflare": "@cf/openai/gpt-oss-120b"}
CLEF_MODEL = "@cf/cloudflare/clef"


class RequestBudget:
	def __init__(self, path, interval=12, daily_limit=8000):
		self.path = Path(path)
		self.interval = interval
		self.daily_limit = daily_limit
		self.lock = threading.Lock()
		self.next_request = 0
		self.data = {"day": "", "requests": 0}
		if self.path.exists():
			self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

	def reserve(self):
		with self.lock:
			with budget_lock(self.path.with_suffix(".lock")):
				if self.path.exists():
					self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
				stamp = datetime.datetime.now(datetime.timezone.utc)
				day = stamp.date().isoformat()
				if day != self.data["day"]:
					self.data = {"day": day, "requests": 0}
				if self.data["requests"] >= self.daily_limit:
					midnight = datetime.datetime.combine(stamp.date() + datetime.timedelta(days=1), datetime.time(), datetime.timezone.utc)
					return int((midnight - stamp).total_seconds()) + 1
				reserved_at = max(time.time(), self.data.get("next_at", 0))
				self.data["next_at"] = reserved_at + self.interval
				self.data["requests"] += 1
				tmp = self.path.with_suffix(".tmp")
				tmp.write_text(json.dumps(self.data), encoding="utf-8")
				tmp.replace(self.path)
			delay = reserved_at - time.time()
			if delay > 0:
				time.sleep(delay)
			return 0


@contextlib.contextmanager
def budget_lock(path):
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("a+b") as handle:
		handle.seek(0, 2)
		if handle.tell() == 0:
			handle.write(b"0")
			handle.flush()
		handle.seek(0)
		if __import__("os").name == "nt":
			import msvcrt
			msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
		else:
			import fcntl
			fcntl.flock(handle, fcntl.LOCK_EX)
		try:
			yield
		finally:
			handle.seek(0)
			if __import__("os").name == "nt":
				msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
			else:
				fcntl.flock(handle, fcntl.LOCK_UN)


class FreeGateway:
	def __init__(self, usage_path, interval=12):
		from secret_store import load
		from provider_budget import ProviderBudget
		self.key = load_key()
		if not self.key:
			raise ValueError("No NVIDIA key is available.")
		self.budget = RequestBudget(usage_path, interval=interval)
		self.credentials = {provider: load(provider) for provider in EXTRA_MODELS}
		self.providers = {"nvidia": True, **{p: bool(c.get("key") and (p != "cloudflare" or c.get("account_id"))) for p, c in self.credentials.items()}}
		self.quotas = ProviderBudget(Path(usage_path).with_name("free-provider-budgets.json"))
		self.quotas.seed_probes(Path(usage_path).parent / "endpoint-tests")
		self.metrics = {"requests": 0, "errors": 0, "cost": 0, "successes": {}}
		gateway = self
		class Handler(http.server.BaseHTTPRequestHandler):
			protocol_version = "HTTP/1.1"
			def log_message(self, *args):
				pass
			def send_json(self, code, body, retry=None):
				data = json.dumps(body).encode()
				self.send_response(code)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(data)))
				if retry:
					self.send_header("Retry-After", str(retry))
				self.end_headers()
				self.wfile.write(data)
			def do_GET(self):
				if self.path == "/v1/status":
					self.send_json(200, gateway.status())
					return
				if self.path != "/v1/models":
					self.send_json(404, {"error": "Unknown endpoint"})
					return
				self.send_json(200, {"object": "list", "data": [{"id": model, "object": "model"} for model in FREE_MODELS.values()]})
			def do_POST(self):
				if self.path not in ("/v1/chat/completions", "/v1/route"):
					self.send_json(404, {"error": "Unknown endpoint"})
					return
				length = int(self.headers.get("Content-Length", 0))
				if not 0 < length <= 16 * 1024 * 1024:
					self.send_json(413, {"error": "The request is too large"})
					return
				try:
					body = json.loads(self.rfile.read(length))
				except (ValueError, UnicodeError):
					self.send_json(400, {"error": "Invalid JSON"})
					return
				if not isinstance(body, dict):
					self.send_json(400, {"error": "A JSON object is required."})
					return
				if self.path == "/v1/route" or body.get("model") in EXTRA_MODELS.values():
					model = CLEF_MODEL if self.path == "/v1/route" else body["model"]
					provider = "cloudflare" if self.path == "/v1/route" else next(p for p, m in EXTRA_MODELS.items() if m == model)
					code, result, retry = gateway.extra_request(provider, model, body)
					self.send_json(code, result, retry)
					return
				if body.get("model") not in FREE_MODELS.values():
					self.send_json(400, {"error": "This gateway permits only configured free models"})
					return
				retry = gateway.budget.reserve()
				if retry:
					self.send_json(429, {"error": {"type": "FreeUsageLimitError", "message": "The local daily request budget is exhausted"}}, retry)
					return
				gateway.metrics["requests"] += 1
				request = urllib.request.Request(
					"https://integrate.api.nvidia.com/v1/chat/completions",
					data=json.dumps(body).encode(),
					headers={"Authorization": "Bearer " + gateway.key, "Content-Type": "application/json", "User-Agent": "ocswarm-free-workers/1.0"},
				)
				try:
					with urllib.request.urlopen(request, timeout=180) as upstream:
						self.send_response(upstream.status)
						self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
						self.send_header("Connection", "close")
						self.end_headers()
						self.close_connection = True
						if body.get("stream"):
							for line in upstream:
								self.wfile.write(line)
								self.wfile.flush()
						else:
							self.wfile.write(upstream.read())
						gateway.metrics["successes"][body["model"]] = gateway.metrics["successes"].get(body["model"], 0) + 1
				except urllib.error.HTTPError as exc:
					gateway.metrics["errors"] += 1
					data = exc.read().replace(gateway.key.encode(), b"[REDACTED]")
					self.send_response(exc.code)
					self.send_header("Content-Type", "application/json")
					self.send_header("Content-Length", str(len(data)))
					if exc.headers.get("Retry-After"):
						self.send_header("Retry-After", exc.headers["Retry-After"])
					self.end_headers()
					self.wfile.write(data)
				except (BrokenPipeError, ConnectionResetError):
					pass
				except (OSError, TimeoutError):
					gateway.metrics["errors"] += 1
					self.send_json(503, {"error": "The NVIDIA endpoint did not respond"})
		self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.server.daemon_threads = True
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

	def status(self):
		stamp = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
		count = self.budget.data["requests"] if self.budget.data["day"] == stamp else 0
		rows = {"nvidia": {"enabled": True, "ready": count < self.budget.daily_limit, "requests_today": count, "used": count, "remaining": max(0, self.budget.daily_limit - count), "limit": self.budget.daily_limit, "unit": "requests", "cost": 0, "successes": self.metrics["successes"].copy()}}
		for provider in EXTRA_MODELS:
			rows[provider] = {**self.quotas.status(provider), "enabled": self.providers[provider]}
			rows[provider]["ready"] = rows[provider]["ready"] and self.providers[provider]
		return rows

	def extra_request(self, provider, model, body):
		from provider_budget import charge
		if model not in (*EXTRA_MODELS.values(), CLEF_MODEL) or not self.providers.get(provider):
			return 400, {"error": {"type": "FreeProviderUnavailable", "message": "This free provider is not configured."}}, 0
		if (provider == "groq" and model != EXTRA_MODELS["groq"]) or (provider == "cloudflare" and model not in (EXTRA_MODELS["cloudflare"], CLEF_MODEL)):
			return 400, {"error": "The provider does not permit this model."}, 0
		body = dict(body)
		body["model"] = model
		body["stream"] = False
		body.pop("service_tier", None)
		if model == CLEF_MODEL:
			if not isinstance(body.get("state"), str) or len(body["state"]) > 16000 or not isinstance(body.get("questions"), dict):
				return 400, {"error": "A bounded routing state and questions are required."}, 0
			body = {"model": "clef", "state": body["state"], "questions": body["questions"]}
		else:
			body = {k: v for k, v in body.items() if k in {"model", "messages", "tools", "tool_choice", "max_tokens", "max_completion_tokens", "stream", "reasoning_effort"}}
			if not isinstance(body.get("messages"), list):
				return 400, {"error": "A message list is required."}, 0
			try:
				cap = 4096
				output = int(body.pop("max_tokens", body.get("max_completion_tokens", cap)))
				body["max_completion_tokens"] = min(cap, max(1, output))
			except (TypeError, ValueError):
				return 400, {"error": "The output limit must be an integer."}, 0
			body["reasoning_effort"] = "high"
		ticket, retry, reason = self.quotas.reserve(provider, charge(provider, model, body), "router" if model == CLEF_MODEL else "worker")
		if not ticket:
			kind = "InputBudgetError" if retry < 0 else "LocalRateLimitError"
			return (413 if retry < 0 else 429), {"error": {"type": kind, "message": reason}}, max(0, retry)
		credential = self.credentials[provider]
		url = "https://api.groq.com/openai/v1/chat/completions" if provider == "groq" else "https://api.cloudflare.com/client/v4/accounts/" + credential["account_id"] + "/ai/run/" + model
		request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + credential["key"], "Content-Type": "application/json", "User-Agent": "ocswarm-free-workers/1.0"})
		self.metrics["requests"] += 1
		try:
			with urllib.request.urlopen(request, timeout=20 if model == CLEF_MODEL else 180) as response:
				raw = response.read().decode("utf-8").replace(credential["key"], "[REDACTED]")
				result = json.loads(raw)
				if provider == "cloudflare":
					if result.get("success") is False:
						raise ValueError("Cloudflare rejected the request.")
					result = result.get("result", result)
				usage = result.get("usage")
				if usage:
					self.quotas.settle(ticket, charge(provider, model, body, usage))
				return response.status, result, 0
		except urllib.error.HTTPError as error:
			with error:
				self.metrics["errors"] += 1
				raw = error.read().decode("utf-8", "replace").replace(credential["key"], "[REDACTED]")
				try:
					result = json.loads(raw)
				except ValueError:
					result = {"error": {"message": "The free provider rejected the request."}}
				try:
					retry = max(60, int(error.headers.get("Retry-After", "90")))
				except (ValueError, TypeError):
					retry = 90
				if error.code in (401, 403):
					retry = 3600
				if error.code in (401, 403, 429, 502, 503, 504):
					self.quotas.cooldown(provider, retry, "Upstream HTTP " + str(error.code) + ".")
				return error.code, result, retry
		except (OSError, TimeoutError, ValueError):
			self.metrics["errors"] += 1
			self.quotas.cooldown(provider, 90, "The provider did not return a valid result.")
			return 503, {"error": {"message": "The free provider did not respond."}}, 90

	def close(self):
		self.server.shutdown()
		self.server.server_close()


def model_config(base_url):
	models = {}
	for alias, upstream in FREE_MODELS.items():
		models[alias] = {
			"modelID": upstream, "name": upstream,
			"limit": {"context": 1048576, "output": 16384},
			"capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
			"compatibility": {"reasoningField": "reasoning_content"},
			"body": {"reasoning_effort": "max"},
			"cost": {"input": 0, "output": 0},
		}
	return {"nvidia": {
		"name": "NVIDIA free endpoints", "package": "@opencode/ai/providers/openai-compatible",
		"settings": {"baseURL": base_url, "apiKey": "ocswarm-local"}, "models": models,
	}}
