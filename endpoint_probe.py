import argparse
import concurrent.futures
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from nvidia_auth import load_key
from secret_store import load


ROOT = Path(__file__).parent
RESULTS = ROOT / "runtime/endpoint-tests"


def request(provider, model, prompt="Reply PONG only.", max_tokens=512, overrides=None):
	credentials = load(provider) if provider != "nvidia" else {"key": load_key()}
	if not credentials.get("key"):
		return {"provider": provider, "model": model, "status": "missing_key"}
	if provider == "nvidia":
		url = "https://integrate.api.nvidia.com/v1/chat/completions"
	elif provider == "groq":
		url = "https://api.groq.com/openai/v1/chat/completions"
	elif provider == "cloudflare":
		url = "https://api.cloudflare.com/client/v4/accounts/" + credentials["account_id"] + "/ai/run/" + model
	elif provider == "mistral":
		url = "https://api.mistral.ai/v1/chat/completions"
	else:
		raise ValueError("Unsupported provider.")
	body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "stream": False}
	if "clef" in model:
		body = {"model": model.split("/")[-1], "state": prompt, "questions": {"route": {"type": "choice", "instructions": "Which next action fits the state?", "criteria": {"research": "Missing original method evidence. Request IL before coding.", "code": "Original evidence is available and the bounded source task is ready.", "wait": "The selected provider returned HTTP 429. Wait for its quota cooldown.", "review": "A candidate is complete and needs independent evidence review."}}}}
	elif provider == "groq" and "gpt-oss" in model:
		body["reasoning_effort"] = "high"
		body["max_completion_tokens"] = body.pop("max_tokens")
	if overrides:
		body.update(overrides)
	req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + credentials["key"], "Content-Type": "application/json", "User-Agent": "ocswarm-endpoint-tests/1.0"})
	started = time.monotonic()
	try:
		with urllib.request.urlopen(req, timeout=120) as response:
			data = json.load(response)
			headers = {k: v for k, v in response.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))}
			status = response.status
	except urllib.error.HTTPError as error:
		with error:
			status = error.code
			raw = error.read().decode("utf-8", "replace").replace(credentials["key"], "[REDACTED]")
			try:
				data = json.loads(raw)
			except ValueError:
				data = {"error": raw[:1200]}
			headers = {k: v for k, v in error.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))}
	except (OSError, TimeoutError):
		status, data, headers = "timeout", {}, {}
	data = json.loads(json.dumps(data).replace(credentials["key"], "[REDACTED]"))
	return {"provider": provider, "model": model, "status": status, "seconds": round(time.monotonic() - started, 3), "headers": headers, "body": data}


def run(provider, model, count, prompt, max_tokens, overrides=None):
	start = time.monotonic()
	with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
		rows = list(pool.map(lambda _: request(provider, model, prompt, max_tokens, overrides), range(count)))
	result = {"tested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "simultaneous_requests": count, "wall_seconds": round(time.monotonic() - start, 3), "results": rows}
	RESULTS.mkdir(parents=True, exist_ok=True)
	path = RESULTS / (provider + "-" + model.split("/")[-1] + "-" + str(time.time_ns()) + ".json")
	path.write_text(json.dumps(result, indent=2))
	print(json.dumps({"file": str(path), "statuses": [r["status"] for r in rows], "seconds": result["wall_seconds"]}))
	return result


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("provider", choices=["nvidia", "groq", "cloudflare", "mistral"])
	parser.add_argument("model")
	parser.add_argument("--count", type=int, choices=range(1, 17), default=1)
	parser.add_argument("--prompt-file")
	parser.add_argument("--max-tokens", type=int, default=512)
	args = parser.parse_args()
	prompt = Path(args.prompt_file).read_text() if args.prompt_file else "Reply PONG only."
	run(args.provider, args.model, args.count, prompt, args.max_tokens)


if __name__ == "__main__":
	main()
