# Overnight swarm for OpenCode. Edit the values in this block to change a run.
WORKER_MODEL = "nvidia/kimi-k3"
WORKER_MODELS = [
	WORKER_MODEL,
	"nvidia/nemotron-3-ultra",
	"nvidia/deepseek-v4.1-flash",
	"nvidia/glm-5.3",
]
MODEL = WORKER_MODEL
ORCH_MODEL = "deepseek/deepseek-v4-flash"
ORCH_EFFORT = "max"
ORCH_HARNESS = "codebuff"
WORKER_COUNT = 19
WAKE_SECONDS = 30
INITIAL_LIVE = 2
MAX_LIVE = WORKER_COUNT
CAP_HOURS = 8
STALL_MINUTES = 45
POLL_SECONDS = 1
SERVE_HOST = "127.0.0.1"
MAX_REPAIR_ROUNDS = 3
MAX_ITEM_TRIES = 2
MAX_RATE_HITS = 8
RATE_BACKOFF_SECONDS = 90
MAX_RATE_BACKOFF_SECONDS = 3600
ACCEPT_TIMEOUT_SECONDS = 2400
AGENT_BASH = False
ALLOW_WEB = False
SKIP_DIRS = {".ocswarm", "target", ".git"}
SNAP_MAX_BYTES = 8 * 1024 * 1024
UI_PORT = 8124

import argparse
import ctypes
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
CONFIG_DIR = ROOT / "config"
RUNTIME = ROOT / "runtime"
OPENCODE = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
SERVER_PASSWORD = ""
MARKER = "OC_SWARM_JSON"
ACTIVE_PROTECTS = []
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACH_FLAGS = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000 if os.name == "nt" else 0

SLOTS = [("orchestrator", "orchestrator")] + [(f"worker-{i}", "worker") for i in range(1, WORKER_COUNT + 1)]

ANGLES = {
	"explorer-a": "Map the files, the entry points, and the tests that already cover the task. Name the files a change must touch.",
	"explorer-b": "Find the risks, the invariants, and the files that must not change together. Quote the evidence path for each claim.",
	"explorer-c": "Find how to prove the task is done. Name the existing test pattern and the command that shows a failure.",
}

PROTOCOL = """You are one slot in a 20-slot swarm. You share one tree with the other slots. The supervisor is not a person. Do not ask a question. Do not wait. Do not run git.

End your reply with a line that is exactly OC_SWARM_JSON and then one JSON object. status is done or blocked. summary is one line of at most 160 characters. If you cannot finish, status is blocked and the summary says why.

Touch only the paths in owns, plus your scratch directory. The supervisor restores every other path after you exit. A restored path fails your item.
"""


def die(text):
	print("ocswarm: " + text, file=sys.stderr)
	raise SystemExit(text)


def now():
	return time.strftime("%Y-%m-%dT%H:%M:%S")


def atomic_write(path, text):
	path.parent.mkdir(parents=True, exist_ok=True)
	import uuid
	tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
	try:
		tmp.write_text(text, encoding="utf-8")
		for attempt in range(8):
			try:
				tmp.replace(path)
				break
			except PermissionError:
				if attempt == 7:
					raise
				time.sleep(0.02 * 2 ** attempt)
	finally:
		if tmp.exists():
			tmp.unlink()


def write_json(path, obj):
	atomic_write(path, json.dumps(obj, indent=2))


def append_jsonl(path, obj):
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("a", encoding="utf-8") as handle:
		handle.write(json.dumps(obj) + "\n")


def clean_rel(raw):
	text = str(raw).replace("\\", "/").strip()
	if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
		raise ValueError("absolute path")
	parts = []
	for part in text.split("/"):
		if part in ("", "."):
			continue
		if part == "..":
			raise ValueError("parent path")
		parts.append(part)
	if not parts:
		raise ValueError("empty path")
	return "/".join(parts)


def perm_rules(kind):
	rules = [
		("*", "*", "deny"),
		("read", "*", "allow"),
		("glob", "*", "allow"),
		("grep", "*", "allow"),
		("list", "*", "allow"),
		("external_directory", "*", "allow"),
		("skill", "*", "allow"),
	]
	if kind in ("worker", "architect", "challenger"):
		rules.append(("edit", "*", "allow"))
	if AGENT_BASH and kind in ("worker", "architect", "challenger"):
		rules.append(("shell", "*", "allow"))
		rules.append(("shell", "git push*", "deny"))
		rules.append(("shell", "git commit*", "deny"))
		rules.append(("shell", "git reset*", "deny"))
		rules.append(("shell", "git clean*", "deny"))
	if ALLOW_WEB:
		rules.append(("webfetch", "*", "allow"))
		rules.append(("websearch", "*", "allow"))
	return rules


def yaml_perm(rules):
	lines = ["permissions:"]
	for action, resource, effect in rules:
		lines.append("  - action: " + json.dumps(action))
		lines.append("    resource: " + json.dumps(resource))
		lines.append("    effect: " + effect)
	return lines


BODIES = {}
BODIES["sentinel"] = PROTOCOL + """
You are the sentinel. Read the task in the user message. Return JSON with keys status, summary, goal, constraints, acceptance, integrity.
acceptance is a list of objects with keys name and command. If the user message already lists acceptance commands, copy those commands exactly and do not add any.
integrity is development. Do not edit a product file. Do not invent a command that only prints success.
"""
BODIES["orchestrator"] = PROTOCOL + """
You are the orchestrator. You do not edit product files. You split one task into milestones and items that can run at the same time.

Return JSON with keys status, summary, and plan. plan is an object with key milestones. Each milestone has id, title, and items.
Each item has id, role, title, brief, owns, depends_on, verify.
role is architect, worker, or challenger.
id matches [A-Za-z0-9._-] and is at most 64 characters.
owns is a list of relative file paths. Two items may share a path only when one depends_on the other, directly or through a chain.
The first milestone starts with one architect item. Workers that need the contract depend_on it.
owns entries are relative product paths. Do not put a path under .ocswarm.
Use as many worker items as there are disjoint files. Do not invent busywork. Do not assign the same file to two unordered items.
A challenger item owns only a new test file. Put at least one challenger item in each milestone that has a worker.
verify is a command string or an empty string. Prefer the acceptance command the brief already names.
On a repair turn the user message says repair. Return JSON with keys status, summary, and add. add is a list of new items for the open milestone. Do not repeat an id.
"""
BODIES["explorer"] = PROTOCOL + """
You are an explorer. You are read-only. Do not edit a file. Study the tree and the evidence paths in the user message.
Return JSON with keys status, summary, and findings. findings is a list of short strings. Each string names a path and one fact.
"""
BODIES["architect"] = PROTOCOL + """
You are the architect. Write the contract for this item and nothing else. The contract names the types, the files, and the test that proves the rule.
Stay inside owns. A done status is rejected when the owned file bytes are the same as when you started. Return JSON with keys status, summary, files, and notes. files lists the relative paths you changed. notes is a list of objects with keys to and text.
"""
BODIES["worker"] = PROTOCOL + """
You are a worker. Implement only the item in the user message. Stay inside owns. Match the style of the surrounding code.
Do not redo work the brief marks DONE. Do not regenerate a generated table by hand. Do not run a shell. A done status is rejected when the owned file bytes are the same as when you started.
Return JSON with keys status, summary, files, and notes. files lists the relative paths you changed. notes is a list of objects with keys to and text.
If the item needs a path you do not own, return status blocked and name the path.
"""
BODIES["critic"] = PROTOCOL + """
You are a critic. You are read-only. Review the item named in the user message. Fail the work when a test is faked, a stub pretends to pass, or a file outside the owned paths changed.
Return JSON with keys status, verdict, summary, and defects. verdict is pass or fail. status done means the review finished. defects is a list of short strings.
"""
BODIES["challenger"] = PROTOCOL + """
You are the challenger. Write one adversarial test inside owns. The test must fail if the new rule is removed. Match the existing test style.
Do not run a shell. A done status is rejected when the owned file bytes are the same as when you started. Return JSON with keys status, summary, files, and notes.
"""
BODIES["tester"] = PROTOCOL + """
You are the tester. You are read-only. Read the test log path in the user message. Explain the first real failure and name the file that should change.
Return JSON with keys status, summary, and failures. failures is a list of short strings. Do not claim a pass when the log shows a non-zero exit.
"""
BODIES["auditor"] = PROTOCOL + """
You are the auditor. You are read-only. Read the evidence log. verdict is pass only when every acceptance command exited 0 and the critics did not fail.
Return JSON with keys status, verdict, summary, and fake_evidence. fake_evidence is true when a test is stubbed, skipped, or disconnected from the rule.
"""
BODIES["success"] = PROTOCOL + """
You are the success auditor. You are read-only. Read the board facts in the user message. Say what is true now, which checks passed, and what stays open.
Return JSON with keys status, verdict, summary, and morning. morning is a short report of at most 40 lines. verdict is pass only when the evidence says every acceptance command exited 0.
"""

KIND_STEPS = {
	"sentinel": 12,
	"orchestrator": 24,
	"explorer": 20,
	"architect": 24,
	"worker": 30,
	"critic": 16,
	"challenger": 24,
	"tester": 12,
	"auditor": 12,
	"success": 12,
}


def install_agents():
	folder = CONFIG_DIR / "agents"
	folder.mkdir(parents=True, exist_ok=True)
	for old in folder.glob("*.md"):
		old.unlink()
	for name, kind in SLOTS:
		if kind != "worker":
			continue
		body = BODIES[kind]
		desc = name + " slot in the ocswarm, role " + kind
		lines = ["---", "description: " + json.dumps(desc), "mode: primary", "steps: " + str(KIND_STEPS[kind]), "permissions:"]
		lines.extend(yaml_perm(perm_rules(kind))[1:])
		lines.append("---")
		lines.append(body.strip() + "\n")
		atomic_write(folder / (name + ".md"), "\n".join(lines))
	return folder


def find_opencode():
	if OPENCODE.is_file():
		return str(OPENCODE)
	found = shutil.which("opencode")
	if not found:
		die("opencode is not on PATH")
	return found


def opencode_env():
	env = os.environ.copy()
	env["OPENCODE_CONFIG_DIR"] = str(CONFIG_DIR)
	env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
	env.pop("OPENCODE_SERVER_PASSWORD", None)
	if SERVER_PASSWORD:
		env["OPENCODE_SERVER_PASSWORD"] = SERVER_PASSWORD
		env["OPENCODE_SERVER_USERNAME"] = "opencode"
	return env


def free_port():
	sock = socket.socket()
	sock.bind((SERVE_HOST, 0))
	port = sock.getsockname()[1]
	sock.close()
	return port


def read_server_password(path, timeout, previous=""):
	deadline = time.time() + timeout
	while time.time() < deadline:
		found = ""
		if path.is_file():
			for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
				if line.startswith("server password "):
					found = line[len("server password "):].strip()
		if found and found != previous:
			return found
		time.sleep(0.2)
	die("opencode serve did not print a password")
	deadline = time.time() + timeout
	while time.time() < deadline:
		if path.is_file():
			for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
				if line.startswith("server password "):
					return line[len("server password "):].strip()
		time.sleep(0.2)
	die("opencode serve did not print a password")


def wait_port(port, timeout):
	deadline = time.time() + timeout
	while time.time() < deadline:
		try:
			sock = socket.create_connection((SERVE_HOST, port), 1)
			sock.close()
			return
		except OSError:
			time.sleep(0.2)
	die("opencode serve did not open port " + str(port))


def kill_tree(pid):
	if not pid:
		return
	if os.name == "nt":
		subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	else:
		try:
			os.kill(pid, 15)
		except OSError:
			return


def pid_alive(pid):
	if not pid:
		return False
	if os.name == "nt":
		import ctypes
		from ctypes import wintypes
		kernel = ctypes.WinDLL("kernel32", use_last_error=True)
		kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
		kernel.OpenProcess.restype = wintypes.HANDLE
		kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
		kernel.WaitForSingleObject.restype = wintypes.DWORD
		kernel.CloseHandle.argtypes = [wintypes.HANDLE]
		handle = kernel.OpenProcess(0x100000, False, int(pid))
		if not handle:
			return ctypes.get_last_error() == 5
		try:
			return kernel.WaitForSingleObject(handle, 0) == 258
		finally:
			kernel.CloseHandle(handle)
	try:
		os.kill(int(pid), 0)
	except OSError:
		return False
	except SystemError:
		return False
	return True


def iter_files(root):
	for dirpath, dirs, files in os.walk(root):
		dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
		for name in files:
			yield Path(dirpath) / name


def files_same(left, right):
	if not left.is_file() or not right.is_file():
		return False
	ls = left.stat()
	rs = right.stat()
	if ls.st_size != rs.st_size:
		return False
	with left.open("rb") as fa, right.open("rb") as fb:
		while True:
			ca = fa.read(1024 * 1024)
			cb = fb.read(1024 * 1024)
			if ca != cb:
				return False
			if not ca:
				return True


def take_snap(root, dest):
	if dest.exists():
		shutil.rmtree(dest)
	dest.mkdir(parents=True)
	index = []
	for src in iter_files(root):
		rel = src.relative_to(root).as_posix()
		index.append(rel)
		try:
			size = src.stat().st_size
		except OSError:
			continue
		if size > SNAP_MAX_BYTES:
			continue
		dst = dest / rel
		dst.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, dst)
	write_json(dest / "_index.json", index)
	return dest


def allowed(rel, allow):
	norm = rel.replace("\\", "/")
	for own in allow:
		if norm == own or norm.startswith(own.rstrip("/") + "/"):
			return True
	return False


def restore_strays(root, dest, allow):
	index = json.loads((dest / "_index.json").read_text(encoding="utf-8"))
	problems = []
	known = set(index)
	for rel in index:
		if allowed(rel, allow):
			continue
		src = dest / rel
		cur = root / rel
		if not src.is_file() or files_same(cur, src):
			continue
		cur.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, cur)
		problems.append(rel)
	for cur in list(iter_files(root)):
		rel = cur.relative_to(root).as_posix()
		if rel in known or allowed(rel, allow):
			continue
		cur.unlink()
		problems.append(rel)
	return problems


def owned_changed(root, dest, owns):
	index = set(json.loads((dest / "_index.json").read_text(encoding="utf-8")))
	for own in owns:
		prefix = own.rstrip("/")
		for cur in iter_files(root):
			rel = cur.relative_to(root).as_posix()
			if rel != prefix and not rel.startswith(prefix + "/"):
				continue
			src = dest / rel
			if rel not in index:
				return True
			if not src.is_file():
				continue
			if not files_same(cur, src):
				return True
		if prefix not in index and not any(rel.startswith(prefix + "/") for rel in index):
			# A new file under this own counts as a change when it exists.
			if (root / prefix).exists():
				return True
	return False


def copy_protect(src, dest):
	if dest.exists():
		shutil.rmtree(dest)
	dest.parent.mkdir(parents=True, exist_ok=True)
	if src.is_file():
		dest.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, dest / src.name)
		return
	shutil.copytree(src, dest, ignore=shutil.ignore_patterns("target", ".ocswarm", ".git"))


def protect_differs(src, dest):
	if src.is_file():
		copy = dest / src.name
		return not files_same(copy, src)
	for dirpath, dirs, files in os.walk(src):
		dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
		for name in files:
			live = Path(dirpath) / name
			rel = live.relative_to(src)
			copy = dest / rel
			if (not copy.is_file()) or copy.stat().st_size != live.stat().st_size or copy.stat().st_mtime_ns != live.stat().st_mtime_ns:
				# Size or mtime moved. Confirm bytes so a touch does not hide a real edit, and a same-size edit is caught below.
				if not files_same(copy, live):
					return True
	return False


def restore_protect(src, dest):
	if src.is_file():
		shutil.copy2(dest / src.name, src)
		return
	for dirpath, dirs, files in os.walk(dest):
		dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
		for name in files:
			copy = Path(dirpath) / name
			rel = copy.relative_to(dest)
			live = src / rel
			live.parent.mkdir(parents=True, exist_ok=True)
			if not files_same(live, copy):
				shutil.copy2(copy, live)


def loosen_json(text):
	start = text.find("{")
	end = text.rfind("}")
	if start < 0 or end <= start:
		return None
	blob = text[start:end + 1]
	try:
		obj = json.loads(blob)
	except json.JSONDecodeError:
		fixed = re.sub(r'([\{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', blob)
		fixed = re.sub(r':\s*([A-Za-z_][A-Za-z0-9_]*)\s*([,\}])', r': "\1"\2', fixed)
		try:
			obj = json.loads(fixed)
		except json.JSONDecodeError:
			return None
	if isinstance(obj, dict) and "status" in obj:
		return obj
	return None


def parse_marker(blob):
	idx = blob.rfind(MARKER)
	if idx < 0:
		return None
	tail = blob[idx + len(MARKER):].strip()
	if tail.startswith("```"):
		tail = tail.split("\n", 1)[-1] if "\n" in tail else ""
		fence = tail.rfind("```")
		if fence >= 0:
			tail = tail[:fence]
	try:
		obj, _end = json.JSONDecoder().raw_decode(tail.lstrip())
	except json.JSONDecodeError:
		return loosen_json(tail)
	if isinstance(obj, dict) and "status" in obj:
		return obj
	return loosen_json(tail)


def extract(path):
	if not path.is_file():
		return None
	size = path.stat().st_size
	with path.open("rb") as handle:
		if size > 4_000_000:
			handle.seek(size - 4_000_000)
		text = handle.read().decode("utf-8", "replace")
	found = []
	for line in text.splitlines():
		try:
			obj = json.loads(line)
		except json.JSONDecodeError:
			if MARKER in line:
				found.append(line)
			continue
		if not isinstance(obj, dict):
			continue
		part = obj.get("part")
		if obj.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str):
			found.append(part["text"])
		elif isinstance(obj.get("text"), str):
			found.append(obj["text"])
		walk_strings(obj, found)
	for blob in reversed(found):
		got = parse_marker(blob)
		if got:
			return got
	return None


def walk_strings(obj, found):
	if isinstance(obj, dict):
		for value in obj.values():
			walk_strings(value, found)
	elif isinstance(obj, list):
		for value in obj:
			walk_strings(value, found)
	elif isinstance(obj, str) and MARKER in obj:
		found.append(obj)


def activity_from_line(line):
	text = line.decode("utf-8", "replace").strip() if isinstance(line, bytes) else line.strip()
	if not text:
		return ""
	try:
		obj = json.loads(text)
	except json.JSONDecodeError:
		return text[:160]
	if not isinstance(obj, dict):
		return text[:160]
	kind = str(obj.get("type") or "")
	part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
	if kind == "tool_use":
		tool = str(part.get("tool") or "tool")
		state = part.get("state") if isinstance(part.get("state"), dict) else {}
		title = str(state.get("title") or state.get("status") or "")
		return (tool + " " + title).strip()[:160]
	if kind == "text":
		return str(part.get("text") or "")[:160]
	if kind in ("step_start", "step_finish"):
		return kind
	if obj.get("error"):
		return str(obj.get("error"))[:160]
	return kind[:160]


def log_error(path):
	if not path or not Path(path).is_file():
		return {}
	file = Path(path)
	size = file.stat().st_size
	with file.open("rb") as handle:
		if size > 200_000:
			handle.seek(size - 200_000)
		text = handle.read().decode("utf-8", "replace")
	for line in reversed(text.splitlines()):
		try:
			obj = json.loads(line)
		except json.JSONDecodeError:
			if re.match(r"^\s*(error\b|fatal\b|HTTP\s+[45]\d\d\b)", line, re.IGNORECASE):
				return {"message": line}
			continue
		if isinstance(obj, dict) and obj.get("type") == "error":
			error = obj.get("error")
			return error if isinstance(error, dict) else {"message": str(error or "")}
	return {}


def log_kind(path):
	error = log_error(path)
	text = json.dumps(error).lower()
	if "inputbudgeterror" in text:
		return "context"
	if error.get("status") in (401, 403):
		return "free"
	if error.get("status") in (429, 502, 503, 504) or any(word in text for word in (
		"rate limit", "too many requests", "freeusagelimiterror", "free usage exceeded", "overloaded",
	)):
		return "rate"
	if "free tier" in text or "freetier" in text:
		return "free"
	return ""


def rate_limited(path):
	return log_kind(path) == "rate"


def retry_after(path):
	error = log_error(path)
	response = error.get("response")
	response = response if isinstance(response, dict) else {}
	headers = response.get("headers") or error.get("headers") or {}
	if not isinstance(headers, dict):
		return 0
	value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
	if value is None:
		return 0
	try:
		seconds = float(value)
		return seconds if 0 <= seconds < float("inf") else 0
	except (TypeError, ValueError):
		try:
			return max(0, parsedate_to_datetime(str(value)).timestamp() - time.time())
		except (ValueError, TypeError, OverflowError):
			return 0


class WorkerRoutes:
	def __init__(self, models=None):
		self.models = list(dict.fromkeys(WORKER_MODELS if models is None else models))
		if not self.models:
			raise ValueError("no worker models configured")
		self.states = {model: {"hits": 0, "until": 0, "retry_at": "", "disabled": False} for model in self.models}
		self.selected = self.models[0]
		self.limit = min(INITIAL_LIVE, MAX_LIVE)

	def model(self):
		stamp = time.monotonic()
		for model in self.models:
			state = self.states[model]
			if state["disabled"] or state["until"] > stamp:
				continue
			if model != self.selected:
				self.selected = model
			return model
		return None

	def reject(self, model, kind, log):
		state = self.states[model]
		stamp = time.monotonic()
		# In-flight workers share one rejection window for this model.
		if state["disabled"] or state["until"] > stamp:
			return False
		state["hits"] += 1
		self.limit = max(1, self.limit // 2)
		state["disabled"] = kind == "free" or state["hits"] >= MAX_RATE_HITS
		delay = max(retry_after(log), min(MAX_RATE_BACKOFF_SECONDS, RATE_BACKOFF_SECONDS * 2 ** (state["hits"] - 1)))
		state["until"] = stamp + delay
		state["retry_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delay))
		return True

	def success(self, model):
		state = self.states[model]
		if state["disabled"] or state["until"] > time.monotonic():
			return
		state["hits"] = 0
		if model == self.selected:
			self.limit = min(MAX_LIVE, self.limit + 1)

	def exhausted(self):
		return all(state["disabled"] for state in self.states.values())

	def message(self):
		if self.exhausted():
			return "all configured free models rejected requests; run ocswarm check before restarting"
		state = min((state for state in self.states.values() if not state["disabled"]), key=lambda state: state["until"])
		return "free models cooling down; next probe at " + state["retry_at"]


def check_workers():
	from free_swarm import check
	return check(sys.modules[__name__])


def check_workers_legacy():
	exe = find_opencode()
	root = RUNTIME / "rate-probe"
	root.mkdir(parents=True, exist_ok=True)
	prompt = 'Do not use tools. Reply with OC_SWARM_JSON followed by {"status":"done","summary":"ping"}.'
	for model in WORKER_MODELS:
		log = root / (model.split("/")[-1] + ".log")
		with log.open("wb") as handle:
			proc = subprocess.Popen(
				[exe, "run", "--standalone", "--model", model, "--agent", "build", "--auto", "--format", "json", prompt],
				cwd=str(root), env=opencode_env(), stdout=handle, stderr=subprocess.STDOUT,
				stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
			)
			try:
				code = proc.wait(timeout=60)
			except subprocess.TimeoutExpired:
				kill_tree(proc.pid)
				proc.wait(timeout=10)
				print(model + ": timed out")
				continue
		got = extract(log)
		if code == 0 and isinstance(got, dict) and got.get("status") == "done" and got.get("summary") == "ping":
			print(model + ": ready")
			return 0
		kind = log_kind(log)
		print(model + ": " + ("rate limited" if kind == "rate" else "free access rejected" if kind == "free" else "probe failed"))
	return 1


def deny_command(cmd):
	low = cmd.lower()
	banned = ("git push", "git reset", "git clean", "git rebase", "shutdown", "format ", "remove-item", "rm -rf", "del /s", "diskpart", "cipher ", "reg delete", "mkfs")
	return any(item in low for item in banned)


def run_accept(cmd, cwd, log_path):
	if deny_command(cmd):
		log_path.write_text("rejected command\n" + cmd + "\n", encoding="utf-8")
		return 2
	bash = Path(r"C:\Program Files\Git\bin\bash.exe")
	argv = [str(bash), "-c", cmd] if bash.is_file() else ["cmd", "/c", cmd]
	with log_path.open("wb") as handle:
		try:
			proc = subprocess.run(argv, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=ACCEPT_TIMEOUT_SECONDS)
		except subprocess.TimeoutExpired:
			handle.write(b"\nTIMEOUT\n")
			return 124
	return proc.returncode


def overlaps(a, b):
	for left in a:
		for right in b:
			if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
				return True
	return False


def reaches(deps, src, dst):
	seen = set()
	stack = list(deps.get(src, ()))
	while stack:
		cur = stack.pop()
		if cur == dst:
			return True
		if cur in seen:
			continue
		seen.add(cur)
		stack.extend(deps.get(cur, ()))
	return False


def validate_plan(plan):
	if not isinstance(plan, dict) or not isinstance(plan.get("milestones"), list) or not plan["milestones"]:
		return "plan.milestones is missing"
	if len(plan["milestones"]) > 8:
		return "too many milestones"
	seen = set()
	roles = {"architect", "worker", "challenger", "explorer", "critic", "tester"}
	flat = []
	for ms in plan["milestones"]:
		if not isinstance(ms, dict) or not ms.get("id") or not isinstance(ms.get("items"), list):
			return "bad milestone"
		if len(ms["items"]) > 40:
			return "too many items"
		for item in ms["items"]:
			if not isinstance(item, dict):
				return "bad item"
			ident = str(item.get("id") or "")
			if not ident or len(ident) > 80 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for ch in ident):
				return "bad id " + ident
			if ident in seen:
				return "duplicate id " + ident
			seen.add(ident)
			if item.get("role") not in roles:
				return "bad role on " + ident
			try:
				item["owns"] = [clean_rel(path) for path in item.get("owns") or []]
			except ValueError as exc:
				return ident + " " + str(exc)
			for path in item["owns"]:
				if path == ".ocswarm" or path.startswith(".ocswarm/"):
					return ident + " owns a control path"
			item["depends_on"] = [str(dep) for dep in item.get("depends_on") or []]
			item["title"] = str(item.get("title") or ident)
			item["brief"] = str(item.get("brief") or "")
			item["verify"] = str(item.get("verify") or "")
			if not item.get("state"):
				item["state"] = "pending"
			item["tries"] = int(item.get("tries") or 0)
			if not item.get("verdict"):
				item["verdict"] = ""
			flat.append(item)
	known = {item["id"] for item in flat}
	deps = {item["id"]: item["depends_on"] for item in flat}
	for item in flat:
		for dep in item["depends_on"]:
			if dep not in known:
				return item["id"] + " depends on missing " + dep
		if item["role"] in ("worker", "architect", "challenger") and not item["owns"]:
			return item["id"] + " has no owns"
		if reaches(deps, item["id"], item["id"]):
			return "cycle at " + item["id"]
	live = [item for item in flat if item.get("state") != "done"]
	for i, left in enumerate(live):
		for right in live[i + 1:]:
			if left["owns"] and right["owns"] and overlaps(left["owns"], right["owns"]):
				if not reaches(deps, left["id"], right["id"]) and not reaches(deps, right["id"], left["id"]):
					return left["id"] + " overlaps " + right["id"]
	return ""


def inject_critics(plan):
	for ms in plan["milestones"]:
		extra = []
		for item in ms["items"]:
			if item["role"] not in ("worker", "architect", "challenger"):
				continue
			ident = item["id"] + "-critic"
			if any(other["id"] == ident for other in ms["items"]):
				continue
			extra.append({
				"id": ident,
				"role": "critic",
				"title": "Review " + item["id"],
				"brief": "Review item " + item["id"] + ". Fail a fake test, a stub, or an edit outside its owns list: " + ", ".join(item["owns"]),
				"owns": [],
				"depends_on": [item["id"]],
				"verify": "",
				"state": "pending",
				"tries": 0,
				"verdict": "",
			})
		ms["items"].extend(extra)


class Board:
	def __init__(self, root):
		self.root = root
		self.dir = root / ".ocswarm"
		self.dir.mkdir(parents=True, exist_ok=True)
		self.path = self.dir / "board.json"
		self.events = self.dir / "events.jsonl"
		self.log = self.dir / "supervisor.log"
		self.data = {
			"task": "",
			"phase": "boot",
			"milestone": "",
			"started": now(),
			"updated": now(),
			"ok": None,
			"message": "boot",
			"done": 0,
			"total": 0,
			"slots": [],
		}

	def set(self, **kwargs):
		self.data.update(kwargs)
		self.data["updated"] = now()
		write_json(self.path, self.data)
		line = self.data["phase"] + " " + self.data["message"]
		if line == getattr(self, "last_line", None):
			return
		self.last_line = line
		with self.log.open("a", encoding="utf-8") as handle:
			handle.write(self.data["updated"] + " " + line + "\n")

	def event(self, kind, **kwargs):
		append_jsonl(self.events, {"time": now(), "kind": kind, **kwargs})


def public_slots(slots):
	rows = []
	for slot in slots.values():
		rows.append({
			"name": slot["name"],
			"role": slot["role"],
			"state": slot["state"],
			"item": slot["item"],
			"since": slot["since"],
			"activity": slot["activity"],
			"model": slot.get("model", ""),
			"log": str(slot.get("log") or ""),
			"elapsed": max(0, round((time.time() if slot.get("proc") else slot.get("finished_at", time.time())) - slot.get("started_at", time.time()))),
		})
	return rows


def make_slots():
	slots = {}
	for name, role in SLOTS:
		slots[name] = {
			"name": name,
			"role": role,
			"state": "wait",
			"item": "",
			"since": "",
			"activity": "",
			"beat": time.time(),
			"proc": None,
			"log": None,
			"thread": None,
			"result": None,
			"fresh": False,
			"code": None,
			"snap": None,
			"allow": [],
		}
	return slots


def pump(proc, log_path, slot):
	with log_path.open("ab") as handle:
		while True:
			line = proc.stdout.readline()
			if not line:
				break
			handle.write(line)
			handle.flush()
			text = activity_from_line(line)
			if text:
				slot["activity"] = text
				slot["beat"] = time.time()


def start_agent(slot, exe, url, cwd, prompt, log_path, snap, allow, model=None, env=None):
	if len(prompt) > 24000:
		prompt = prompt[:24000] + "\n\nEnd with OC_SWARM_JSON and one JSON object."
	log_path.parent.mkdir(parents=True, exist_ok=True)
	if log_path.exists():
		log_path = log_path.with_name(log_path.stem + "-" + str(time.time_ns()) + log_path.suffix)
	log_path.write_bytes(b"")
	env = opencode_env() if env is None else env
	env["CARGO_TARGET_DIR"] = str(RUNTIME / "cargo" / slot["name"])
	model = model or MODEL
	command = [exe, "run", "--standalone", "--model", model, "--agent", "build", "--auto", "--format", "json", "--title", slot["name"] + " " + slot["item"], prompt]
	if model.startswith("gemini/"):
		if allow or slot["role"] != "reviewer":
			raise ValueError("Gemini is configured only for reviews without file edits.")
		prompt_file = RUNTIME / "gemini-prompts" / (slot["name"] + "-" + str(time.time_ns()) + ".txt")
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		prompt_file.write_text(prompt, encoding="utf-8")
		command = [sys.executable, str(Path(__file__).with_name("gemini_agent.py")), "--prompt-file", str(prompt_file), "--cwd", str(cwd)]
	elif model.startswith(("groq/", "cloudflare/")):
		prompt_file = RUNTIME / "compact-prompts" / (slot["name"] + "-" + str(time.time_ns()) + ".txt")
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		prompt_file.write_text(prompt, encoding="utf-8")
		command = [sys.executable, str(Path(__file__).with_name("bounded_agent.py")), "--config", env["OPENCODE_CONFIG"], "--model", model, "--prompt-file", str(prompt_file)]
	if snap is None and slot["role"] not in ("worker", "architect", "challenger"):
		snap = take_snap(Path(cwd), RUNTIME / "snap" / str(os.getpid()) / ("ro-" + slot["name"]))
		allow = [".ocswarm/scratch/" + slot["name"]]
	proc = subprocess.Popen(
		command,
		cwd=str(cwd),
		env=env,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		stdin=subprocess.DEVNULL,
		creationflags=NO_WINDOW,
	)
	slot["proc"] = proc
	slot["model"] = model
	slot["log"] = log_path
	slot["snap"] = snap
	slot["allow"] = allow
	slot["root"] = Path(cwd)
	slot["result"] = None
	slot["fresh"] = False
	slot["code"] = None
	slot["state"] = "run"
	slot["since"] = now()
	slot["started_at"] = time.time()
	slot["beat"] = time.time()
	slot["activity"] = "start"
	thread = threading.Thread(target=pump, args=(proc, log_path, slot), daemon=True)
	slot["thread"] = thread
	thread.start()


def reap(slot):
	proc = slot.get("proc")
	if proc is None:
		return
	if proc.poll() is None:
		if time.time() - slot["beat"] > STALL_MINUTES * 60:
			kill_tree(proc.pid)
			slot["activity"] = "stall"
		return
	if slot.get("thread"):
		slot["thread"].join(10)
	slot["code"] = proc.returncode
	slot["finished_at"] = time.time()
	slot["result"] = extract(slot["log"]) if slot.get("log") else None
	if slot.get("snap") and slot.get("root") and slot["role"] not in ("worker", "architect", "challenger"):
		restore_strays(slot["root"], slot["snap"], slot.get("allow") or [])
	for src, dest in ACTIVE_PROTECTS:
		if dest.exists() and protect_differs(src, dest):
			restore_protect(src, dest)
	slot["proc"] = None
	slot["fresh"] = True
	slot["state"] = "wait"
	slot["activity"] = (slot["result"] or {}).get("summary") or slot["activity"] or "exit " + str(slot["code"])


def live_count(slots):
	return sum(1 for slot in slots.values() if slot["state"] == "run")


def idle_named(slots, name):
	slot = slots[name]
	return slot["state"] != "run" and not slot["fresh"]


def prompt_for(slot, text):
	body = BODIES[slot["role"]]
	shell = "Do not run a shell. Do not run cargo. " if not AGENT_BASH else ""
	return body + "\nYou are " + slot["name"] + ".\n" + shell + text + "\n\nEnd with OC_SWARM_JSON and one JSON object."


def consume(slot, board, on_result):
	if not slot["fresh"]:
		return False
	slot["fresh"] = False
	on_result(slot, board)
	return True


def item_allow(item, slot_name):
	allow = list(item.get("owns") or [])
	allow.append(".ocswarm/scratch/" + slot_name)
	allow.append(".ocswarm/outbox")
	allow.append(".ocswarm/notes")
	return allow


def find_item(milestones, ident):
	for ms in milestones:
		for item in ms["items"]:
			if item["id"] == ident:
				return item
	return None


def ready_items(ms):
	done = {item["id"] for item in ms["items"] if item["state"] == "done"}
	out = []
	for item in ms["items"]:
		if item["state"] != "pending":
			continue
		if all(dep in done for dep in item["depends_on"]):
			out.append(item)
	return out


def milestone_settled(ms):
	return all(item["state"] in ("done", "failed", "blocked") for item in ms["items"])


def milestone_ok(ms):
	if any(item["state"] != "done" for item in ms["items"]):
		return False
	if any(item.get("verdict") == "fail" for item in ms["items"]):
		return False
	return True


def needs_repair(ms):
	if any(item["state"] == "running" for item in ms["items"]):
		return False
	if ready_items(ms):
		return False
	return not milestone_ok(ms)


ORCH_BRIEF = """You are the only orchestrator. Nineteen OpenCode workers do the edits. You do not edit product source.

Write .ocswarm/assignments.json and nothing else under the product tree. The file shape is:
{"tasks":[{"id":"t1","title":"short","brief":"what to change","owns":["relative/file"]}]}

Rules:
- Keep at least 19 tasks that are not finished, so every worker has a task.
- id uses only letters, numbers, dots, and dashes.
- owns lists relative product paths. Do not use .ocswarm.
- Two tasks that can run together must not share a path.
- Read .ocswarm/traces.jsonl before you add the next task.
- When workers finish, add new tasks. Do not stop while the user task is open.
"""


def freebuff_exe():
	return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "@codebufffreebuff-desktop" / "Freebuff.exe"


def freebuff_log():
	return Path(os.environ.get("APPDATA", "")) / "Freebuff" / "logs" / "orchestrator-stderr.log"


def freebuff_port():
	path = freebuff_log()
	if not path.is_file():
		return 0
	port = 0
	for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
		mark = "listening on http://127.0.0.1:"
		if mark in line:
			port = int(line.rsplit(":", 1)[-1].strip())
	return port


def parse_orchestrator_ready(text):
	found = None
	for line in text.splitlines():
		if "orchestrator-ready" not in line:
			continue
		start = line.find("{")
		if start < 0:
			continue
		try:
			obj = json.loads(line[start:])
		except json.JSONDecodeError:
			continue
		if not obj.get("port"):
			continue
		token = str(obj.get("launchId") or "")
		if token == "[redacted]" or len(token) < 16:
			continue
		found = (token, int(obj["port"]))
	return found


def read_orchestrator_ready(paths):
	chunks = []
	for path in paths:
		if path.is_file():
			chunks.append(path.read_text(encoding="utf-8", errors="replace"))
	return parse_orchestrator_ready("\n".join(chunks))


def port_open(port):
	if not port:
		return False
	try:
		sock = socket.create_connection((SERVE_HOST, port), 1)
	except OSError:
		return False
	sock.close()
	return True


def freebuff_request(port, token, method, path, body):
	data = None if body is None else json.dumps(body).encode("utf-8")
	headers = {"content-type": "application/json"}
	if token:
		headers["x-freebuff-launch-id"] = token
	req = urllib.request.Request(
		"http://" + SERVE_HOST + ":" + str(port) + path,
		data=data,
		method=method,
		headers=headers,
	)
	try:
		with urllib.request.urlopen(req, timeout=30) as resp:
			raw = resp.read().decode("utf-8", "replace")
	except urllib.error.HTTPError as exc:
		detail = exc.read().decode("utf-8", "replace")[:300]
		die("Freebuff " + method + " " + path + " failed: " + str(exc.code) + " " + detail)
	if not raw:
		return {}
	return json.loads(raw)


def thread_id_of(obj):
	if not isinstance(obj, dict):
		return ""
	if isinstance(obj.get("id"), str):
		return obj["id"]
	thread = obj.get("thread")
	if isinstance(thread, dict) and isinstance(thread.get("id"), str):
		return thread["id"]
	return ""


def thread_busy(obj):
	thread = obj.get("thread") if isinstance(obj, dict) and isinstance(obj.get("thread"), dict) else obj
	if not isinstance(thread, dict):
		return False
	status = str(thread.get("status") or "")
	if status in ("running", "busy", "streaming"):
		return True
	return thread.get("running") is True


def ensure_freebuff(token_path):
	exe = freebuff_exe()
	if not exe.is_file():
		die("Freebuff is not installed at " + str(exe))
	spawn_log = RUNTIME / "freebuff-spawn.log"
	listening = port_open(freebuff_port())
	if listening and not token_path.is_file():
		die("Freebuff is already open. Close it, then run ocswarm again.")
	if not listening:
		env = os.environ.copy()
		env["FREEBUFF_LAUNCH_ID"] = secrets.token_urlsafe(24)
		env["FREEBUFF_DESKTOP_ECHO_LAUNCH_ID"] = "1"
		spawn_log.parent.mkdir(parents=True, exist_ok=True)
		handle = spawn_log.open("wb")
		subprocess.Popen([str(exe)], env=env, stdout=handle, stderr=subprocess.STDOUT)
		deadline = time.time() + 45
		found = None
		while time.time() < deadline and not found:
			found = read_orchestrator_ready([spawn_log, freebuff_log()])
			if found and not port_open(found[1]):
				found = None
			time.sleep(0.3)
		if not found:
			die("Freebuff did not print orchestrator-ready")
		token, port = found
		write_json(token_path, {"token": token, "port": port})
		freebuff_request(port, token, "GET", "/healthz", None)
		return port, token
	saved = json.loads(token_path.read_text(encoding="utf-8"))
	token = str(saved.get("token") or "")
	port = int(saved.get("port") or freebuff_port())
	try:
		freebuff_request(port, token, "GET", "/healthz", None)
	except SystemExit:
		if token_path.is_file():
			token_path.unlink()
		die("Freebuff is already open with a different launch id. Close it, then run ocswarm again.")
	return port, token


def open_orchestrator(port, token, root, task):
	created = freebuff_request(port, token, "POST", "/api/threads", {
		"title": "ocswarm orchestrator",
		"projectPath": str(root),
		"harnessId": ORCH_HARNESS,
		"model": ORCH_MODEL,
	})
	ident = thread_id_of(created)
	if not ident:
		die("Freebuff did not return a thread id")
	# /agent returns rejected and clears the model. Create already set the harness and the model.
	freebuff_request(port, token, "POST", "/api/thread/" + ident + "/effort", {"effort": ORCH_EFFORT, "projectPath": str(root)})
	got = freebuff_request(port, token, "GET", "/api/thread/" + ident, None)
	thread = got.get("thread") if isinstance(got, dict) and isinstance(got.get("thread"), dict) else got
	if not isinstance(thread, dict) or thread.get("reasoningEffort") != ORCH_EFFORT or not thread.get("model"):
		die("Freebuff did not keep model " + ORCH_MODEL + " at effort " + ORCH_EFFORT)
	freebuff_request(port, token, "POST", "/api/thread/" + ident + "/message", {"text": ORCH_BRIEF + "\n\nUser task:\n" + task, "projectPath": str(root)})
	return ident


def validate_task(raw, seen, busy_owns):
	if not isinstance(raw, dict):
		return "bad task", None
	ident = str(raw.get("id") or "")
	if not ident or len(ident) > 80 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for ch in ident):
		return "bad task id", None
	if ident in seen:
		return "", None
	try:
		owns = [clean_rel(path) for path in raw.get("owns") or []]
	except ValueError as exc:
		return ident + " " + str(exc), None
	if not owns:
		return ident + " has no owns", None
	for path in owns:
		if path == ".ocswarm" or path.startswith(".ocswarm/"):
			return ident + " owns a control path", None
	for own in busy_owns:
		if overlaps(owns, own):
			return ident + " overlaps a live task", None
	task = {
		"id": ident,
		"title": str(raw.get("title") or ident),
		"brief": str(raw.get("brief") or ""),
		"owns": owns,
		"state": "pending",
		"tries": 0,
		"verdict": "",
		"role": "worker",
	}
	return "", task


def load_assignments(path, seen, busy_owns):
	if not path.is_file():
		return [], ""
	try:
		obj = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError:
		return [], "assignments.json is not JSON"
	tasks = obj.get("tasks") if isinstance(obj, dict) else None
	if not isinstance(tasks, list):
		return [], "assignments.tasks is missing"
	fresh = []
	owns = list(busy_owns)
	errors = []
	for raw in tasks:
		err, task = validate_task(raw, seen, owns)
		if err:
			errors.append(err)
			continue
		if task:
			fresh.append(task)
			seen.add(task["id"])
			owns.append(task["owns"])
	if errors and not fresh:
		return [], errors[0]
	return fresh, ""


def fill_workers(slots, queue):
	waiting = []
	for name, kind in SLOTS:
		if kind != "worker":
			continue
		slot = slots[name]
		if slot["state"] == "run":
			continue
		if slot.get("task") and slot["task"].get("state") == "running":
			continue
		if slot.get("task") and slot["task"].get("state") == "pending":
			slot["state"] = "armed"
			slot["item"] = slot["task"]["id"]
			slot["activity"] = slot["task"].get("last_error") or slot["task"]["title"]
			continue
		slot["task"] = None
		if not queue:
			slot["state"] = "wait"
			slot["activity"] = "waiting for a task"
			slot["item"] = ""
			waiting.append(name)
			continue
		task = queue.pop(0)
		slot["task"] = task
		slot["state"] = "armed"
		slot["item"] = task["id"]
		slot["activity"] = task["title"]
	return waiting


def retry_or_fail(item, slot, board, reason):
	item["last_error"] = reason
	item["summary"] = reason
	if item["tries"] < MAX_ITEM_TRIES:
		item["state"] = "pending"
		slot["state"] = "wait"
		slot["activity"] = reason[:160]
		board.event("retry", item=item["id"], why=reason)
		return "retry"
	item["state"] = "failed"
	slot["state"] = "fail"
	slot["activity"] = reason[:160]
	board.event("fail", item=item["id"], why=reason)
	return ""


def bar(done, total, width):
	if total <= 0:
		return "-" * width
	fill = int(width * done / total)
	return "#" * fill + "-" * (width - fill)


def enable_vt():
	if os.name != "nt":
		return None
	kernel = ctypes.windll.kernel32
	handle = kernel.GetStdHandle(-11)
	mode = ctypes.c_uint()
	if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
		return None
	kernel.SetConsoleMode(handle, mode.value | 0x0004)
	return (handle, mode.value)


def restore_vt(saved):
	if not saved:
		return
	ctypes.windll.kernel32.SetConsoleMode(saved[0], saved[1])


def draw(board):
	cols = shutil.get_terminal_size((100, 30)).columns
	data = board
	lines = []
	lines.append("ocswarm  " + str(data.get("phase")) + "  " + str(data.get("milestone") or "-") + "  " + str(data.get("done")) + "/" + str(data.get("total")))
	lines.append(bar(int(data.get("done") or 0), int(data.get("total") or 0), min(40, cols)))
	lines.append(str(data.get("message") or "")[:cols])
	lines.append("")
	for slot in data.get("slots") or []:
		name = str(slot.get("name")).ljust(16)
		state = str(slot.get("state")).ljust(6)
		item = str(slot.get("item") or "-")[:14].ljust(14)
		activity = str(slot.get("activity") or "")[: max(10, cols - 42)]
		lines.append(name + " " + state + " " + item + " " + activity)
	lines.append("")
	lines.append("q close view    s stop swarm")
	sys.stdout.write("\x1b[H")
	for line in lines:
		sys.stdout.write(line + "\x1b[K\n")
	sys.stdout.write("\x1b[J")
	sys.stdout.flush()


def read_key():
	if os.name != "nt":
		return ""
	import msvcrt
	if not msvcrt.kbhit():
		return ""
	ch = msvcrt.getwch()
	if ch in ("\x00", "\xe0"):
		msvcrt.getwch()
		return ""
	return ch.lower()


def watch(root):
	path = root / ".ocswarm" / "board.json"
	saved = enable_vt()
	sys.stdout.write("\x1b[?1049h\x1b[?25l")
	try:
		while True:
			if path.is_file():
				draw(json.loads(path.read_text(encoding="utf-8")))
			else:
				sys.stdout.write("\x1b[Hno board yet at " + str(path) + "\x1b[K\n")
				sys.stdout.flush()
			key = read_key()
			if key == "q":
				return
			if key == "s":
				(root / ".ocswarm").mkdir(parents=True, exist_ok=True)
				(root / ".ocswarm" / "STOP").write_text("stop\n", encoding="utf-8")
			time.sleep(0.4)
	finally:
		sys.stdout.write("\x1b[?25h\x1b[?1049l")
		sys.stdout.flush()
		restore_vt(saved)


def stop_swarm(root):
	board_dir = root / ".ocswarm"
	board_dir.mkdir(parents=True, exist_ok=True)
	(board_dir / "STOP").write_text("stop\n", encoding="utf-8")
	pid_path = board_dir / "pids.json"
	if pid_path.is_file():
		pids = json.loads(pid_path.read_text(encoding="utf-8"))
		kill_tree(pids.get("supervisor"))
		kill_tree(pids.get("serve"))
		kill_tree(pids.get("daemon"))
	print("stop requested for " + str(root))


def status(root):
	path = root / ".ocswarm" / "board.json"
	if not path.is_file():
		die("no board at " + str(path))
	data = json.loads(path.read_text(encoding="utf-8"))
	print(data.get("phase"), data.get("message"))
	for slot in data.get("slots") or []:
		print(slot["name"].ljust(16), slot["state"].ljust(6), slot.get("item") or "-", slot.get("activity") or "")


def supervisor_log_tail(path):
	if not path.is_file():
		return ""
	raw = path.read_bytes()[-2000:]
	return raw.decode("utf-8", "replace")


def supervise_legacy(launch):
	root = Path(launch["directory"]).resolve()
	if not root.is_dir():
		die("missing directory " + str(root))
	os.chdir(root)
	install_agents()
	exe = find_opencode()
	board = Board(root)
	crash_path = board.dir / "crash.log"
	if crash_path.is_file():
		crash_path.unlink()
	board.data["task"] = launch["task"]
	slots = make_slots()
	stop_path = board.dir / "STOP"
	if stop_path.exists():
		stop_path.unlink()
	pid_path = board.dir / "pids.json"
	if pid_path.is_file():
		old = json.loads(pid_path.read_text(encoding="utf-8"))
		if pid_alive(old.get("supervisor")) and old.get("supervisor") != os.getpid():
			die("swarm already runs as pid " + str(old.get("supervisor")))
	write_json(pid_path, {"supervisor": os.getpid(), "serve": 0})
	write_json(board.dir / "request.json", {"task": launch["task"], "acceptance": launch.get("accept") or []})
	protects = []
	for i, raw in enumerate(launch.get("protect") or []):
		src = Path(raw)
		if not src.exists():
			die("protect path missing " + raw)
		dest = RUNTIME / "protect" / str(os.getpid()) / str(i)
		board.set(message="copy protect " + src.name)
		copy_protect(src, dest)
		protects.append((src, dest))
	global ACTIVE_PROTECTS
	ACTIVE_PROTECTS = protects
	try:
		token_path = RUNTIME / "freebuff-launch.json"
		port, token = ensure_freebuff(token_path)
		thread = open_orchestrator(port, token, root, launch["task"])
		write_json(board.dir / "orchestrator.json", {"thread": thread, "model": ORCH_MODEL, "effort": ORCH_EFFORT})
		started = time.monotonic()
		phase = "work"
		queue = []
		seen = set()
		accepted = []
		base = take_snap(root, RUNTIME / "snap" / str(os.getpid()) / "base")
		last_wake = time.time()
		last_assign = 0
		test_log = board.dir / "evidence" / "accept.log"
		routes = WorkerRoutes()

		def publish(message):
			done = 0
			live = 0
			for _name, kind in SLOTS:
				if kind != "worker":
					continue
				task = slots[_name].get("task")
				if task and task.get("state") == "done":
					done += 1
				if task and task.get("state") in ("running", "pending"):
					live += 1
			board.set(phase=phase, milestone="workers", message=message, done=done, total=done + live + len(queue), slots=public_slots(slots), ok=board.data.get("ok"), worker_model=routes.selected, live_limit=routes.limit)

		def stop_reason():
			if stop_path.exists():
				return "stop file"
			if (time.monotonic() - started) > float(launch["hours"]) * 3600:
				return "hour cap"
			return ""

		def finish_all():
			for slot in slots.values():
				proc = slot.get("proc")
				if proc and proc.poll() is None:
					kill_tree(proc.pid)

		def busy_owns():
			owns = []
			for task in queue:
				owns.append(task["owns"])
			for name, kind in SLOTS:
				if kind != "worker":
					continue
				task = slots[name].get("task")
				if task and task.get("state") in ("running", "pending"):
					owns.append(task["owns"])
			return owns

		def wake(reason):
			nonlocal last_wake
			text = ORCH_BRIEF + "\n\nUser task:\n" + launch["task"] + "\n\n" + reason
			trace = board.dir / "traces.jsonl"
			if trace.is_file():
				text += "\n\nRecent traces:\n" + trace.read_text(encoding="utf-8", errors="replace")[-4000:]
			freebuff_request(port, token, "POST", "/api/thread/" + thread + "/message", {"text": text, "projectPath": str(root)})
			last_wake = time.time()
			slots["orchestrator"]["state"] = "run"
			slots["orchestrator"]["activity"] = reason[:160]
			board.event("wake", reason=reason)

		slots["orchestrator"]["state"] = "run"
		slots["orchestrator"]["item"] = thread
		slots["orchestrator"]["activity"] = ORCH_MODEL + " effort " + ORCH_EFFORT
		publish("orchestrator started")
		while True:
			for slot in slots.values():
				reap(slot)
			why = stop_reason()
			if why:
				phase = "stopped"
				publish(why)
				board.data["ok"] = False
				break
			assign_path = board.dir / "assignments.json"
			if assign_path.is_file():
				mtime = assign_path.stat().st_mtime
				if mtime != last_assign:
					fresh, err = load_assignments(assign_path, seen, busy_owns())
					last_assign = mtime
					if err:
						board.event("assignments-rejected", error=err)
						if time.time() - last_wake > WAKE_SECONDS:
							wake("The assignments file was rejected: " + err)
					else:
						queue.extend(fresh)
						slots["orchestrator"]["activity"] = str(len(queue)) + " tasks queued"
			for name, kind in SLOTS:
				if kind != "worker":
					continue
				slot = slots[name]
				if not slot["fresh"]:
					continue
				slot["fresh"] = False
				task = slot.get("task")
				if not task:
					continue
				flag = handle_item_result(slot, {"items": [task]}, board, root, protects)
				if flag in ("rate", "free"):
					task["state"] = "pending"
					if routes.reject(slot["model"], flag, slot["log"]):
						state = routes.states[slot["model"]]
						board.event("model-rejected", model=slot["model"], reason=flag, hits=state["hits"], retry_at=state["retry_at"], disabled=state["disabled"])
					continue
				if task.get("state") == "done":
					routes.success(slot["model"])
					append_jsonl(board.dir / "traces.jsonl", {"worker": name, "id": task["id"], "summary": task.get("summary") or ""})
					accepted.extend(task["owns"])
					slot["task"] = None
				elif task.get("state") == "failed":
					append_jsonl(board.dir / "traces.jsonl", {"worker": name, "id": task["id"], "summary": "failed: " + (task.get("summary") or "")})
					slot["task"] = None
					if time.time() - last_wake > WAKE_SECONDS:
						wake("Worker " + name + " failed " + task["id"] + ". Assign a replacement task.")
			waiting = fill_workers(slots, queue)
			for name, kind in SLOTS:
				if kind != "worker":
					continue
				slot = slots[name]
				if slot["state"] != "armed":
					continue
				model = routes.model()
				running = sum(1 for _name, _kind in SLOTS if _kind == "worker" and slots[_name]["state"] == "run")
				if not model or running >= routes.limit:
					slot["activity"] = "waiting for model capacity"
					continue
				task = slot["task"]
				dispatch_item(slot, task, exe, "", root, board, launch, {}, {"items": [task]}, model=model)
				running = sum(1 for _name, _kind in SLOTS if _kind == "worker" and slots[_name]["state"] == "run")
				publish(str(running) + " running, " + str(len(waiting)) + " waiting, " + str(len(queue)) + " queued")
			owned_now = list(accepted)
			for task in queue:
				owned_now.extend(task["owns"])
			for _name, _kind in SLOTS:
				if _kind != "worker":
					continue
				_task = slots[_name].get("task")
				if _task:
					owned_now.extend(_task["owns"])
			running = sum(1 for _name, _kind in SLOTS if _kind == "worker" and slots[_name]["state"] == "run")
			if not routes.model():
				phase = "blocked" if routes.exhausted() else "cooldown"
				publish(routes.message())
			else:
				phase = "work"
				publish(str(running) + " running, " + str(len(waiting)) + " waiting, " + str(len(queue)) + " queued")
			restore_strays(root, base, owned_now)
			if routes.exhausted():
				board.data["ok"] = False
				break
			if waiting and phase == "work" and time.time() - last_wake > WAKE_SECONDS:
				wake("Workers have no task. Add at least " + str(len(waiting)) + " tasks to .ocswarm/assignments.json.")
			time.sleep(POLL_SECONDS)
		publish(board.data["message"])
		write_json(board.dir / "result.json", {"ok": board.data.get("ok"), "phase": phase, "message": board.data.get("message")})
	except SystemExit as exc:
		(board.dir / "crash.log").write_text(str(exc) + "\n", encoding="utf-8")
		raise
	except Exception:
		(board.dir / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
		raise
	finally:
		fin = locals().get("finish_all")
		if fin:
			fin()
		if pid_path.is_file():
			left = json.loads(pid_path.read_text(encoding="utf-8"))
			left["supervisor"] = 0
			write_json(pid_path, left)

def pick_slot(slots, role):
	names = [name for name, kind in SLOTS if kind == role]
	for name in names:
		slot = slots[name]
		if slot["state"] != "run" and not slot["fresh"]:
			return slot
	return None


def dispatch_item(slot, item, exe, url, root, board, launch, findings, ms, model=None):
	item["state"] = "running"
	item["tries"] = item.get("tries", 0) + 1
	slot["item"] = item["id"]
	allow = item_allow(item, slot["name"])
	snap = None
	if item["role"] in ("worker", "architect", "challenger"):
		snap = take_snap(root, RUNTIME / "snap" / str(os.getpid()) / item["id"])
	notes = ""
	notes_path = board.dir / "notes.jsonl"
	if notes_path.is_file():
		notes = notes_path.read_text(encoding="utf-8")[-4000:]
	text = "Item " + item["id"] + " role " + item["role"] + "\nTitle: " + item["title"] + "\nOwns: " + json.dumps(item["owns"]) + "\n\n" + item["brief"] + "\n\nTask:\n" + launch["task"] + "\n\nNotes:\n" + notes
	if item.get("last_error"):
		text += "\n\nThe previous attempt failed: " + item["last_error"] + "\nA done status is rejected until the owned file bytes change."
	if item["role"] == "critic":
		text += "\n\nMilestone items:\n" + json.dumps([{"id": other["id"], "state": other["state"], "summary": other.get("summary")} for other in ms["items"]])[:4000]
	start_agent(slot, exe, url, root, prompt_for(slot, text), board.dir / "logs" / (item["id"] + ".log"), snap, allow, model=model)
	board.event("dispatch", item=item["id"], slot=slot["name"], model=slot["model"])


def handle_item_result(slot, ms, board, root, protects):
	item = find_item([ms], slot["item"])
	if not item:
		return ""
	if slot.get("log"):
		kind = log_kind(slot["log"])
		if kind in ("rate", "free") and not isinstance(slot.get("result"), dict):
			item["state"] = "pending"
			item["tries"] = max(0, int(item.get("tries") or 1) - 1)
			slot["activity"] = kind
			return kind
	for src, dest in protects:
		if protect_differs(src, dest):
			restore_protect(src, dest)
			board.event("protect-restore", path=str(src))
	got = slot["result"] if isinstance(slot["result"], dict) else None
	if got is None and item["tries"] < MAX_ITEM_TRIES:
		item["state"] = "pending"
		slot["activity"] = "no json, retry"
		board.event("retry", item=item["id"])
		return
	if not got or got.get("status") not in ("done", "blocked"):
		item["state"] = "failed"
		item["summary"] = "no result"
		slot["state"] = "fail"
		board.event("fail", item=item["id"])
		return
	if got.get("status") == "blocked":
		item["state"] = "blocked"
		item["summary"] = got.get("summary") or "blocked"
		board.event("blocked", item=item["id"])
		return ""
	problems = []
	if slot.get("snap"):
		problems = restore_strays(root, slot["snap"], slot["allow"])
		if item["role"] in ("worker", "architect", "challenger") and not owned_changed(root, slot["snap"], item["owns"]):
			reason = "owned files did not change"
			if problems:
				reason += ". restored edits outside owns: " + ", ".join(problems[:8])
			return retry_or_fail(item, slot, board, reason)
	if problems and item["role"] in ("worker", "architect", "challenger"):
		return retry_or_fail(item, slot, board, "edited outside owns: " + ", ".join(problems[:8]))
	item["state"] = "done"
	item["summary"] = got.get("summary") or ""
	item["verdict"] = got.get("verdict") or ""
	for note in got.get("notes") or []:
		if isinstance(note, dict):
			append_jsonl(board.dir / "notes.jsonl", {"from": slot["name"], "item": item["id"], **note})
	slot["activity"] = item["summary"]
	slot["state"] = "done"
	board.event("done", item=item["id"], summary=item["summary"])
	return ""


def merge_add(plan, ms_index, added):
	if not isinstance(added, list) or not added:
		return "repair returned no items"
	ms = plan["milestones"][ms_index]
	known = {item["id"] for item in ms["items"]}
	for raw in added:
		if not isinstance(raw, dict):
			return "bad repair item"
		raw.setdefault("owns", [])
		raw.setdefault("depends_on", [])
		raw.setdefault("verify", "")
		raw.setdefault("brief", "")
		raw.setdefault("title", raw.get("id"))
		raw["state"] = "pending"
		raw["tries"] = 0
		raw["verdict"] = ""
		ms["items"].append(raw)
	err = validate_plan({"milestones": [ms]})
	if err:
		# Roll back the appends that failed validation by dropping unknown-state pending adds that were new.
		ms["items"] = [item for item in ms["items"] if item["id"] in known or item["state"] != "pending"]
		return err
	inject_critics({"milestones": [ms]})
	return ""


def load_launch(path):
	return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_run(args):
	launch = {
		"directory": str(Path(args.directory).resolve()),
		"task": " ".join(args.message),
		"hours": args.hours,
		"accept": args.accept or [],
		"protect": args.protect or [],
	}
	root = Path(launch["directory"])
	(root / ".ocswarm").mkdir(parents=True, exist_ok=True)
	from free_swarm import require_stopped
	require_stopped(root / ".ocswarm", sys.modules[__name__])
	stop = root / ".ocswarm" / "STOP"
	if stop.exists():
		stop.unlink()
	path = ROOT / "launch.json"
	write_json(path, launch)
	open_dashboard(launch["directory"])
	if args.detach:
		argv = [sys.executable, str(Path(__file__).resolve()), "daemon", "--launch", str(path)]
		proc = subprocess.Popen(argv, creationflags=DETACH_FLAGS, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, cwd=str(ROOT))
		print("detached pid " + str(proc.pid))
		print("ocswarm watch " + launch["directory"])
		return
	supervise(launch)


def supervise(launch):
	from free_swarm import run
	run(launch, sys.modules[__name__])


def selftest():
	sample = '{"type":"text","timestamp":1,"sessionID":"s","part":{"type":"text","text":"hello\\nOC_SWARM_JSON\\n{\\"status\\":\\"done\\",\\"summary\\":\\"ping\\"}"}}'
	blob = json.loads(sample)["part"]["text"]
	got = parse_marker(blob)
	if not got or got.get("summary") != "ping":
		die("marker parse failed")
	loose = parse_marker("OC_SWARM_JSON\n{status:done,summary:ping}")
	if not loose or loose.get("summary") != "ping":
		die("loose json failed")
	plan = {"milestones": [{"id": "m1", "title": "one", "items": [
		{"id": "arch", "role": "architect", "title": "c", "brief": "c", "owns": ["docs/rule.md"], "depends_on": [], "verify": ""},
		{"id": "w1", "role": "worker", "title": "a", "brief": "a", "owns": ["src/a.rs"], "depends_on": ["arch"], "verify": ""},
		{"id": "w2", "role": "worker", "title": "b", "brief": "b", "owns": ["src/b.rs"], "depends_on": ["arch"], "verify": ""},
	]}]}
	if validate_plan(plan):
		die("good plan rejected " + validate_plan(plan))
	bad = json.loads(json.dumps(plan))
	bad["milestones"][0]["items"][2]["owns"] = ["src/a.rs"]
	bad["milestones"][0]["items"][2]["depends_on"] = ["arch"]
	if not validate_plan(bad):
		die("overlap was accepted")
	root = ROOT / "selftest-tree"
	if root.exists():
		shutil.rmtree(root)
	(root / "src").mkdir(parents=True)
	(root / "src" / "a.rs").write_text("old\n", encoding="utf-8")
	(root / "keep.txt").write_text("keep\n", encoding="utf-8")
	dest = root / ".ocswarm" / "snap" / "t"
	take_snap(root, dest)
	(root / "src" / "a.rs").write_text("new\n", encoding="utf-8")
	(root / "keep.txt").write_text("changed\n", encoding="utf-8")
	(root / "stray.txt").write_text("x\n", encoding="utf-8")
	problems = restore_strays(root, dest, ["src/a.rs"])
	if (root / "src" / "a.rs").read_text(encoding="utf-8") != "new\n":
		die("owned file was restored")
	if (root / "keep.txt").read_text(encoding="utf-8") != "keep\n":
		die("stray edit survived")
	if (root / "stray.txt").exists():
		die("new stray survived")
	if "keep.txt" not in problems:
		die("stray was not reported")
	shutil.rmtree(root)
	global SNAP_MAX_BYTES
	cap = SNAP_MAX_BYTES
	SNAP_MAX_BYTES = 8
	root = ROOT / "selftest-tree"
	(root / "src").mkdir(parents=True)
	(root / "big.bin").write_bytes(b"0123456789abcdef")
	(root / "small.txt").write_text("s\n", encoding="utf-8")
	dest = root / ".ocswarm" / "snap" / "t"
	take_snap(root, dest)
	if (dest / "big.bin").exists():
		die("oversized file was copied")
	listed = json.loads((dest / "_index.json").read_text(encoding="utf-8"))
	if "big.bin" not in listed:
		die("oversized file was left out of the index")
	(root / "big.bin").write_bytes(b"zzzzzzzzzzzzzzzz")
	(root / "small.txt").write_text("t\n", encoding="utf-8")
	restore_strays(root, dest, [])
	if (root / "big.bin").read_bytes() != b"zzzzzzzzzzzzzzzz":
		die("uncopied large file was restored")
	if (root / "small.txt").read_text(encoding="utf-8") != "s\n":
		die("small file was not restored")
	SNAP_MAX_BYTES = cap
	shutil.rmtree(root)
	stuck = {"items": [
		{"id": "a", "state": "failed", "depends_on": [], "owns": ["a.rs"]},
		{"id": "b", "state": "pending", "depends_on": ["a"], "owns": ["b.rs"]},
	]}
	if not needs_repair(stuck):
		die("stuck milestone was not sent to repair")
	ready = {"items": [{"id": "a", "state": "pending", "depends_on": [], "owns": ["a.rs"]}]}
	if needs_repair(ready):
		die("ready milestone was sent to repair")
	ready = parse_orchestrator_ready('[orchestrator] [orchestrator-ready] {"launchId":"abc","pid":1,"port":53457}')
	if ready is not None:
		die("short launch id was accepted")
	ready = parse_orchestrator_ready('[orchestrator] [orchestrator-ready] {"launchId":"[redacted]","pid":1,"port":53457}')
	if ready is not None:
		die("redacted launch id was accepted")
	ready = parse_orchestrator_ready('[orchestrator] [orchestrator-ready] {"launchId":"abcdefghijklmnop","pid":1,"port":53457}')
	if ready != ("abcdefghijklmnop", 53457):
		die("orchestrator-ready parse failed")
	if len(SLOTS) != 20:
		die("slot count")
	if sum(1 for _name, kind in SLOTS if kind == "worker") != 19:
		die("worker count")
	if SLOTS[0] != ("orchestrator", "orchestrator"):
		die("orchestrator slot")
	slots = make_slots()
	queue = [{"id": "t" + str(i), "title": "t", "brief": "b", "owns": ["f" + str(i) + ".txt"], "state": "pending", "tries": 0, "role": "worker"} for i in range(1, 20)]
	waiting = fill_workers(slots, queue)
	if waiting or queue:
		die("workers were left without a task")
	slots["worker-1"]["state"] = "wait"
	slots["worker-1"]["task"] = None
	queue.append({"id": "t20", "title": "next", "brief": "b", "owns": ["f20.txt"], "state": "pending", "tries": 0, "role": "worker"})
	waiting = fill_workers(slots, queue)
	if slots["worker-1"]["item"] != "t20" or waiting:
		die("finished worker was not given the next task")
	slots["worker-2"]["state"] = "wait"
	slots["worker-2"]["task"] = None
	waiting = fill_workers(slots, [])
	if "worker-2" not in waiting or slots["worker-2"]["state"] != "wait":
		die("empty queue did not mark the worker waiting")
	err, _task = validate_task({"id": "bad", "owns": [".ocswarm/x"]}, set(), [])
	if not err:
		die("control path was accepted")
	print("selftest ok")


def ui_url():
	return "http://" + SERVE_HOST + ":" + str(UI_PORT) + "/"


def listener_pid(port):
	try:
		out = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, errors="replace")
	except (OSError, subprocess.CalledProcessError):
		return 0
	want = str(port)
	for line in out.splitlines():
		parts = line.split()
		if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
			continue
		if parts[1].rsplit(":", 1)[-1] != want:
			continue
		try:
			return int(parts[4])
		except ValueError:
			return 0
	return 0


def dashboard_directory():
	if not port_open(UI_PORT):
		return ""
	try:
		with urllib.request.urlopen(ui_url() + "live.json", timeout=2) as resp:
			body = json.loads(resp.read().decode("utf-8"))
	except (OSError, json.JSONDecodeError, urllib.error.URLError):
		return ""
	if not isinstance(body, dict):
		return ""
	if body.get("buildVersion") != 3:
		return ""
	return str(body.get("directory") or "")


def open_browser(url):
	if os.name == "nt":
		subprocess.Popen(["cmd", "/c", "start", "", url], creationflags=DETACH_FLAGS, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
		return
	subprocess.Popen(["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def open_dashboard(root, show=True):
	root = Path(root).resolve()
	script = DASHBOARD_DIR / "ocswarm-serve.ts"
	if not script.is_file():
		die("Ocswarm dashboard missing at " + str(script))
	if not root.is_dir():
		die("missing directory " + str(root))
	bundle = RUNTIME / "dashboard-server.mjs"
	inputs = [script, script.with_name("render.ts"), script.with_name("data.ts")]
	current_bundle = bundle.is_file() and bundle.stat().st_mtime >= max(p.stat().st_mtime for p in inputs if p.is_file())
	if dashboard_directory() == str(root) and current_bundle:
		if show:
			open_browser(ui_url())
			print(ui_url())
		return
	old = listener_pid(UI_PORT)
	if old:
		kill_tree(old)
		deadline = time.time() + 3
		while time.time() < deadline and port_open(UI_PORT):
			time.sleep(0.1)
	bundle.parent.mkdir(parents=True, exist_ok=True)
	build = "import {buildSync} from 'esbuild'; buildSync({entryPoints:[process.argv[1]],outfile:process.argv[2],bundle:true,platform:'node',format:'esm'});"
	subprocess.run(["node", "--input-type=module", "-e", build, str(script), str(bundle)], cwd=ROOT, check=True, creationflags=NO_WINDOW)
	argv = ["node", str(bundle), str(root), "--port", str(UI_PORT)]
	log_path = RUNTIME / "ui.log"
	log_path.parent.mkdir(parents=True, exist_ok=True)
	handle = log_path.open("ab")
	flags = (DETACH_FLAGS if show else NO_WINDOW) if os.name == "nt" else 0
	env = os.environ.copy()
	env["OCSWARM_ASSET_DIR"] = str(ROOT / "node_modules" / "vis-network" / "standalone" / "umd")
	proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
	write_json(RUNTIME / "ui.json", {"pid": proc.pid, "port": UI_PORT, "directory": str(root)})
	deadline = time.time() + 8
	while time.time() < deadline and not port_open(UI_PORT):
		if proc.poll() is not None:
			die("dashboard exited. see " + str(log_path))
		time.sleep(0.1)
	if not port_open(UI_PORT):
		die("dashboard did not open. see " + str(log_path))
	if show:
		open_browser(ui_url())
		print(ui_url())


def serve_ui(root):
	open_dashboard(root)


def main():
	parser = argparse.ArgumentParser(prog="ocswarm")
	sub = parser.add_subparsers(dest="cmd", required=True)
	run = sub.add_parser("run")
	run.add_argument("--detach", action="store_true")
	run.add_argument("--hours", type=float, default=CAP_HOURS)
	run.add_argument("--accept", action="append", default=[])
	run.add_argument("--protect", action="append", default=[])
	run.add_argument("directory")
	run.add_argument("message", nargs="+")
	sup = sub.add_parser("supervise")
	sup.add_argument("--launch", required=True)
	daemon_p = sub.add_parser("daemon")
	daemon_p.add_argument("--launch", required=True)
	watch_p = sub.add_parser("watch")
	watch_p.add_argument("directory")
	ui_p = sub.add_parser("ui")
	ui_p.add_argument("directory")
	stop_p = sub.add_parser("stop")
	stop_p.add_argument("directory")
	status_p = sub.add_parser("status")
	status_p.add_argument("directory")
	sub.add_parser("selftest")
	sub.add_parser("check")
	sub.add_parser("agents")
	args = parser.parse_args()
	if args.cmd == "selftest":
		selftest()
	elif args.cmd == "check":
		raise SystemExit(check_workers())
	elif args.cmd == "agents":
		install_agents()
		print(CONFIG_DIR)
	elif args.cmd == "run":
		cmd_run(args)
	elif args.cmd == "supervise":
		supervise(load_launch(args.launch))
	elif args.cmd == "daemon":
		from free_swarm import daemon
		daemon(args.launch, sys.modules[__name__])
	elif args.cmd == "watch":
		watch(Path(args.directory).resolve())
	elif args.cmd == "ui":
		serve_ui(Path(args.directory).resolve())
	elif args.cmd == "stop":
		stop_swarm(Path(args.directory).resolve())
	elif args.cmd == "status":
		status(Path(args.directory).resolve())


if __name__ == "__main__":
	main()
