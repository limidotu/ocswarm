import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import ocswarm


def quota_log(path, headers=None):
	path.write_text(json.dumps({
		"type": "error",
		"error": {
			"type": "provider.quota", "status": 429,
			"message": "Rate limit exceeded. Please try again later.",
			"response": {
				"headers": headers or {},
				"body": json.dumps({"error": {"type": "FreeUsageLimitError"}}),
			},
		},
	}) + "\n", encoding="utf-8")


class RateLimitTests(unittest.TestCase):
	def test_rejected_route_is_not_dispatched_in_the_same_tick(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			log = root / "worker.log"
			quota_log(log)
			task = {"id": "t1", "role": "worker", "state": "running", "tries": 1,
				"owns": ["a.txt"], "title": "a", "brief": "b"}
			slots = ocswarm.make_slots()
			slot = slots["worker-1"]
			slot.update(item="t1", task=task, log=log, result=None, state="wait")
			board = ocswarm.Board(root)
			flag = ocswarm.handle_item_result(slot, {"items": [task]}, board, root, [])
			routes = ocswarm.WorkerRoutes(["opencode/test-free"])
			routes.reject("opencode/test-free", flag, log)
			ocswarm.fill_workers(slots, [])
			self.assertIsNone(routes.model())
			self.assertEqual(task["tries"], 0)

	def test_text_about_429_is_not_a_provider_error(self):
		with tempfile.TemporaryDirectory() as tmp:
			log = Path(tmp) / "worker.log"
			log.write_text(json.dumps({"type": "text", "part": {
				"text": "The code handles 429 and FreeUsageLimitError."
			}}) + "\n", encoding="utf-8")
			self.assertEqual(ocswarm.log_kind(log), "")

	def test_retry_after_and_one_penalty_for_concurrent_errors(self):
		with tempfile.TemporaryDirectory() as tmp, patch.object(ocswarm.time, "monotonic", return_value=100):
			log = Path(tmp) / "worker.log"
			quota_log(log, {"Retry-After": "600"})
			routes = ocswarm.WorkerRoutes(["opencode/test-free"])
			for _ in range(19):
				routes.reject("opencode/test-free", "rate", log)
			self.assertEqual(routes.states["opencode/test-free"]["hits"], 1)
			self.assertEqual(routes.states["opencode/test-free"]["until"], 700)
			self.assertIsNone(routes.model())
			with patch.object(ocswarm.time, "monotonic", return_value=700):
				self.assertEqual(routes.model(), "opencode/test-free")

	def test_rejected_model_uses_only_configured_fallback(self):
		with tempfile.TemporaryDirectory() as tmp:
			log = Path(tmp) / "worker.log"
			quota_log(log)
			routes = ocswarm.WorkerRoutes(["opencode/first-free", "opencode/second-free"])
			routes.reject("opencode/first-free", "rate", log)
			self.assertEqual(routes.model(), "opencode/second-free")
			self.assertEqual(routes.limit, max(1, ocswarm.INITIAL_LIVE // 2))
			with patch.object(ocswarm.time, "monotonic", return_value=routes.states["opencode/first-free"]["until"]):
				self.assertEqual(routes.model(), "opencode/first-free")

	def test_supervisor_fails_over_and_stops_after_bounded_rejections(self):
		with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
			root = Path(tmp)
			control = root / ".ocswarm"
			control.mkdir()
			(control / "assignments.json").write_text(json.dumps({"tasks": [
				{"id": "t1", "owns": ["a.txt"], "title": "a", "brief": "b"},
				{"id": "t2", "owns": ["b.txt"], "title": "b", "brief": "b"},
			]}), encoding="utf-8")
			clock = [1000.0]
			calls = []
			class FakeProcess:
				pid = 123
				def poll(self):
					return 1
			def start(slot, exe, url, cwd, prompt, log_path, snap, allow, model=None):
				calls.append((clock[0], model))
				log_path.parent.mkdir(parents=True, exist_ok=True)
				quota_log(log_path)
				slot.update(proc=FakeProcess(), state="run", log=log_path, model=model)
			def reap(slot):
				if slot.get("proc"):
					slot.update(proc=None, state="wait", fresh=True, result=None, code=1)
			def sleep(seconds):
				clock[0] += seconds
				if clock[0] > 1300:
					raise AssertionError("The supervisor did not stop after quota exhaustion.")
			cwd = Path.cwd()
			stack.callback(os.chdir, cwd)
			for name, value in {
				"WORKER_MODELS": ["opencode/first-free", "opencode/second-free"],
				"MAX_RATE_HITS": 2, "RATE_BACKOFF_SECONDS": 5,
				"install_agents": lambda: None, "find_opencode": lambda: "unused",
				"ensure_freebuff": lambda _: (123, "unused"),
				"open_orchestrator": lambda *args: "thread", "freebuff_request": lambda *args: {},
				"take_snap": lambda *args: None, "restore_strays": lambda *args: [],
				"start_agent": start, "reap": reap,
			}.items():
				stack.enter_context(patch.object(ocswarm, name, value))
			stack.enter_context(patch.object(ocswarm.time, "monotonic", side_effect=lambda: clock[0]))
			stack.enter_context(patch.object(ocswarm.time, "time", side_effect=lambda: clock[0]))
			stack.enter_context(patch.object(ocswarm.time, "sleep", side_effect=sleep))
			ocswarm.supervise_legacy({"directory": str(root), "task": "test", "hours": 1})
			result = json.loads((control / "result.json").read_text())
			self.assertEqual(result["phase"], "blocked")
			self.assertFalse(result["ok"])
			self.assertLessEqual(len(calls), 2 * ocswarm.INITIAL_LIVE * 2)
			for model in ["opencode/first-free", "opencode/second-free"]:
				stamps = sorted({stamp for stamp, route in calls if route == model})
				self.assertEqual(len(stamps), 2)
				self.assertGreaterEqual(stamps[1] - stamps[0], 5)


if __name__ == "__main__":
	unittest.main()
