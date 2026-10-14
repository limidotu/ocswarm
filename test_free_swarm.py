import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import free_swarm
import ocswarm


class FreeSwarmTests(unittest.TestCase):
	def setUp(self):
		self.gemini_route = patch.object(free_swarm.gemini_agent, "available", return_value="")
		self.gemini_route.start()
		self.addCleanup(self.gemini_route.stop)

	def test_original_reader_accepts_a_compiler_generated_iterator_name(self):
		with tempfile.TemporaryDirectory() as tmp:
			project = Path(tmp)
			(project / "EtG_Data/Managed").mkdir(parents=True)
			(project / "EtG_Data/Managed/Assembly-CSharp.dll").write_bytes(b"fixture")
			with patch.object(free_swarm.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()) as call:
				paths = free_swarm.collect_evidence(ocswarm, project, [{"kind": "method_il", "name": "<HandleShatter>c__Iterator0.MoveNext"}, {"kind": "method_il", "name": "MirrorController.*"}, {"kind": "method_il", "name": "--help"}])
			self.assertEqual(len(paths), 2)
			self.assertEqual(call.call_args_list[0].args[0][-1], "<HandleShatter>c__Iterator0.MoveNext")
			self.assertIn("SHA256:", Path(paths[0]).read_text())

	def test_board_publish_recovers_from_a_brief_windows_read_lock(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "board.json"
			path.write_text('{"state":"old"}')
			replace = Path.replace
			calls = []
			def transient_lock(source, target):
				calls.append(source)
				if len(calls) <= 2:
					raise PermissionError("The dashboard holds a read handle.")
				return replace(source, target)
			with patch.object(Path, "replace", transient_lock), patch.object(ocswarm.time, "sleep"):
				ocswarm.write_json(path, {"state": "new"})
			self.assertEqual(json.loads(path.read_text()), {"state": "new"})
			self.assertFalse(list(Path(tmp).glob("*.tmp")))

	def test_process_probe_does_not_stop_a_live_child(self):
		child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
		try:
			self.assertTrue(ocswarm.pid_alive(child.pid))
			self.assertTrue(ocswarm.pid_alive(child.pid))
			self.assertIsNone(child.poll())
		finally:
			child.terminate()
			child.wait(timeout=10)
		self.assertFalse(ocswarm.pid_alive(child.pid))

	def test_second_daemon_cannot_replace_a_live_supervisor_record(self):
		with tempfile.TemporaryDirectory() as tmp:
			control = Path(tmp)
			path = control / "pids.json"
			path.write_text(json.dumps({"supervisor": 321, "daemon": 654}))
			before = path.read_bytes()
			with patch.object(ocswarm, "pid_alive", side_effect=lambda pid: pid == 321):
				with self.assertRaises(RuntimeError):
					free_swarm.require_stopped(control, ocswarm)
				free_swarm.require_stopped(control, ocswarm, allow_supervisor=True)
			with patch.object(ocswarm, "pid_alive", return_value=True):
				with self.assertRaises(RuntimeError):
					free_swarm.require_stopped(control, ocswarm, allow_supervisor=True)
			self.assertEqual(path.read_bytes(), before)

	def scenario(self, root, reject_validation=False, reject_review=False, stop_during_review=False, refill_during_review=False, multiprovider=False):
		project = root / "project"
		(project / "etg-rs").mkdir(parents=True)
		(project / "etg-rs/README.md").write_text("Fixture source instructions.")
		(project / "etg-rs/crates/etg-core/tests").mkdir(parents=True)
		(project / "etg-rs/crates/etg-core/tests/pits.rs").write_text("Fixture pit tests.")
		(project / "etg-rs" / "a.rs").write_text("old a")
		(project / "etg-rs" / "b.rs").write_text("old b")
		for name in ["c", "d"]:
			(project / "etg-rs" / (name + ".rs")).write_text("old " + name)
		clock = [1000.0]
		worker_launches = []
		worker_models = []
		refilled = [False]
		class Process:
			pid = 123
			def poll(self):
				return 0
		class Gateway:
			url = "http://127.0.0.1:1/v1"
			metrics = {"requests": 0, "cost": 0}
			budget = type("Budget", (), {"data": {"requests": 0}})()
			def __init__(self, path):
				if multiprovider:
					self.providers = {"nvidia": True, "groq": True, "cloudflare": True}
			def status(self):
				return {p: {"ready": True} for p in self.providers}
			def extra_request(self, provider, model, body):
				choice = next(iter(body["questions"]["task"]["criteria"]))
				return 200, {"answers": {"task": {"choice": choice, "confidence": 1}}}, 0
			def close(self):
				pass
		def start(slot, exe, url, cwd, prompt, log, snap, allow, model=None, env=None):
			if slot["role"] == "orchestrator":
				self.assertTrue((Path(cwd) / "etg-rs/README.md").is_file(), "Planner README path is missing")
				self.assertTrue((Path(cwd) / "etg-rs/crates/etg-core/tests/pits.rs").is_file(), "Planner pit test path is missing")
				config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
				self.assertFalse(any(rule["action"] == "edit" and rule["effect"] == "allow" for rule in config["agents"]["build"]["permissions"]))
				names = ["c", "d"] if refill_during_review and slot["item"] != "plan-1" else ["a", "b"]
				if multiprovider:
					names = ["a", "b", "c"]
				result = {"status": "done", "tasks": [
					{"id": ("p2." if name in ["c", "d"] else "p1.") + name, "title": name, "brief": "repair " + name, "owns": ["etg-rs/" + name + ".rs"]} for name in names
				]}
			else:
				worker_launches.append(slot["item"])
				worker_models.append(model)
				for rel in allow:
					(Path(cwd) / rel).write_text("new " + Path(rel).stem)
				result = {"status": "done", "summary": "changed", "evidence": ["fixture"]}
			log.parent.mkdir(parents=True, exist_ok=True)
			log.write_text("")
			slot.update(proc=Process(), state="run", model=model, log=log, pending_result=result)
		def reap(slot):
			if slot.get("proc"):
				slot.update(proc=None, fresh=True, state="wait", result=slot["pending_result"], code=0)
		def sleep(seconds):
			clock[0] += seconds
			state_path = project / ".ocswarm/state-free.json"
			if state_path.exists():
				state = json.loads(state_path.read_text())
				if len(state.get("history", [])) >= 2:
					(project / ".ocswarm/STOP").write_text("stop")
			if clock[0] > (1300 if refill_during_review else 1030):
				raise AssertionError("The scenario did not complete.")
		def review(*args, **kwargs):
			if refill_during_review and not refilled[0]:
				refilled[0] = True
				clock[0] += free_swarm.PLAN_GAP + 1
				kwargs["maintenance"]()
				kwargs["maintenance"]()
				self.assertIn("p2.c", worker_launches)
				self.assertIn("p2.d", worker_launches)
				self.assertEqual((project / "etg-rs/a.rs").read_text(), "old a")
			if stop_during_review:
				(project / ".ocswarm/STOP").write_text("stop")
				return "retry", "The run stopped."
			return "reject" if reject_review else "accept", "The fixture evidence check."
		with ExitStack() as stack:
			for name, value in {"RUNTIME": root / "runtime", "install_agents": lambda: None, "find_opencode": lambda: "unused", "start_agent": start, "reap": reap, "freebuff_request": lambda *args: self.fail("Freebuff must not run")}.items():
				stack.enter_context(patch.object(ocswarm, name, value))
			stack.enter_context(patch.object(free_swarm, "FreeGateway", Gateway))
			stack.enter_context(patch.object(free_swarm, "review_candidate", side_effect=review))
			stack.enter_context(patch.object(free_swarm, "acceptance", return_value=(not reject_validation, "Acceptance commands passed." if not reject_validation else "test failed")))
			stack.enter_context(patch.object(ocswarm.time, "monotonic", side_effect=lambda: clock[0]))
			stack.enter_context(patch.object(ocswarm.time, "time", side_effect=lambda: clock[0]))
			stack.enter_context(patch.object(ocswarm.time, "sleep", side_effect=sleep))
			free_swarm.run({"directory": str(project), "task": "test", "hours": 0}, ocswarm)
		state = json.loads((project / ".ocswarm/state-free.json").read_text())
		state["worker_launches"] = worker_launches
		state["worker_models"] = worker_models
		return project, state

	def test_three_providers_dispatch_and_merge_only_reviewed_candidates(self):
		with tempfile.TemporaryDirectory() as tmp:
			project, state = self.scenario(Path(tmp), multiprovider=True)
			self.assertEqual({m.split("/")[0] for m in state["worker_models"]}, {"nvidia", "groq", "cloudflare"})
			self.assertTrue(all(row["state"] == "done" for row in state["history"]))
			self.assertEqual(len(state["history"]), 3)
			board = json.loads((project / ".ocswarm/board.json").read_text())
			self.assertEqual(set(board["providers"]), {"nvidia", "groq", "cloudflare"})
			self.assertGreater(board["router"]["decisions"], 0)

	def test_stop_during_review_preserves_candidates_without_dispatching_again(self):
		with tempfile.TemporaryDirectory() as tmp:
			project, state = self.scenario(Path(tmp), stop_during_review=True)
			self.assertEqual(state["worker_launches"], ["p1.a", "p1.b"])
			self.assertEqual((project / "etg-rs/a.rs").read_text(), "old a")
			self.assertEqual((project / "etg-rs/b.rs").read_text(), "old b")
			self.assertTrue(all(task.get("candidate") for task in state["tasks"]))

	def test_planner_refills_and_starts_more_workers_during_a_review(self):
		with tempfile.TemporaryDirectory() as tmp:
			project, state = self.scenario(Path(tmp), refill_during_review=True)
			self.assertEqual(state["worker_launches"], ["p1.a", "p1.b", "p2.c", "p2.d"])

	def test_free_planner_workers_and_validated_merges_complete(self):
		with tempfile.TemporaryDirectory() as tmp:
			project, state = self.scenario(Path(tmp))
			self.assertEqual((project / "etg-rs/a.rs").read_text(), "new a")
			self.assertEqual((project / "etg-rs/b.rs").read_text(), "new b")
			self.assertTrue(all(row["state"] == "done" for row in state["history"]))
			self.assertEqual(len(list((project / ".ocswarm/patches").glob("*.patch"))), 2)

	def test_failed_validation_restores_only_the_candidate_files(self):
		with tempfile.TemporaryDirectory() as tmp:
			project, state = self.scenario(Path(tmp), reject_validation=True)
			self.assertEqual((project / "etg-rs/a.rs").read_text(), "old a")
			self.assertEqual((project / "etg-rs/b.rs").read_text(), "old b")
			self.assertTrue(all(row["state"] == "failed" for row in state["history"]))

	def test_review_rejection_never_merges_the_candidate(self):
		with tempfile.TemporaryDirectory() as tmp:
			project, state = self.scenario(Path(tmp), reject_review=True)
			self.assertEqual((project / "etg-rs/a.rs").read_text(), "old a")
			self.assertEqual((project / "etg-rs/b.rs").read_text(), "old b")
			self.assertTrue(all(row["state"] == "failed" for row in state["history"]))
			self.assertTrue(all("Review rejected" in row["summary"] for row in state["history"]))

	def test_reviewer_reads_requested_original_evidence_before_accepting(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			project = root / "project"
			(project / "etg-rs").mkdir(parents=True)
			(project / "etg-rs/a.rs").write_text("before")
			workspace = free_swarm.Workspace(project, root / "work/one", root / "work", ["etg-rs/a.rs"])
			(workspace.path / "etg-rs/a.rs").write_text("after")
			slots = ocswarm.make_slots()
			slots["reviewer"] = {**ocswarm.make_slots()["worker-1"], "name": "reviewer", "role": "reviewer"}
			prompts = []
			results = [{"status": "blocked", "research_requests": [{"kind": "method_il", "name": "MirrorController.Interact"}]}, {"status": "done", "verdict": "accept", "summary": "Original IL supports the change."}]
			def start(slot, exe, url, cwd, prompt, log, snap, allow, **kwargs):
				self.assertFalse(allow)
				self.assertEqual(Path(cwd), workspace.path)
				prompts.append(prompt)
				log.parent.mkdir(parents=True, exist_ok=True)
				log.write_text("")
				slot.update(proc=object(), model=kwargs["model"], log=log)
			def reap(slot):
				if not slot.get("proc"):
					return
				slot.update(proc=None, code=0, result=results.pop(0), fresh=True)
			gateway = type("Gateway", (), {"url": "http://127.0.0.1:1/v1", "budget": type("Budget", (), {"data": {"requests": 0}})()})()
			with patch.object(ocswarm, "RUNTIME", root / "runtime"), patch.object(ocswarm, "start_agent", side_effect=start), patch.object(ocswarm, "find_opencode", return_value="unused"), patch.object(ocswarm, "reap", side_effect=reap), patch.object(free_swarm, "collect_evidence", return_value=["native-evidence.txt"]) as reader, patch.object(free_swarm.time, "sleep"):
				verdict, summary = free_swarm.review_candidate(ocswarm, project, {"id": "p1.a", "title": "a"}, workspace, {"status": "done"}, gateway, ocswarm.WorkerRoutes(), ocswarm.Board(project), slots)
			self.assertEqual(verdict, "accept")
			self.assertEqual(len(prompts), 2)
			self.assertIn("native-evidence.txt", prompts[-1])
			reader.assert_called_once()
			self.assertEqual((project / "etg-rs/a.rs").read_text(), "before")

	def test_planner_cannot_assign_original_assets_or_corpus(self):
		rows = [{"id": "bad", "owns": ["etg-rs/crates/etg-sim/tests/corpus/run.txt"]}, {"id": "bad2", "owns": ["rea-analysis/etg-content.json"]}]
		tasks, errors = free_swarm.validate_tasks(ocswarm, rows, [], [])
		self.assertFalse(tasks)
		self.assertEqual(len(errors), 2)

	def test_gemini_quota_failure_keeps_nvidia_worker_capacity(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			project = root / "project"
			(project / "etg-rs").mkdir(parents=True)
			(project / "etg-rs/a.rs").write_text("before")
			workspace = free_swarm.Workspace(project, root / "work/one", root / "work", ["etg-rs/a.rs"])
			(workspace.path / "etg-rs/a.rs").write_text("after")
			slots = ocswarm.make_slots()
			slots["reviewer"] = {**slots["worker-1"], "name": "reviewer", "role": "reviewer"}
			routes = ocswarm.WorkerRoutes()
			limit = routes.limit
			def start(slot, exe, url, cwd, prompt, log, snap, allow, **kwargs):
				self.assertEqual(kwargs["model"], free_swarm.gemini_agent.MODEL)
				log.parent.mkdir(parents=True, exist_ok=True)
				log.write_text('{"type":"error","error":{"status":429,"message":"weekly quota exhausted"}}')
				slot.update(proc=object(), log=log)
			def reap(slot):
				if slot.get("proc"):
					slot.update(proc=None, result=None, code=1, fresh=True)
			gateway = type("Gateway", (), {"url": "http://127.0.0.1:1/v1", "budget": type("Budget", (), {"data": {"requests": 0}})()})()
			with patch.object(ocswarm, "RUNTIME", root / "runtime"), patch.object(ocswarm, "start_agent", side_effect=start), patch.object(ocswarm, "find_opencode", return_value="unused"), patch.object(ocswarm, "reap", side_effect=reap), patch.object(free_swarm.gemini_agent, "available", return_value=free_swarm.gemini_agent.MODEL), patch.object(free_swarm.gemini_agent, "record") as record, patch.object(free_swarm.time, "sleep"):
				verdict, _ = free_swarm.review_candidate(ocswarm, project, {"id": "p1.a", "title": "a"}, workspace, {"status": "done"}, gateway, routes, ocswarm.Board(project), slots)
				self.assertEqual(verdict, "retry")
				record.assert_called_once_with(ocswarm, slots["reviewer"]["log"], failed=True)
			self.assertEqual(routes.limit, limit)
			self.assertTrue(all(state["hits"] == 0 for state in routes.states.values()))
			self.assertEqual((project / "etg-rs/a.rs").read_text(), "before")

	def test_baseline_repair_must_remove_failures_without_skipping_tests(self):
		def report(failed, tests=10, ignored=0):
			return "CHECK_METRICS " + json.dumps({"errors": [], "failed": failed, "tests": tests, "ignored": ignored})
		self.assertTrue(free_swarm.improvement(report(["a", "b"]), report(["b"])))
		self.assertFalse(free_swarm.improvement(report(["a", "b"]), report(["c"])))
		self.assertFalse(free_swarm.improvement(report(["a", "b"]), report(["b"], tests=9)))
		self.assertFalse(free_swarm.improvement(report(["a", "b"]), report(["b"], ignored=1)))
		notes = {"owns": ["rea-analysis/new-evidence.md"]}
		self.assertTrue(free_swarm.notes_do_not_regress(notes, report(["a"]), report(["a"])))
		self.assertFalse(free_swarm.notes_do_not_regress(notes, report(["a"]), report(["a", "b"])))
		self.assertFalse(free_swarm.notes_do_not_regress({"owns": ["etg-rs/src/lib.rs"]}, report(["a"]), report(["a"])))


if __name__ == "__main__":
	unittest.main()
