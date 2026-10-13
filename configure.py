"""Save provider credentials outside the repository."""
import argparse
import getpass
from nvidia_auth import save_key
from secret_store import save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=["nvidia", "groq", "cloudflare"])
    parser.add_argument("--account", help="Cloudflare account ID")
    args = parser.parse_args()
    account = args.account
    if args.provider == "cloudflare" and not account:
        account = input("Cloudflare account ID: ").strip()
    key = getpass.getpass("API key (hidden): ").strip()
    if not key:
        parser.error("The API key is empty.")
    if args.provider == "nvidia":
        save_key(key)
    else:
        save(args.provider, {"key": key, "account_id": account or ""})
    print("The credential is saved with Windows encryption outside the repository.")


if __name__ == "__main__":
    main()
