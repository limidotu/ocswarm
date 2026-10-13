import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from free_gateway import FreeGateway, model_config
from swarm_workspace import Workspace
from provider_routes import ProviderRoutes
from clef_router import ClefRouter
import gemini_agent


PLAN_GAP = 120
RECOVERY_GAP = 3600
MISSION = """Reverse engineer Enter the Gungeon and improve its Rust reconstruction continuously.
Use the original managed assembly, recovered method IL, and asset tables as evidence.
Keep the original game binaries and asset files unchanged.
Read etg-rs/README.md. Do not repeat an item marked DONE.
Find the next real behavior or data gap from the current source and evidence.
State the original method or asset path for each claim. Mark unknown behavior as unknown.
Keep the recorded replay fingerprints unchanged. Do not regenerate the corpus to hide a failure.
Use small tasks with explicit owned paths and a test or evidence check.
"""


def agent_environment(core, slot, workspace, project, owns, gateway):
	env = core.opencode_env()
	for key in ("NVIDIA_API_KEY", "GROQ_API_KEY", "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
		env.pop(key, None)
	rules = [{"action": "*", "resource": "*", "effect": "deny"}]
	for action in ("glob", "grep", "list"):
		rules.append({"action": action, "resource": "*", "effect": "allow"})
	for action in ("read", "external_directory"):
		for folder in (workspace, project):
			path = str(Path(folder).resolve()).replace("\\", "/")
			rules.append({"action": action, "resource": path, "effect": "allow"})
			rules.append({"action": action, "resource": path + "/**", "effect": "allow"})
	for path in (".", "etg-rs/**", "rea-analysis/**", "AGENTS.md"):
		rules.append({"action": "read", "resource": path, "effect": "allow"})
	for own in owns:
		for path in (own, str(Path(workspace) / own).replace("\\", "/")):
			rules.append({"action": "edit", "resource": path, "effect": "allow"})
			rules.append({"action": "edit", "resource": path.rstrip("/") + "/**", "effect": "allow"})
	config = {"providers": model_config(gateway.url), "agents": {"build": {"permissions": rules, "steps": 40}}}
	config["ocswarm"] = {"gateway": gateway.url, "workspace": str(workspace), "project": str(project), "owns": owns, "checkpoint": str(core.RUNTIME / "compact-sessions" / (hashlib.sha256((slot["name"] + slot["item"]).encode()).hexdigest() + ".json"))}
	path = core.RUNTIME / "role-config" / (slot["name"] + ".json")
	core.write_json(path, config)
	env["OPENCODE_CONFIG"] = str(path)
	return env


def worker_prompt(task, project):
	return (
		MISSION + "\nYou have one bounded task. Finish it and return your result.\n"
		"This is a private workspace. Edit only the owned relative paths.\n"
		"The supervisor runs the tests and merges accepted changes. Do not run a shell.\n"
		"Read the original evidence at " + str(project) + "/rea-analysis when the local copy is incomplete.\n"
		'If you need original method IL, return status blocked with "research_requests":[{"kind":"method_il","name":"Type.Method"}] or kind method_list with a type name. Ask before editing. The supervisor will run its trusted reader and retry with the evidence path.\n'
		"Do not write a file called OC_SWARM_JSON. That marker belongs only in your final reply.\n"
		"Task: " + task["title"] + "\nOwns: " + json.dumps(task["owns"]) + "\n" + task["brief"] + "\n"
		"Previous failure: " + str(task.get("last_error") or "none") + "\n"
		'End your reply with OC_SWARM_JSON on its own line, then {"status":"done|blocked","summary":"one line","evidence":["method or asset path and finding"],"files":["changed relative paths"]}.'
	)


def planner_prompt(project, task, history, active, round_number, baseline):
	return (
		MISSION + "\nUser goal: " + task + "\nProject: " + str(project) + "\n"
		"You plan only. Read the current project source and evidence. Do not edit files or run a shell.\n"
		"The README's old list is complete. Inspect real gaps before choosing new work.\n"
		"Assign at most four useful tasks with disjoint owned paths. Prefer one small behavior per task.\n"
		"Keep each task brief below 1,200 characters. Split evidence research into one original method per note.\n"
		"The compact free workers need small source ranges. Avoid a six-method investigation in one task.\n"
		"A worker receives a private source copy and can read original evidence by absolute path.\n"
		"Workers cannot run a shell. The supervisor runs cargo tests after each merge.\n"
		"Workers can request original method IL or a method list. The supervisor supplies evidence from the original managed assembly.\n"
		"Own only source, tests, extraction scripts, or new research notes under etg-rs or rea-analysis.\n"
		"Do not own existing recovered JSON tables, binaries, assets, or replay corpus fixtures.\n"
		"Recent results: " + json.dumps(history[-12:])[-16000:] + "\n"
		"Reserved tasks: " + json.dumps([{k: t.get(k) for k in ("id", "title", "owns")} for t in active])[-12000:] + "\n"
		"Current baseline test result: " + baseline[-6000:] + "\n"
		"Use new task IDs starting with p" + str(round_number) + ".\n"
		"For each task, cite an existing evidence path in its brief and state the testable outcome.\n"
		"If the baseline fails, assign a small repair for the first failure before new behavior work.\n"
		"A baseline test can be wrong. Read original method IL before choosing its expected outcome.\n"
		"Include every needed source, test, and CLI path in owns. A worker cannot repair files outside owns.\n"
		"For mirror work, read rea-analysis/MIRROR-EVIDENCE.md: the original adds curse without a curse gate.\n"
		'Do not write a marker file. Return OC_SWARM_JSON on its own line, then {"status":"done","summary":"one line","tasks":[{"id":"p1.task","title":"...","brief":"...","owns":["relative/file"]}]}.'
	)


def validate_tasks(core, rows, tasks, history):
	if not isinstance(rows, list):
		return [], ["The planner returned no task list."]
	seen = {task["id"] for task in tasks} | {task["id"] for task in history}
	busy = [task["owns"] for task in tasks if task["state"] in ("pending", "running")]
	accepted, errors = [], []
	for raw in rows[:4]:
		error, task = core.validate_task(raw, seen, busy)
		if error:
			errors.append(error)
			continue
		if not task:
			continue
		bad = [path for path in task["owns"] if (
			not (path.startswith("etg-rs/") or path.startswith("rea-analysis/"))
			or path.startswith("etg-rs/crates/etg-sim/tests/corpus/")
			or Path(path).suffix.lower() in (".json", ".exe", ".dll", ".assets", ".bundle")
			or path in ("rea-analysis/metadata.mjs", "rea-analysis/il.mjs", "rea-analysis/read-method-il.mjs")
		)]
		if bad:
			errors.append(task["id"] + " owns protected paths: " + ", ".join(bad))
			continue
		accepted.append(task)
		seen.add(task["id"])
		busy.append(task["owns"])
	return accepted, errors


def collect_evidence(core, project, requests):
	paths = []
	script = project / "rea-analysis" / "read-method-il.mjs"
	dll = project / "EtG_Data" / "Managed" / "Assembly-CSharp.dll"
	for request in requests[:4]:
		name = str(request.get("name", ""))
		kind = request.get("kind")
		if kind not in ("method_il", "method_list") or not re.fullmatch(r"[A-Za-z_<][A-Za-z0-9_+.<>]*(?:\.\*)?", name):
			continue
		args = ["node", str(script), "--dll", str(dll)]
		args.extend(["--list", name] if kind == "method_list" else [name])
		key = hashlib.sha256(json.dumps(args).encode()).hexdigest()[:16]
		path = project / ".ocswarm" / "evidence" / "native" / (key + ".txt")
		if not path.exists():
			path.parent.mkdir(parents=True, exist_ok=True)
			with path.open("wb") as output:
				output.write(("Source: " + str(dll) + "\nSHA256: " + hashlib.sha256(dll.read_bytes()).hexdigest() + "\nReader: " + str(script) + "\nRequest: " + kind + " " + name + "\n\n").encode())
				output.flush()
				try:
					process = subprocess.run(args, cwd=project, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=60, creationflags=core.NO_WINDOW)
					output.write(("\nReader exit: " + str(process.returncode) + "\n").encode())
				except subprocess.TimeoutExpired:
					output.write(b"\nReader timeout.\n")
		paths.append(str(path))
	return paths


def failure_metrics(text):
	return {
		"errors": sorted(set(re.findall(r"^error\[E\d+\]: .+", text, re.MULTILINE))),
		"failed": sorted(set(re.findall(r"^test (\S+) \.\.\. FAILED", text, re.MULTILINE))),
		"tests": sum(int(n) for n in re.findall(r"test result: .*? (\d+) passed;", text)) + sum(int(n) for n in re.findall(r"test result: .*? (\d+) failed;", text)),
		"ignored": sum(int(n) for n in re.findall(r"test result: .*? (\d+) ignored;", text)),
	}


def improvement(before, after):
	try:
		old = json.loads(before.splitlines()[0].removeprefix("CHECK_METRICS "))
		new = json.loads(after.splitlines()[0].removeprefix("CHECK_METRICS "))
	except (ValueError, IndexError):
		return False
	old_errors, new_errors = set(old["errors"]), set(new["errors"])
	if old_errors:
		return new_errors < old_errors
	return (not new_errors and set(new["failed"]) < set(old["failed"])
		and new["tests"] >= old["tests"] and new["ignored"] <= old["ignored"])


def notes_do_not_regress(task, before, after):
	if not all(path.startswith("rea-analysis/") and Path(path).suffix == ".md" for path in task["owns"]):
		return False
	try:
		old = json.loads(before.splitlines()[0].removeprefix("CHECK_METRICS "))
		new = json.loads(after.splitlines()[0].removeprefix("CHECK_METRICS "))
	except (ValueError, IndexError):
		return False
	return (set(new["errors"]) <= set(old["errors"]) and set(new["failed"]) <= set(old["failed"])
		and new["tests"] >= old["tests"] and new["ignored"] <= old["ignored"])


def acceptance(core, project, commands, path):
	path.parent.mkdir(parents=True, exist_ok=True)
	for index, command in enumerate(commands):
		log = path.with_name(path.stem + "-" + str(index) + ".log")
		code = core.run_accept(command, project, log)
		if code:
			text = log.read_text(encoding="utf-8", errors="replace")
			return False, "CHECK_METRICS " + json.dumps(failure_metrics(text)) + "\n" + text[-6000:]
	return True, "Acceptance commands passed."


def save_patch(core, control, task, workspace, changed):
	chunks = []
	for rel in changed:
		before = workspace.backup / rel
		after = workspace.path / rel
		old = before.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if before.exists() else []
		new = after.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if after.exists() else []
		chunks.extend(difflib.unified_diff(old, new, fromfile="a/" + rel, tofile="b/" + rel))
	key = hashlib.sha256(task["id"].encode()).hexdigest()[:16]
	core.atomic_write(control / "patches" / (key + ".patch"), "".join(chunks))


def review_candidate(core, project, task, workspace, result, gateway, routes, board, slots, maintenance=None):
	changed, strays = workspace.changes()
	if strays or not changed:
		return "reject", "The candidate has stray edits or no owned changes."
	slot = slots["reviewer"]
	model = gemini_agent.available("reviewer") or (routes.model(role="reviewer", exclude=task.get("review_exclude", [])) if isinstance(routes, ProviderRoutes) else routes.model())
	if not model:
		return "retry", "The reviewer waits for a free model."
	diff = []
	for rel in changed:
		before, after = project / rel, workspace.path / rel
		old = before.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if before.exists() else []
		new = after.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if after.exists() else []
		diff.extend(difflib.unified_diff(old, new, fromfile="original/" + rel, tofile="candidate/" + rel))
	prompt = (MISSION + "\nReview this candidate independently. Read its source and original evidence. Do not edit or run a shell.\n"
		"Reject guessed behavior presented as recovered behavior, stubs, lost APIs, and weakened tests that hide failures.\n"
		"A test correction requires original evidence that proves the old assertion wrong. Do not approve a change merely because it passes tests.\n"
		"If original IL is needed, return status blocked with research_requests as method_il Type.Method or method_list Type.\n"
		"Original project: " + str(project) + "\nCandidate workspace: " + str(workspace.path) + "\nTask: " + task["title"] + "\n"
		"Worker result: " + json.dumps(result)[:6000] + "\nDiff preview (read complete files):\n" + "".join(diff)[:8000] + "\n"
		'End with OC_SWARM_JSON then {"status":"done","verdict":"accept|reject","summary":"evidence and reason"}.')
	for round_number in range(3):
		slot["item"] = "review-" + task["id"]
		env = agent_environment(core, slot, workspace.path, project, [], gateway)
		log = project / ".ocswarm" / "logs" / (slot["item"] + "-" + str(round_number) + ".log")
		core.start_agent(slot, core.find_opencode(), "", workspace.path, prompt, log, False, [], model=model, env=env)
		deadline = time.monotonic() + 7200
		while slot["proc"] and not (project / ".ocswarm" / "STOP").exists() and time.monotonic() < deadline:
			core.reap(slot)
			for name, other in slots.items():
				if name != "reviewer":
					core.reap(other)
			if maintenance:
				maintenance()
			board.set(phase="review", message="review " + task["id"], slots=core.public_slots(slots), requests_today=gateway.budget.data["requests"])
			time.sleep(core.POLL_SECONDS)
		if slot["proc"]:
			core.kill_tree(slot["proc"].pid)
			core.reap(slot)
			slot["fresh"] = False
			if model.startswith("gemini/"):
				gemini_agent.record(core, slot["log"], failed=True)
			return "retry", "The reviewer stopped or timed out."
		slot["fresh"] = False
		kind = core.log_kind(slot["log"])
		if kind in ("rate", "free", "context"):
			if kind == "context":
				task.setdefault("review_exclude", []).append(model)
				return "retry", "The review needs a larger-context model."
			if model.startswith("gemini/"):
				gemini_agent.record(core, slot["log"], failed=True)
			else:
				routes.reject(model, kind, log)
			return "retry", "The reviewer model is on a cooldown."
		review = slot.get("result") or {}
		if model.startswith("gemini/"):
			valid = slot.get("code") == 0 and review.get("status") in ("done", "blocked")
			gemini_agent.record(core, slot["log"], failed=not valid)
			if not valid:
				return "retry", "Gemini returned no valid review. The next review uses NVIDIA."
		requests = review.get("research_requests")
		if isinstance(requests, list) and requests:
			paths = collect_evidence(core, project, requests)
			if paths:
				prompt += "\nRead the requested original evidence:\n" + "\n".join(paths)
				continue
		verdict = review.get("verdict", "reject") if slot.get("code") == 0 and review.get("status") == "done" else "reject"
		if review.get("status") == "done" and slot.get("code") == 0 and model in routes.states:
			routes.success(model)
		summary = str(review.get("summary") or "The reviewer returned no valid verdict.")
		board.event("review", item=task["id"], verdict=verdict, model=model, summary=summary)
		return verdict, summary
	return "reject", "Original evidence did not resolve the review."


def run(launch, core):
	project = Path(launch["directory"]).resolve()
	control = project / ".ocswarm"
	control.mkdir(parents=True, exist_ok=True)
	stop = control / "STOP"
	pid_path = control / "pids.json"
	old = json.loads(pid_path.read_text()) if pid_path.exists() else {}
	if core.pid_alive(old.get("supervisor")) and old["supervisor"] != os.getpid():
		raise RuntimeError("The swarm already has a supervisor.")
	if stop.exists():
		return
	core.write_json(pid_path, {"supervisor": os.getpid(), "daemon": old.get("daemon", 0), "serve": 0})
	board = core.Board(project)
	slots = core.make_slots()
	slots["reviewer"] = {**core.make_slots()["worker-1"], "name": "reviewer", "role": "reviewer"}
	core.install_agents()
	exe = core.find_opencode()
	state_path = control / "state-free.json"
	state = json.loads(state_path.read_text()) if state_path.exists() else {}
	tasks = state.get("tasks", [])
	history = state.get("history", [])
	pending_merge = state.get("pending_merge")
	if pending_merge:
		workspace_path = Path(pending_merge["workspace"]).resolve()
		if not workspace_path.is_relative_to((core.RUNTIME / "work").resolve()):
			raise ValueError("The recovery workspace is outside its root.")
		accepted, conflicts = Workspace.recover(project, workspace_path)
		if conflicts:
			raise RuntimeError("Merge recovery requires review: " + ", ".join(conflicts))
		if accepted:
			history.append(accepted)
			tasks = [task for task in tasks if task["id"] != pending_merge["id"]]
	for task in tasks:
		if task["state"] == "running":
			task["state"] = "pending"
			if task.get("candidate", {}).get("needs_work"):
				task["tries"] = max(0, task["tries"] - 1)
	started = time.monotonic()
	round_number = int(state.get("round", 0))
	last_plan = 0
	workspaces = {}
	workspace_root = core.RUNTIME / "work"
	run_root = workspace_root / str(os.getpid())
	plan_root = run_root / "planner"
	plan_root.mkdir(parents=True, exist_ok=True)
	commands = launch.get("accept") or [". ~/.etg-tools/etg-env.sh && cargo test --manifest-path etg-rs/Cargo.toml --no-fail-fast"]
	gateway = FreeGateway(core.RUNTIME / "provider-usage.json")
	routes = ProviderRoutes(core, gateway) if hasattr(gateway, "providers") else core.WorkerRoutes()
	router = ClefRouter(core, gateway) if hasattr(gateway, "providers") else None
	if router:
		slots["router"] = {**core.make_slots()["worker-1"], "name": "router", "role": "router", "model": "cloudflare/clef", "activity": "Select eligible tasks after ownership checks."}
	last_recovery = time.monotonic()
	baseline = "The baseline has not run."
	phase = "baseline"
	def maintain():
		nonlocal routes, last_recovery, last_plan, round_number
		if stop.exists():
			return
		for name, role in core.SLOTS:
			if role != "worker":
				continue
			slot = slots[name]
			core.reap(slot)
			task = slot.get("task")
			if not slot["fresh"] or not task:
				continue
			result = slot.get("result")
			kind = core.log_kind(slot["log"])
			if kind in ("rate", "free", "context") and not isinstance(result, dict):
				if kind == "context":
					task.setdefault("excluded_models", []).append(slot["model"])
				else:
					routes.reject(slot["model"], kind, slot["log"])
				task["state"] = "pending"
				task["tries"] -= 1
				slot.update(fresh=False, task=None, activity="model rejected the request")
				board.event("model-rejected", slot=name, model=slot["model"], reason=kind)
			elif slot.get("code") == 0 and isinstance(result, dict):
				if not task.get("api_ready"):
					book = workspaces.get(name)
					if result.get("status") == "done" and book:
						task["candidate"] = {"workspace": str(book.path), "result": result, "model": slot["model"], "log": str(slot["log"])}
					routes.success(slot["model"])
					task["api_ready"] = True
		planner = slots["orchestrator"]
		core.reap(planner)
		if planner["fresh"]:
			planner["fresh"] = False
			kind = core.log_kind(planner["log"])
			result = planner.get("result")
			if kind in ("rate", "free") and not isinstance(result, dict):
				routes.reject(planner["model"], kind, planner["log"])
				board.event("model-rejected", slot="orchestrator", model=planner["model"], reason=kind)
			else:
				fresh, errors = validate_tasks(core, (result or {}).get("tasks"), tasks, history)
				tasks.extend(fresh)
				planner["activity"] = str(len(fresh)) + " tasks planned"
				board.event("plan", tasks=[task["id"] for task in fresh], errors=errors)
				if fresh:
					routes.success(planner["model"])
				elif errors:
					planner["state"] = "fail"
		if routes.exhausted() and time.monotonic() - last_recovery >= RECOVERY_GAP:
			routes = ProviderRoutes(core, gateway) if hasattr(gateway, "providers") else core.WorkerRoutes()
			last_recovery = time.monotonic()
		model = routes.model(role="orchestrator") if isinstance(routes, ProviderRoutes) else routes.model()
		planner = slots["orchestrator"]
		if model and planner["proc"] is None and len(tasks) < core.MAX_LIVE * 2 and time.monotonic() - last_plan >= PLAN_GAP:
			round_number += 1
			planner["item"] = "plan-" + str(round_number)
			env = agent_environment(core, planner, plan_root, project, [], gateway)
			planner_model = model
			core.start_agent(planner, exe, "", plan_root, planner_prompt(project, launch["task"], history, tasks, round_number, baseline), control / "logs" / (planner["item"] + ".log"), False, [], model=planner_model, env=env)
			last_plan = time.monotonic()
			board.event("planner-start", model=planner_model, round=round_number)
		for name, role in core.SLOTS:
			if role != "worker":
				continue
			slot = slots[name]
			if slot["proc"] or slot.get("task"):
				continue
			worker_count = sum(1 for worker_name, worker_role in core.SLOTS if worker_role == "worker" and slots[worker_name]["proc"])
			eligible_tasks = tasks
			if isinstance(routes, ProviderRoutes):
				eligible_tasks = [task for task in tasks if task["state"] == "pending" and ((task.get("candidate") and not task["candidate"].get("needs_work")) or (worker_count < routes.limit and routes.model(slots=slots, exclude=task.get("excluded_models", []), advance=False)))]
			pending = router.choose(eligible_tasks, slots, board) if router else next((task for task in tasks if task["state"] == "pending"), None)
			if not pending:
				slot["state"] = "wait"
				continue
			model = routes.model(slots=slots, exclude=pending.get("excluded_models", [])) if isinstance(routes, ProviderRoutes) else routes.model()
			ready_candidate = pending.get("candidate") and not pending["candidate"].get("needs_work")
			if ready_candidate and not model:
				model = pending["candidate"]["model"]
			if not model or (worker_count >= routes.limit and not ready_candidate):
				continue
			saved = pending.get("candidate")
			workspace = Workspace(project, run_root / name, workspace_root, pending["owns"])
			if saved and Path(saved["workspace"]).resolve() == workspace.path and workspace.previous_path:
				saved["workspace"] = str(workspace.previous_path)
			workspaces[name] = workspace
			slot["task"] = pending
			slot["item"] = pending["id"]
			pending["state"] = "running"
			pending.pop("api_ready", None)
			pending["tries"] += 1
			pending["baseline_repair"] = baseline != "Acceptance commands passed."
			candidate = pending.pop("candidate", None)
			if candidate:
				try:
					workspace.restore_candidate(candidate["workspace"], workspace_root)
				except (OSError, ValueError) as exc:
					pending["last_error"] = str(exc)
				else:
					candidate["workspace"] = str(workspace.path)
					pending["candidate"] = candidate
					board.event("candidate-restored", item=pending["id"], workspace=str(workspace.path))
					if not candidate.get("needs_work"):
						slot.update(proc=None, fresh=True, code=0, result=candidate["result"], model=candidate["model"], log=Path(candidate["log"]), state="wait", started_at=time.time(), finished_at=time.time())
						pending["tries"] -= 1
						continue
			env = agent_environment(core, slot, workspace.path, project, pending["owns"], gateway)
			core.start_agent(slot, exe, "", workspace.path, worker_prompt(pending, project), control / "logs" / (pending["id"] + ".log"), False, pending["owns"], model=model, env=env)
			pending["candidate"] = {"workspace": str(workspace.path), "result": {"status": "blocked", "summary": "The previous session ended before its final result."}, "model": model, "log": str(slot["log"]), "needs_work": True}
			board.event("dispatch", item=pending["id"], slot=name, model=model, workspace=str(workspace.path))
		if router:
			slots["router"]["activity"] = router.last["reason"] + " " + router.last["choice"]
		board.set(task=launch["task"], done=sum(row["state"] == "done" for row in history), total=len(history) + len(tasks), baseline_ok=baseline == "Acceptance commands passed.", cost=0, slots=core.public_slots(slots), live_limit=routes.limit, worker_model=routes.selected, provider=gateway.metrics, providers=routes.snapshot(slots) if isinstance(routes, ProviderRoutes) else {}, router=router.last if router else {}, requests_today=gateway.budget.data["requests"])
		core.write_json(state_path, {"tasks": tasks, "history": history, "round": round_number})

	try:
		board.set(task=launch["task"], phase=phase, message="run baseline acceptance", slots=core.public_slots(slots), providers=routes.snapshot(slots) if isinstance(routes, ProviderRoutes) else {}, router=router.last if router else {}, live_limit=routes.limit, worker_model=routes.selected, cost=0)
		ok, baseline = acceptance(core, project, commands, control / "evidence" / "baseline")
		board.event("baseline", ok=ok)
		while not stop.exists():
			if launch.get("hours", 0) > 0 and time.monotonic() - started >= launch["hours"] * 3600:
				phase = "stopped"
				break
			for slot in slots.values():
				core.reap(slot)
				deadline = 7200 if slot["role"] == "orchestrator" else 10800
				if slot.get("proc") and slot.get("started_at") and time.time() - slot["started_at"] > deadline:
					core.kill_tree(slot["proc"].pid)
					core.reap(slot)
					slot["activity"] = "The session reached its time limit."
			maintain()
			for name, slot in slots.items():
				if not slot["fresh"]:
					continue
				slot["fresh"] = False
				kind = core.log_kind(slot["log"])
				result = slot.get("result")
				if kind in ("rate", "free", "context") and not isinstance(result, dict):
					if kind == "context" and slot.get("task"):
						slot["task"].setdefault("excluded_models", []).append(slot["model"])
					else:
						routes.reject(slot["model"], kind, slot["log"])
					slot["activity"] = "model rejected the request"
					board.event("model-rejected", slot=name, model=slot["model"], reason=kind)
					if slot.get("task"):
						slot["task"]["state"] = "pending"
						slot["task"]["tries"] -= 1
						slot["task"] = None
					continue
				task = slot.get("task")
				if not task:
					continue
				workspace = workspaces.pop(name)
				if isinstance(result, dict) and result.get("research_requests") and task.get("research_rounds", 0) < 3:
					requests = result["research_requests"]
					paths = collect_evidence(core, project, requests) if isinstance(requests, list) else []
					if paths:
						task["brief"] += "\nRead the requested original evidence:\n" + "\n".join(paths)
						task["state"] = "pending"
						task["tries"] -= 1
						task["research_rounds"] = task.get("research_rounds", 0) + 1
						board.event("native-evidence", item=task["id"], paths=paths)
						slot["task"] = None
						continue
				failure = ""
				changed = []
				if slot.get("code") != 0 or not isinstance(result, dict) or result.get("status") != "done":
					failure = (result or {}).get("summary") or "The worker did not return a done result."
					if isinstance(result, dict) and result.get("status") == "blocked":
						task["tries"] = core.MAX_ITEM_TRIES
				else:
					task["candidate"] = {"workspace": str(workspace.path), "result": result, "model": slot["model"], "log": str(slot["log"])}
					core.write_json(state_path, {"tasks": tasks, "history": history, "round": round_number})
					verdict, detail = review_candidate(core, project, task, workspace, result, gateway, routes, board, slots, maintenance=maintain)
					if verdict == "retry":
						task["state"] = "pending"
						task["tries"] -= 1
						task["last_error"] = detail
						slot["task"] = None
						continue
					if verdict != "accept":
						failure = "Review rejected: " + detail
					try:
						if failure:
							raise ValueError(failure)
						core.write_json(state_path, {"tasks": tasks, "history": history, "round": round_number, "pending_merge": {"id": task["id"], "workspace": str(workspace.path)}})
						changed = workspace.merge()
						board.set(phase="validate", message="test " + task["id"], slots=core.public_slots(slots))
						before_check = baseline
						ok, baseline = acceptance(core, project, commands, control / "evidence" / task["id"])
						baseline_improved = task.get("baseline_repair", False) and improvement(before_check, baseline)
						notes_unchanged = notes_do_not_regress(task, before_check, baseline)
						if not ok and not baseline_improved and not notes_unchanged:
							conflicts = workspace.rollback()
							failure = "Acceptance failed: " + baseline[-3000:]
							if conflicts:
								raise RuntimeError("Rollback requires review: " + ", ".join(conflicts))
						else:
							save_patch(core, control, task, workspace, changed)
							workspace.commit({"id": task["id"], "title": task["title"], "state": "done", "owns": task["owns"], "summary": result.get("summary", ""), "evidence": result.get("evidence", []), "files": changed, "model": slot["model"], "time": core.now(), "tests_passed": ok})
							board.event("accepted", item=task["id"], model=slot["model"], files=changed, tests_passed=ok, baseline_improved=baseline_improved)
					except ValueError as exc:
						failure = str(exc)
					if failure and changed:
						ok, baseline = acceptance(core, project, commands, control / "evidence" / "after-rollback")
				if failure and task["tries"] < core.MAX_ITEM_TRIES:
					task.pop("candidate", None)
					task["state"] = "pending"
					task["last_error"] = failure
					slot["activity"] = "retry: " + failure[:100]
				else:
					task["state"] = "failed" if failure else "done"
					task["summary"] = failure or result.get("summary", "")
					history.append({"id": task["id"], "title": task["title"], "state": task["state"], "owns": task["owns"], "summary": task["summary"], "evidence": (result or {}).get("evidence", []), "files": changed, "model": slot["model"], "time": core.now(), "tests_passed": baseline == "Acceptance commands passed."})
					core.append_jsonl(control / "traces.jsonl", history[-1])
					slot["activity"] = task["summary"][:160]
					tasks.remove(task)
					slot["state"] = "fail" if failure else "wait"
				slot["task"] = None
			if stop.exists():
				core.write_json(state_path, {"tasks": tasks, "history": history, "round": round_number})
				break
			maintain()
			model = routes.model()
			phase = "work" if model else "cooldown"
			running = sum(1 for name, role in core.SLOTS if role == "worker" and slots[name]["proc"])
			message = str(running) + " workers running, " + str(len(tasks)) + " tasks active" if model else routes.message()
			board.set(task=launch["task"], phase=phase, message=message, done=sum(row["state"] == "done" for row in history), total=len(history) + len(tasks), slots=core.public_slots(slots), worker_model=routes.selected, live_limit=routes.limit, provider=gateway.metrics, providers=routes.snapshot(slots) if isinstance(routes, ProviderRoutes) else {}, router=router.last if router else {}, requests_today=gateway.budget.data["requests"], cost=0, baseline_ok=baseline == "Acceptance commands passed.")
			core.write_json(state_path, {"tasks": tasks, "history": history, "round": round_number})
			time.sleep(core.POLL_SECONDS)
		board.set(phase="stopped", message="stop requested", slots=core.public_slots(slots))
	except Exception as exc:
		board.set(phase="error", message=str(exc)[:240], slots=core.public_slots(slots))
		raise
	finally:
		for slot in slots.values():
			if slot.get("proc") and slot["proc"].poll() is None:
				core.kill_tree(slot["proc"].pid)
		gateway.close()
		old["supervisor"] = 0
		old["daemon"] = json.loads(pid_path.read_text()).get("daemon", 0) if pid_path.exists() else 0
		core.write_json(pid_path, old)


def require_stopped(control, core, allow_supervisor=False):
	path = control / "pids.json"
	pids = json.loads(path.read_text()) if path.exists() else {}
	for role in ("supervisor", "daemon"):
		if role == "supervisor" and allow_supervisor:
			continue
		pid = pids.get(role)
		if pid != os.getpid() and core.pid_alive(pid):
			raise RuntimeError("The swarm already has a " + role + " (PID " + str(pid) + ").")


def daemon(launch_path, core):
	launch = core.load_launch(launch_path)
	control = Path(launch["directory"]) / ".ocswarm"
	control.mkdir(parents=True, exist_ok=True)
	require_stopped(control, core, allow_supervisor=True)
	pid_path = control / "pids.json"
	pids = json.loads(pid_path.read_text()) if pid_path.exists() else {}
	existing = pids.get("supervisor") if core.pid_alive(pids.get("supervisor")) else 0
	class ExistingSupervisor:
		def __init__(self, pid):
			self.pid = pid
		def poll(self):
			if core.pid_alive(self.pid):
				return None
			path = control / "board.json"
			return 0 if path.exists() and json.loads(path.read_text()).get("phase") == "stopped" else 1
		def wait(self):
			while self.poll() is None:
				time.sleep(1)
			return self.poll()
	stop = control / "STOP"
	if stop.exists():
		stop.unlink()
	delay = 5
	while not stop.exists():
		core.write_json(control / "pids.json", {"supervisor": existing or 0, "daemon": os.getpid(), "serve": 0})
		with (control / "daemon.log").open("ab") as output:
			proc = ExistingSupervisor(existing) if existing else subprocess.Popen([sys.executable, str(core.ROOT / "ocswarm.py"), "supervise", "--launch", str(launch_path)], cwd=core.ROOT, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=core.NO_WINDOW)
			existing = 0
			started = time.monotonic()
			last_ui_check = 0
			while proc.poll() is None and not stop.exists():
				if time.monotonic() - last_ui_check >= 30:
					last_ui_check = time.monotonic()
					if not core.port_open(core.UI_PORT):
						try:
							core.open_dashboard(launch["directory"], show=False)
						except (Exception, SystemExit) as exc:
							output.write(("Dashboard restart failed: " + str(exc)[:200] + "\n").encode())
							output.flush()
				time.sleep(1)
			if stop.exists() and proc.poll() is None:
				core.kill_tree(proc.pid)
			code = proc.wait()
		if stop.exists() or (launch.get("hours", 0) > 0 and code == 0):
			break
		if time.monotonic() - started > 300:
			delay = 5
		with (control / "daemon.log").open("a", encoding="utf-8") as output:
			output.write(core.now() + " supervisor exited " + str(code) + "; retry in " + str(delay) + " seconds\n")
		deadline = time.monotonic() + delay
		while not stop.exists() and time.monotonic() < deadline:
			time.sleep(1)
		delay = min(300, delay * 2)


def check(core):
	root = core.RUNTIME / "rate-probe"
	root.mkdir(parents=True, exist_ok=True)
	gateway = FreeGateway(core.RUNTIME / "provider-usage.json")
	try:
		for model in core.WORKER_MODELS:
			slot = core.make_slots()["worker-1"]
			slot["name"] = "worker-health"
			slot["item"] = "health"
			env = agent_environment(core, slot, root, root, [], gateway)
			prompt = 'Do not use tools. Reply with OC_SWARM_JSON followed by {"status":"done","summary":"ping"}.'
			log = root / (model.replace("/", "-") + ".log")
			core.start_agent(slot, core.find_opencode(), "", root, prompt, log, False, [], model=model, env=env)
			deadline = time.monotonic() + 180
			while slot["proc"] and time.monotonic() < deadline:
				core.reap(slot)
				time.sleep(0.2)
			if slot["proc"]:
				core.kill_tree(slot["proc"].pid)
				print(model + ": timed out")
				continue
			result = slot.get("result") or {}
			if slot["code"] == 0 and result.get("status") == "done" and result.get("summary") == "ping":
				print(model + ": ready")
				return 0
			print(model + ": " + (core.log_kind(log) or "probe failed"))
		return 1
	finally:
		gateway.close()
