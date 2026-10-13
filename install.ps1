param([switch]$SkipOpenCode)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE." }
}

foreach ($program in @('python', 'node', 'npm')) {
    if (-not (Get-Command $program -ErrorAction SilentlyContinue)) {
        throw "Install $program and add it to PATH. Then run install.ps1 again."
    }
}
Invoke-Checked 'python' @('scripts/check-runtime.py')
Invoke-Checked 'node' @('scripts/check-runtime.mjs')
Invoke-Checked 'python' @('-m', 'venv', '.venv')
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Invoke-Checked $pythonExe @('-m', 'pip', 'install', '-r', 'requirements.txt')
Invoke-Checked 'npm' @('ci')
if (-not $SkipOpenCode) {
    Invoke-Checked 'npm' @('install', '--global', '@opencode/cli@2.0.27')
}
Invoke-Checked 'npm' @('run', 'build')
Invoke-Checked $pythonExe @('doctor.py')
