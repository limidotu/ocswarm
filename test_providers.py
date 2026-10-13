import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bounded_agent
import clef_router
import free_gateway
import ocswarm
import provider_budget
from provider_routes import ProviderRoutes


class BudgetTests(unittest.TestCase):
	def test_atomic_reservations_share_daily_and_minute_allowances(self):
		with tempfile.TemporaryDirectory() as tmp, patch.object(provider_budget.time, "time", return_value=1791928000):
			a = provider_budget.ProviderBudget(Path(tmp) / "budget.json")
			b = provider_budget.ProviderBudget(a.path)
			ticket, retry, _ = a.reserve("groq", 6000)
			self.assertIsNotNone(ticket)
			self.assertEqual(retry, 0)
			self.assertIsNone(b.reserve("groq", 2000)[0])
			a.settle(ticket, 1000)
			self.assertIsNotNone(b.reserve("groq", 6000)[0])
			self.assertEqual(a.status("groq")["used"], 7000)
			self.assertIsNotNone(b.reserve("cloudflare", 300)[0])

	def test_failed_or_abandoned_calls_keep_reserved_units_after_restart(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "budget.json"
			budget = provider_budget.ProviderBudget(path)
			budget.reserve("cloudflare", 5500)
			self.assertIsNone(provider_budget.ProviderBudget(path).reserve("cloudflare", 600)[0])
			self.assertEqual(budget.status("cloudflare")["used"], 5500)

	def test_coding_limit_preserves_shared_allowance_for_router(self):
		with tempfile.TemporaryDirectory() as tmp:
			budget = provider_budget.ProviderBudget(Path(tmp) / "budget.json")
			budget.reserve("cloudflare", 5900)
			self.assertIsNone(budget.reserve("cloudflare", 200)[0])
			self.assertIsNotNone(budget.reserve("cloudflare", 100, "router")[0])
			self.assertEqual(budget.status("cloudflare")["buckets"]["router"], 100)

	def test_one_provider_cooldown_does_not_block_another(self):
		with tempfile.TemporaryDirectory() as tmp:
			budget = provider_budget.ProviderBudget(Path(tmp) / "budget.json")
			budget.cooldown("groq", 600, "quota")
			self.assertFalse(budget.status("groq")["ready"])
			self.assertTrue(budget.status("cloudflare")["ready"])

	def test_midnight_rollover_does_not_charge_old_completion_to_new_day(self):
		with tempfile.TemporaryDirectory() as tmp:
			budget = provider_budget.ProviderBudget(Path(tmp) / "budget.json")
			with patch.object(provider_budget.time, "time", return_value=1791928000):
				ticket, _, _ = budget.reserve("groq", 6000)
			with patch.object(provider_budget.time, "time", return_value=1792014400):
				budget.settle(ticket, 4000)
				self.assertEqual(budget.status("groq")["used"], 0)

	def test_oversized_request_rejects_before_reservation(self):
		with tempfile.TemporaryDirectory() as tmp:
			budget = provider_budget.ProviderBudget(Path(tmp) / "budget.json")
			self.assertEqual(budget.reserve("groq", 7601)[1], -1)
			self.assertEqual(budget.status("groq")["requests_today"], 0)


class RoutesTests(unittest.TestCase):
	def gateway(self):
		return type("Gateway", (), {"providers": {"nvidia": True, "groq": True, "cloudflare": True}, "status": lambda self: {p: {"ready": True} for p in self.providers}})()

	def test_provider_worker_caps_are_independent_and_review_excludes_glm(self):
		routes = ProviderRoutes(ocswarm, self.gateway())
		slots = {str(i): {"role": "worker", "proc": object(), "model": "nvidia/kimi-k3"} for i in range(6)}
		self.assertTrue(routes.model(slots=slots).startswith("groq/"))
		self.assertEqual(routes.model(role="reviewer"), "groq/gpt-oss-120b")
		self.assertTrue(routes.model(role="reviewer", exclude=["groq/gpt-oss-120b"]).startswith("nvidia/"))

	def test_rate_error_reduces_only_affected_provider_capacity(self):
		with tempfile.TemporaryDirectory() as tmp:
			log = Path(tmp) / "error.log"
			log.write_text(json.dumps({"type": "error", "error": {"status": 429, "response": {"headers": {"Retry-After": "600"}}}}))
			routes = ProviderRoutes(ocswarm, self.gateway())
			limits = routes.limits.copy()
			for _ in range(12):
				routes.reject("nvidia/kimi-k3", "rate", log)
			self.assertEqual(routes.limits["nvidia"], 3)
			self.assertEqual(routes.limits["groq"], limits["groq"])
			self.assertEqual(routes.limits["cloudflare"], limits["cloudflare"])
			self.assertEqual(routes.states["nvidia/kimi-k3"]["hits"], 1)

	def test_successful_api_calls_grow_capacity_before_a_long_task_finishes(self):
		gateway = self.gateway()
		gateway.status = lambda: {"nvidia": {"ready": True, "successes": {"moonshotai/kimi-k3": 8}}, "groq": {"ready": True}, "cloudflare": {"ready": True}}
		routes = ProviderRoutes(ocswarm, gateway)
		routes.status()
		self.assertEqual(routes.limits["nvidia"], 8)
		routes.cached_at = 0
		routes.status()
		self.assertEqual(routes.limits["nvidia"], 8)

	def test_local_quota_wait_does_not_reduce_cap(self):
		with tempfile.TemporaryDirectory() as tmp:
			log = Path(tmp) / "error.log"
			log.write_text(json.dumps({"type": "error", "error": {"status": 429, "type": "LocalRateLimitError"}}))
			routes = ProviderRoutes(ocswarm, self.gateway())
			before = routes.limits.copy()
			routes.reject("groq/gpt-oss-120b", "rate", log)
			self.assertEqual(routes.limits, before)


class CompactToolsTests(unittest.TestCase):
	def test_tools_deny_global_edits_unowned_files_and_path_escape(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp); workspace, project = root / "work", root / "original"
			for p in [workspace, project]:
				(p / "etg-rs").mkdir(parents=True)
				(p / "etg-rs/a.rs").write_text("old")
			files = bounded_agent.FileTools({"workspace": str(workspace), "project": str(project), "owns": ["etg-rs/a.rs"]})
			for path in [str(project / "etg-rs/a.rs"), "etg-rs/b.rs", "../outside.rs", "etg-rs/.env"]:
				with self.assertRaises(ValueError):
					files.execute("write_file", {"path": path, "content": "new"})
			with self.assertRaises(ValueError):
				files.execute("bash", {"command": "anything"})
			files.execute("replace_text", {"path": "etg-rs/a.rs", "old": "old", "new": "new"})
			self.assertEqual((workspace / "etg-rs/a.rs").read_text(), "new")
			self.assertEqual((project / "etg-rs/a.rs").read_text(), "old")

	def test_partial_reads_expose_incomplete_flag_and_next_range(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp); (root / "rea-analysis").mkdir(); (root / "rea-analysis/a.txt").write_text("a\nb\nc\n")
			files = bounded_agent.FileTools({"workspace": str(root), "project": str(root), "owns": []})
			result = files.execute("read_file", {"path": "rea-analysis/a.txt", "line_count": 2})
			self.assertTrue(result["incomplete"])
			self.assertEqual(result["next_line"], 3)

	def test_worker_executes_tool_round_trip_and_keeps_original_source(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp); (root / "etg-rs").mkdir(); (root / "etg-rs/a.rs").write_text("old")
			config = {"gateway": "http://127.0.0.1:1/v1", "workspace": str(root), "project": str(root), "owns": ["etg-rs/a.rs"], "checkpoint": str(root / "session.json")}
			responses = [
				{"choices": [{"message": {"tool_calls": [{"id": "1", "function": {"name": "replace_text", "arguments": json.dumps({"path": "etg-rs/a.rs", "old": "old", "new": "new"})}}]}}]},
				{"choices": [{"finish_reason": "stop", "message": {"content": '{"status":"done","summary":"test"}'}}]},
			]
			with patch.object(bounded_agent.urllib.request, "urlopen", side_effect=[io.BytesIO(json.dumps(r).encode()) for r in responses]), contextlib.redirect_stdout(io.StringIO()) as output:
				self.assertEqual(bounded_agent.run(config, "groq/gpt-oss-120b", "test"), 0)
			self.assertEqual((root / "etg-rs/a.rs").read_text(), "new")
			self.assertIn("OC_SWARM_JSON", output.getvalue())
			self.assertTrue(json.loads((root / "session.json").read_text())["done"])

	def test_unapplied_done_claim_forces_an_edit_before_completion(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp); (root / "etg-rs").mkdir(); (root / "etg-rs/a.rs").write_text("old")
			config = {"gateway": "http://127.0.0.1:1/v1", "workspace": str(root), "project": str(root), "owns": ["etg-rs/a.rs"], "checkpoint": str(root / "session.json")}
			done = {"choices": [{"finish_reason": "stop", "message": {"content": '{"status":"done","summary":"changed"}'}}]}
			edit = {"choices": [{"message": {"tool_calls": [{"id": "1", "function": {"name": "write_file", "arguments": json.dumps({"path": "etg-rs/a.rs", "content": "new"})}}]}}]}
			with patch.object(bounded_agent.urllib.request, "urlopen", side_effect=[io.BytesIO(json.dumps(r).encode()) for r in [done, edit, done]]) as request, contextlib.redirect_stdout(io.StringIO()):
				self.assertEqual(bounded_agent.run(config, "groq/gpt-oss-120b", "test"), 0)
			body = json.loads(request.call_args_list[1].args[0].data)
			self.assertEqual(body["tool_choice"], "required")
			self.assertEqual({t["function"]["name"] for t in body["tools"]}, {"write_file", "replace_text"})
			self.assertEqual((root / "etg-rs/a.rs").read_text(), "new")


class ClefTests(unittest.TestCase):
	def test_locked_actions_never_reach_clef_and_invalid_choice_falls_back(self):
		calls = []
		class Gateway:
			providers = {"cloudflare": True}
			def extra_request(self, provider, model, body):
				calls.append(body)
				return 200, {"answers": {"task": {"choice": "locked"}}}, 0
		with tempfile.TemporaryDirectory() as tmp:
			router = clef_router.ClefRouter(ocswarm, Gateway())
			tasks = [{"id": name, "state": "pending", "title": name, "brief": name, "owns": ["etg-rs/" + name + ".rs"]} for name in ["locked", "a", "b"]]
			slots = {"one": {"task": {"owns": ["etg-rs/locked.rs"]}}}
			selected = router.choose(tasks, slots, ocswarm.Board(Path(tmp)))
			self.assertEqual(selected["id"], "a")
			self.assertNotIn("locked", calls[0]["questions"]["task"]["criteria"])
			self.assertEqual(router.last["fallbacks"], 1)


class ExtraGatewayTests(unittest.TestCase):
	def test_paid_models_reject_and_clef_shares_cloudflare_budget(self):
		with tempfile.TemporaryDirectory() as tmp, patch.object(free_gateway, "load_key", return_value="dummy-key"), patch("secret_store.load", return_value={"key": "dummy-extra-key", "account_id": "test-account"}):
			gateway = free_gateway.FreeGateway(Path(tmp) / "usage.json", interval=0)
			try:
				with patch.object(free_gateway.urllib.request, "urlopen") as network:
					self.assertEqual(gateway.extra_request("cloudflare", "@cf/zai-org/glm-5.3", {})[0], 400)
					network.assert_not_called()
				response = io.BytesIO(json.dumps({"success": True, "result": {"answers": {"task": {"choice": "a"}}, "usage": {"input_tokens": 100, "output_tokens": 0}}}).encode())
				response.status = 200
				with patch.object(free_gateway.urllib.request, "urlopen", return_value=response):
					self.assertEqual(gateway.extra_request("cloudflare", free_gateway.CLEF_MODEL, {"state": "test", "questions": {}})[0], 200)
				self.assertAlmostEqual(gateway.quotas.status("cloudflare")["used"], 2.18, places=2)
				self.assertEqual(gateway.quotas.status("groq")["used"], 0)
			finally:
				gateway.close()


if __name__ == "__main__":
	unittest.main()
