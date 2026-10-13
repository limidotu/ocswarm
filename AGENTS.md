# Agent instructions

Read README.md and docs/INSTALL_AGENT.md before installation or operation.

- Use Windows, Python 3.12+, Node.js 22+, and the OpenCode version in install.ps1.
- Use the local Python environment. Run install.ps1 before the checks.
- Use free provider plans only. Do not purchase credits, enable billing, or bypass provider limits.
- Ask for missing credentials through configure.py's hidden prompt. Never print or commit secrets.
- Ask for the target folder and acceptance command. This repository does not contain the game or reconstruction.
- Start a continuous run only when the user requests it.
- Preserve private candidates, checkpoints, original binaries, tables, and replay fixtures.
- Keep all product names and UI labels consistent with Ocswarm.

Before publishing a code change, run Python tests, selftest, TypeScript checks, dashboard tests, and the dashboard build.
Use the exact commands in README.md. Report failures and material limits.
Model delegation is optional. Apply Luna-specific routing only when the user explicitly invokes `$lunacode` in the current chat.
