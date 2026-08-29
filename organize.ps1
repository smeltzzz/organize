<#
.SYNOPSIS
    Organize — Jellyfin Media Management Launcher for Windows PowerShell

.DESCRIPTION
    Launches the Organize toolkit on Windows 10/11 using py -3 or python.exe.

.EXAMPLE
    .\organize.ps1 doctor
    .\organize.ps1 run --dry-run
    .\organize.ps1 run
    .\organize.ps1 audit
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Detect Python 3.11+ launcher
$PythonCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $verCheck = py -3 -c "import sys; print(1 if sys.version_info >= (3, 11) else 0)" 2>$null
    if ($verCheck -eq "1") {
        $PythonCmd = @("py", "-3")
    }
}

if (-not $PythonCmd -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $verCheck = python -c "import sys; print(1 if sys.version_info >= (3, 11) else 0)" 2>$null
    if ($verCheck -eq "1") {
        $PythonCmd = @("python")
    }
}

if (-not $PythonCmd) {
    Write-Error @"
Python 3.11+ is required but was not found.
Install Python 3.11+ from https://www.python.org/downloads/
Be sure to check 'Add python.exe to PATH' during installation.
"@
    exit 1
}

$TargetScript = Join-Path $ScriptDir "organize.py"
if ($PythonCmd.Count -gt 1) {
    & $PythonCmd[0] $PythonCmd[1] $TargetScript @Arguments
} else {
    & $PythonCmd[0] $TargetScript @Arguments
}
exit $LASTEXITCODE
