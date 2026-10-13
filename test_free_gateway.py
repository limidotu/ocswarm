import datetime
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import free_gateway


class FreeGatewayTests(unittest.TestCase):
	def test_two_instances_share_request_spacing_and_daily_count(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "usage.json"
			clock = [1000.0]
			with patch.object(free_gateway.time, "time", side_effect=lambda: clock[0]), patch.object(free_gateway.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
				a = free_gateway.RequestBudget(path, interval=12)
				b = free_gateway.RequestBudget(path, interval=12)
				self.assertEqual(a.reserve(), 0)
				self.assertEqual(b.reserve(), 0)
				self.assertEqual(a.reserve(), 0)
				self.assertEqual(clock[0], 1024)
				self.assertEqual(json.loads(path.read_text())["requests"], 3)

	def test_daily_budget_rejects_without_another_upstream_request(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "usage.json"
			day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
			path.write_text(json.dumps({"day": day, "requests": 8000}))
			budget = free_gateway.RequestBudget(path)
			self.assertGreater(budget.reserve(), 0)
			self.assertEqual(json.loads(path.read_text())["requests"], 8000)

	def test_gateway_rejects_an_unlisted_model_before_network_access(self):
		with tempfile.TemporaryDirectory() as tmp, patch.object(free_gateway, "load_key", return_value="nvapi-dummy-key-for-test-only"):
			gateway = free_gateway.FreeGateway(Path(tmp) / "usage.json", interval=0)
			try:
				request = urllib.request.Request(gateway.url + "/chat/completions", data=json.dumps({"model": "paid-model"}).encode(), headers={"Content-Type": "application/json"})
				with self.assertRaises(urllib.error.HTTPError) as error:
					urllib.request.urlopen(request, timeout=5)
				self.assertEqual(error.exception.code, 400)
				error.exception.close()
				self.assertEqual(gateway.metrics["requests"], 0)
			finally:
				gateway.close()


if __name__ == "__main__":
	unittest.main()
