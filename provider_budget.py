import datetime
import functools
import json
import math
import threading
import time
import uuid
from pathlib import Path

from free_gateway import budget_lock


@functools.lru_cache(maxsize=1)
def tokenizer():
	try:
		import tiktoken
		return tiktoken.get_encoding("o200k_harmony")
	except (ImportError, OSError, ValueError):
		return None


def input_tokens(body):
	text = json.dumps({k: body[k] for k in ("messages", "tools", "state", "questions") if k in body}, ensure_ascii=False)
	encoding = tokenizer()
	count = len(encoding.encode(text, disallowed_special=())) if encoding else len(text.encode())
	return math.ceil(count * 1.15) + 256


LIMITS = {
	"groq": {"day_units": 180000, "minute_units": 7600, "day_requests": 950, "minute_requests": 28, "unit": "tokens"},
	"cloudflare": {"day_units": 9500, "minute_units": 9500, "day_requests": 100000, "minute_requests": 250, "unit": "neurons"},
}
CF_CODING_LIMIT = 6000


def charge(provider, model, body, usage=None):
	if usage is not None:
		if provider == "groq":
			return max(0, usage.get("total_tokens", 0) - usage.get("prompt_tokens_details", {}).get("cached_tokens", 0))
		if "neurons" in usage:
			return float(usage["neurons"])
		if "clef" in model:
			return usage.get("input_tokens", 0) * 21818 / 1000000
	prompt = input_tokens(body)
	output = int(body.get("max_completion_tokens", body.get("max_tokens", 8192)))
	if provider == "groq":
		return prompt + output
	if "clef" in model:
		return prompt * 21818 / 1000000
	return (prompt * 31818 + output * 68182) / 1000000


class ProviderBudget:
	def __init__(self, path):
		self.path = Path(path)
		self.lock = threading.RLock()

	def read(self):
		return json.loads(self.path.read_text()) if self.path.exists() else {}

	def write(self, data):
		self.path.parent.mkdir(parents=True, exist_ok=True)
		tmp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
		tmp.write_text(json.dumps(data))
		tmp.replace(self.path)

	def current(self, data, provider, now):
		day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date().isoformat()
		row = data.setdefault(provider, {})
		if row.get("day") != day:
			row.clear()
			row.update(day=day, units=0, requests=0, errors=0, windows=[], until=0, reason="")
		row.setdefault("buckets", {"worker": row["units"], "router": 0})
		row["windows"] = [w for w in row["windows"] if w["at"] > now - 60]
		return row

	def retry(self, row, limits, units, now, bucket="worker"):
		if row.get("until", 0) > now:
			return math.ceil(row["until"] - now), row.get("reason", "Provider cooldown.")
		coding_full = limits["unit"] == "neurons" and bucket == "worker" and row["buckets"]["worker"] + units > CF_CODING_LIMIT
		if coding_full or row["units"] + units > limits["day_units"] or row["requests"] >= limits["day_requests"]:
			dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
			midnight = datetime.datetime.combine(dt.date() + datetime.timedelta(days=1), datetime.time(), datetime.timezone.utc)
			return math.ceil(midnight.timestamp() - now) + 1, "Coding budget reserved for Clef routing." if coding_full else "Daily free allowance reserved."
		if units > limits["minute_units"]:
			return -1, "The prompt and output exceed the free minute token limit."
		if sum(w["units"] for w in row["windows"]) + units > limits["minute_units"] or len(row["windows"]) >= limits["minute_requests"]:
			return max(1, math.ceil(min(w["at"] for w in row["windows"]) + 61 - now)), "Minute allowance reserved."
		return 0, "Ready."

	def reserve(self, provider, units, bucket="worker"):
		with self.lock, budget_lock(self.path.with_suffix(".lock")):
			data, now = self.read(), time.time()
			row = self.current(data, provider, now)
			retry, reason = self.retry(row, LIMITS[provider], units, now, bucket)
			ticket = None
			if not retry:
				ticket = {"id": uuid.uuid4().hex, "provider": provider, "day": row["day"], "units": units, "at": now, "bucket": bucket}
				row["requests"] += 1
				row["units"] += units
				row["buckets"][bucket] += units
				row["windows"].append(ticket.copy())
			self.write(data)
			return ticket, retry, reason

	def settle(self, ticket, actual):
		with self.lock, budget_lock(self.path.with_suffix(".lock")):
			data = self.read()
			row = self.current(data, ticket["provider"], time.time())
			if row["day"] == ticket["day"]:
				row["units"] = max(0, row["units"] + actual - ticket["units"])
				bucket = ticket.get("bucket", "worker")
				row["buckets"][bucket] = max(0, row["buckets"][bucket] + actual - ticket["units"])
				for window in row["windows"]:
					if window["id"] == ticket["id"]:
						window["units"] = actual
			self.write(data)

	def cooldown(self, provider, seconds, reason):
		with self.lock, budget_lock(self.path.with_suffix(".lock")):
			data = self.read()
			row = self.current(data, provider, time.time())
			row["until"] = max(row.get("until", 0), time.time() + seconds)
			row["reason"] = reason
			row["errors"] += 1
			self.write(data)

	def status(self, provider, admission=None):
		with self.lock, budget_lock(self.path.with_suffix(".lock")):
			data, now = self.read(), time.time()
			row = self.current(data, provider, now)
			limits = LIMITS[provider]
			retry, reason = self.retry(row, limits, admission if admission is not None else (5400 if provider == "groq" else 450), now)
			self.write(data)
			return {"ready": retry == 0, "retry_after": max(0, retry), "reason": reason, "requests_today": row["requests"], "used": round(row["units"], 2), "remaining": round(max(0, limits["day_units"] - row["units"]), 2), "limit": limits["day_units"], "unit": limits["unit"], "errors": row["errors"], "buckets": row["buckets"].copy(), "coding_limit": CF_CODING_LIMIT if provider == "cloudflare" else limits["day_units"], "cost": 0}

	def seed_probes(self, folder):
		with self.lock, budget_lock(self.path.with_suffix(".lock")):
			data, now = self.read(), time.time()
			day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date().isoformat()
			for provider in LIMITS:
				row = self.current(data, provider, now)
				if row.get("probe_seeded"):
					continue
				for path in Path(folder).glob("*.json"):
					try:
						test = json.loads(path.read_text())
						if not isinstance(test, dict) or not str(test.get("tested_at", "")).startswith(day):
							continue
						for result in test.get("results", []):
							if result.get("provider") != provider or result.get("status") != 200:
								continue
							body = result.get("body", {}); body = body.get("result", body)
							row["units"] += charge(provider, result["model"], {}, body.get("usage", {}))
							bucket = "router" if "clef" in result["model"] else "worker"
							row["buckets"][bucket] += charge(provider, result["model"], {}, body.get("usage", {}))
							row["requests"] += 1
					except (OSError, ValueError, KeyError, TypeError):
						continue
				row["probe_seeded"] = True
			self.write(data)
