import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import endpoint_probe
import secret_store


class Response(io.BytesIO):
	status = 200
	headers = {"X-RateLimit-Limit-Requests": "1000", "Authorization": "unrelated-private-header"}


class EndpointProbeTests(unittest.TestCase):
	def test_success_body_redacts_key_and_omits_private_headers(self):
		key = "dummy-secret-key-for-test-only"
		response = Response(json.dumps({"echo": key}).encode())
		with patch.object(endpoint_probe, "load", return_value={"key": key}), patch.object(endpoint_probe.urllib.request, "urlopen", return_value=response):
			result = endpoint_probe.request("groq", "openai/gpt-oss-120b")
		self.assertNotIn(key, json.dumps(result))
		self.assertEqual(result["body"]["echo"], "[REDACTED]")
		self.assertEqual(result["headers"], {"X-RateLimit-Limit-Requests": "1000"})

	def test_error_body_redacts_key_and_preserves_retry_after(self):
		key = "dummy-secret-key-for-test-only"
		error = urllib.error.HTTPError("https://example.invalid", 429, "quota", {"Retry-After": "600"}, io.BytesIO(("quota " + key).encode()))
		with patch.object(endpoint_probe, "load", return_value={"key": key}), patch.object(endpoint_probe.urllib.request, "urlopen", side_effect=error):
			result = endpoint_probe.request("groq", "openai/gpt-oss-120b")
		self.assertNotIn(key, json.dumps(result))
		self.assertEqual(result["status"], 429)
		self.assertEqual(result["headers"], {"Retry-After": "600"})

	def test_missing_key_makes_no_network_request(self):
		with patch.object(endpoint_probe, "load", return_value={}), patch.object(endpoint_probe.urllib.request, "urlopen") as network:
			self.assertEqual(endpoint_probe.request("groq", "openai/gpt-oss-120b")["status"], "missing_key")
			network.assert_not_called()

	def test_dpapi_round_trip_and_unknown_provider_rejection(self):
		with tempfile.TemporaryDirectory() as tmp, patch.object(secret_store, "ROOT", Path(tmp)):
			value = {"key": "dummy-secret-key-for-test-only", "account_id": "test-account"}
			secret_store.save("groq", value)
			self.assertEqual(secret_store.load("groq"), value)
			self.assertNotIn(value["key"].encode(), secret_store.path_for("groq").read_bytes())
			with self.assertRaises(ValueError):
				secret_store.save("../outside", value)


if __name__ == "__main__":
	unittest.main()
