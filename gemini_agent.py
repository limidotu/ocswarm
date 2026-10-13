import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


MODEL = "gemini/gemini-3.8-flash-high"
PROFILE = Path.home() / ".local/share/ocswarm/agy-home"
CONFIG = Path(__file__).parent / "runtime/gemini.json"


def settings_path():
	return PROFILE / ".gemini/antigravity-cli/settings.json"


def executable():
	return shutil.which("agy") or str(Path.home() / "AppData/Local/agy/bin/agy.exe")


def environment():
	env = os.environ.copy()
	env["USERPROFILE"] = str(PROFILE)
	for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "NVIDIA_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GEMINI_CLI_HOME"):
		env.pop(key, None)
	return env


def read_config():
	try:
		return json.loads(CONFIG.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}


def available(role):
	config = read_config()
	if not config.get("enabled") or role not in config.get("roles", ["reviewer"]):
		return ""
	if config.get("until", 0) > time.time() or not Path(executable()).is_file():
		return ""
	try:
		settings = json.loads(settings_path().read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return ""
	if not safe_settings(settings):
		return ""
	return MODEL


def safe_settings(settings):
	denied = set(settings.get("permissions", {}).get("deny", []))
	return settings.get("useG1Credits", False) is False and not settings.get("modelProvider") and {"write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "read_url(*)", "execute_url(*)"}.issubset(denied)


def record(core, log, failed=False):
	config = read_config()
	if failed:
		config["errors"] = config.get("errors", 0) + 1
		config["until"] = time.time() + 1800
		# The supported CLI reports the actual shared quota reset times.
		try:
			result = subprocess.run([executable(), "-p", "/usage", "--output-format", "json", "--print-timeout", "30s"], env=environment(), capture_output=True, text=True, timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
			groups = json.loads(result.stdout).get("command", {}).get("data", {}).get("groups", [])
			resets = []
			for group in groups:
				if group.get("name") != "Gemini Models":
					continue
				for bucket in group.get("buckets", []):
					if bucket.get("remaining_fraction", 1) <= 0:
						resets.append(datetime.datetime.fromisoformat(bucket["reset_time"].replace("Z", "+00:00")).timestamp())
			if resets:
				config["until"] = max(time.time() + 60, max(resets) + 30)
		except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
			pass
	else:
		config["sessions"] = config.get("sessions", 0) + 1
		config["until"] = 0
		if log and Path(log).is_file():
			for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
				try:
					event = json.loads(line)
				except ValueError:
					continue
				if event.get("type") == "step_finish" and event.get("provider") == "gemini":
					for key, value in event.get("usage", {}).items():
						if isinstance(value, (int, float)):
							config[key] = config.get(key, 0) + value
	config["cost"] = 0
	core.write_json(CONFIG, config)


def normalized(event):
	kind = event.get("event")
	if kind == "init":
		return [{"type": "step_start", "provider": "gemini", "model": event.get("init", {}).get("model")}]
	if kind == "step_update":
		step = event.get("step_update", {})
		if step.get("step_type") == "agent_response" and step.get("text_delta"):
			return [{"type": "text", "part": {"text": step["text_delta"]}}]
		if step.get("step_type") == "tool":
			tool = step.get("tool_info", {})
			parameters = tool.get("parameters", {})
			title = next((str(parameters[k]) for k in ("AbsolutePath", "SearchPath", "DirectoryPath", "CommandLine") if k in parameters), "")
			return [{"type": "tool_use", "part": {"tool": step.get("tool_name", "tool"), "state": {"status": "completed" if step.get("state") == "DONE" else "running", "title": title, "input": parameters, "output": tool.get("output", "")}}}]
	if kind == "result":
		result = event.get("result", {})
		if result.get("status") != "SUCCESS" or not result.get("response", "").strip():
			message = str(result.get("error") or "Gemini returned no response.")
			if result.get("denied_actions"):
				message = "Gemini could not read the requested evidence."
			status = 429 if re.search(r"quota|rate.limit|resource.exhausted|too many", message, re.I) else 503
			return [{"type": "error", "error": {"status": status, "message": message}}]
		return [{"type": "text", "part": {"text": result["response"]}}, {"type": "step_finish", "provider": "gemini", "usage": result.get("usage", {}), "cost": 0}]
	return []


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--prompt-file", required=True)
	parser.add_argument("--cwd", required=True)
	args = parser.parse_args()
	settings = json.loads(settings_path().read_text(encoding="utf-8"))
	if not safe_settings(settings):
		raise ValueError("Gemini requires included quota with credit overages disabled.")
	prompt = Path(args.prompt_file).read_text(encoding="utf-8")
	command = [executable(), "-p", prompt, "--model", MODEL.split("/", 1)[1], "--effort", "high", "--agent", "ocswarm-evidence", "--output-format", "stream-json", "--disable-slash-commands", "--print-timeout", "90m"]
	proc = subprocess.Popen(command, cwd=args.cwd, env=environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
	completed = False
	failed = False
	for line in proc.stdout:
		try:
			event = json.loads(line)
		except ValueError:
			continue
		for row in normalized(event):
			failed = failed or row["type"] == "error"
			print(json.dumps(row), flush=True)
		completed = completed or event.get("event") == "result"
	code = proc.wait()
	if not completed:
		print(json.dumps({"type": "error", "error": {"status": 503, "message": "Gemini ended without a result."}}), flush=True)
	return code or (1 if failed or not completed else 0)


if __name__ == "__main__":
	raise SystemExit(main())
