"""Check local installation without sending model requests."""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    checks = {
        "Windows": os.name == "nt",
        "Python 3.12+": sys.version_info >= (3, 12),
        "Node.js on PATH": bool(shutil.which("node")),
        "OpenCode": bool(shutil.which("opencode")) or (Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@opencode/cli/bin/opencode.exe").is_file(),
        "Dashboard bundle": (ROOT / "runtime/dashboard-server.mjs").is_file(),
        "Graph asset": (ROOT / "node_modules/vis-network/standalone/umd/vis-network.min.js").is_file(),
    }
    try:
        import tiktoken
        tiktoken.get_encoding("o200k_harmony")
        checks["Token counter"] = True
    except ImportError:
        checks["Token counter"] = False
    for label, passed in checks.items():
        print(("OK   " if passed else "FAIL ") + label)
    from nvidia_auth import load_key
    print("NVIDIA credential: " + ("saved" if load_key() else "missing (run python configure.py nvidia)"))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
