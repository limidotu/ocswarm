import argparse
import json
import re
import sys
from pathlib import Path

from nvidia_auth import _crypt


ROOT = Path.home() / ".local/share/ocswarm"
PROVIDERS = {"cloudflare", "groq", "mistral", "gemini-api"}


def path_for(provider):
	if provider not in PROVIDERS:
		raise ValueError("Unknown provider.")
	return ROOT / (provider + "-key.dpapi")


def save(provider, value):
	if not isinstance(value, dict) or not value.get("key"):
		raise ValueError("The provider key is missing.")
	path = path_for(provider)
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_suffix(".tmp")
	tmp.write_bytes(_crypt(json.dumps(value).encode(), True))
	tmp.replace(path)


def load(provider):
	path = path_for(provider)
	if not path.is_file():
		return {}
	return json.loads(_crypt(path.read_bytes(), False))


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("provider", choices=sorted(PROVIDERS))
	parser.add_argument("--account", default="")
	args = parser.parse_args()
	value = sys.stdin.read().strip()
	if not re.fullmatch(r"[A-Za-z0-9_\-]{20,}", value):
		raise ValueError("The clipboard does not contain a valid key.")
	save(args.provider, {"key": value, "account_id": args.account})
	print(args.provider + " key saved with Windows encryption.")


if __name__ == "__main__":
	main()
