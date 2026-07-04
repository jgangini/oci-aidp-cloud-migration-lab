[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (Test-Path ".\graphify-out") {
        graphify update .
    }
    if (Test-Path ".\.sentrux\rules.toml") {
        sentrux check .
        sentrux gate .
    }
    Write-Host "Architecture postflight complete."
}
finally {
    Pop-Location
}
