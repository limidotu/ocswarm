# Install Ocswarm with an AI agent

## Scope

Ocswarm supports Windows 10 and 11. Linux and macOS are not tested installation targets.
The production task rules target an Enter the Gungeon reconstruction with `etg-rs/` and `rea-analysis/`.
This repository supplies the swarm, not game assets, a decompiler, or the reconstruction itself.

## 1. Check the host

Run `python --version`, `node --version`, and `git --version`.
Python must be 3.12 or later. Node.js must be 22 or later.
If a tool is missing, install it through the host's package manager or its official installer.
Open a new PowerShell session after PATH changes.

## 2. Install

```powershell
git clone https://github.com/limidotu/ocswarm.git
Set-Location ocswarm
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The installer uses `npm ci`, a local Python environment, and `@opencode/cli@2.0.27`.
It builds the dashboard from repository source. It does not start a swarm or send model requests.
Use `-SkipOpenCode` only when the tested CLI is already installed.
Do not replace it with a different package that has a similar name.

Use `.\.venv\Scripts\python.exe` for all subsequent Python commands.
No Python environment activation is necessary.

## 3. Configure free credentials

```powershell
.\.venv\Scripts\python.exe configure.py nvidia
.\.venv\Scripts\python.exe configure.py groq
.\.venv\Scripts\python.exe configure.py cloudflare
```

NVIDIA is required. The other providers are optional.
Use the hidden key prompt. Do not put keys in command arguments, chat, source, logs, or Git.
Cloudflare also needs the account ID. Scope the token to that account with Workers AI read and edit access.
Do not add payment methods, purchase credits, upgrade accounts, or bypass provider limits.

Credentials use Windows Data Protection API encryption under `%USERPROFILE%\.local\share\ocswarm`.
Another Windows account cannot use those encrypted files. Save its own credentials through the prompt.

## 4. Check the installation

```powershell
.\.venv\Scripts\python.exe doctor.py
.\.venv\Scripts\python.exe -m unittest discover
.\.venv\Scripts\python.exe ocswarm.py selftest
npm run typecheck
npm test
```

After the user supplies a key, run `.\.venv\Scripts\python.exe ocswarm.py check`.
This check sends a live NVIDIA request. A 429 means the provider must wait for quota or capacity.
Do not create duplicate accounts or spoof clients to avoid restrictions.

## 5. Prepare the target

Ask the user for the reconstruction folder and its acceptance command.
The folder must contain:

```text
EnterTheGungeon/
  etg-rs/                       Rust workspace and tests
  rea-analysis/                 Original evidence and research
    read-method-il.mjs          Trusted original-method reader
  EtG_Data/Managed/
    Assembly-CSharp.dll         Original game assembly
  AGENTS.md                     Project rules, when available
```

The original assembly, extracted tables, and recorded replay fixtures must already exist where the reader expects them.
Make sure the reader works before the swarm starts. It must supply original method evidence without editing game files.
The supervisor calls `node rea-analysis/read-method-il.mjs --dll EtG_Data/Managed/Assembly-CSharp.dll METHOD`.
For method lists, it adds `--list TYPE` instead of `METHOD`.
The reader must print the method evidence to standard output. The supervisor records the assembly hash and exit status.

Run the acceptance command manually from the target folder.
The supervisor uses its Bash runner for acceptance commands. Install Git for Windows with Bash on PATH.
If the target needs WSL or a custom tool environment, include that environment setup in the acceptance command.
Do not copy another machine's shell setup path without checking that it exists.

## Start after the user requests it

Use the start and stop examples in README.md with the user's folder and acceptance command.
Open `http://127.0.0.1:8124/`. Check that the board shows the correct target folder.
The baseline can take several minutes. Workers start after the supervisor records it.

`--hours 0` has no timed stop. The host must stay awake and connected.
After a reboot, start the swarm again. Free daily quotas can pause providers until their reset.

## Common fixes

| Symptom | Action |
| --- | --- |
| PowerShell blocks the installer | Use the process command shown above. No permanent policy change is necessary. |
| OpenCode is missing | Run `npm install --global @opencode/cli@2.0.27`, then open a new shell. |
| Dashboard dependencies are missing | Run `npm ci` and `npm run build`. |
| A provider returns 429 | Read the quota panel and wait for the cooldown or daily reset. |
| Baseline fails | Read target logs. Supply the correct acceptance environment or let the planner repair existing test failures. |
| A second run is refused | Inspect `status`. Stop the existing run before a replacement. |

Keep credentials, target state, and runtime files out of Git. Do not delete a checkpoint to hide a failed check.
