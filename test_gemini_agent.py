import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gemini_agent
import ocswarm


class GeminiAgentTests(unittest.TestCase):
	def test_terminal_response_preserves_a_marker_split_across_deltas(self):
		answer = 'OC_SWARM_JSON\n{"status":"done","verdict":"reject","summary":"Original IL adds curse."}'
		rows = []
		for fragment in [answer[:8], answer[8:20], answer[20:]]:
			rows.extend(gemini_agent.normalized({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": fragment}}))
		rows.extend(gemini_agent.normalized({"event": "result", "result": {"status": "SUCCESS", "response": answer, "usage": {"thinking_tokens": 20}}}))
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "review.log"
			path.write_text("\n".join(json.dumps(row) for row in rows))
			self.assertEqual(ocswarm.extract(path)["verdict"], "reject")
			self.assertEqual(rows[-1]["cost"], 0)

	def test_empty_success_from_denied_evidence_is_an_error(self):
		rows = gemini_agent.normalized({"event": "result", "result": {"status": "SUCCESS", "response": "", "denied_actions": [{"action": "read_file"}]}})
		self.assertEqual(rows[0]["type"], "error")
		self.assertEqual(rows[0]["error"]["status"], 503)

	def test_quota_errors_enter_existing_rate_detection(self):
		rows = gemini_agent.normalized({"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED: weekly quota exhausted"}})
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "quota.log"
			path.write_text(json.dumps(rows[0]))
			self.assertEqual(ocswarm.log_kind(path), "rate")

	def test_credit_fallback_or_missing_denies_disable_gemini(self):
		settings = {"useG1Credits": False, "permissions": {"deny": ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]}}
		self.assertTrue(gemini_agent.safe_settings(settings))
		settings.pop("useG1Credits")
		self.assertTrue(gemini_agent.safe_settings(settings))
		settings["useG1Credits"] = True
		self.assertFalse(gemini_agent.safe_settings(settings))
		settings["useG1Credits"] = False
		settings["permissions"]["deny"].remove("write_file(*)")
		self.assertFalse(gemini_agent.safe_settings(settings))

	def test_failed_quota_waits_for_the_longest_shared_reset(self):
		with tempfile.TemporaryDirectory() as tmp:
			config = Path(tmp) / "gemini.json"
			config.write_text('{"enabled":true}')
			usage = {"command": {"data": {"groups": [{"name": "Gemini Models", "buckets": [{"remaining_fraction": 0, "reset_time": "2030-01-01T01:00:00Z"}, {"remaining_fraction": 0, "reset_time": "2030-01-02T00:00:00Z"}]}]}}}
			result = type("Result", (), {"stdout": json.dumps(usage)})()
			with patch.object(gemini_agent, "CONFIG", config), patch.object(gemini_agent.subprocess, "run", return_value=result):
				gemini_agent.record(ocswarm, None, failed=True)
			state = json.loads(config.read_text())
			self.assertEqual(state["until"], 1893542430.0)
			self.assertEqual(state["cost"], 0)
			with patch.object(gemini_agent, "CONFIG", config):
				self.assertFalse(gemini_agent.available("reviewer"))


if __name__ == "__main__":
	unittest.main()
