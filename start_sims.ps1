# start_sims.ps1 — launches the 4 simulation variants, each in its own window.
# Right-click this file > "Run with PowerShell", or in PowerShell:  .\start_sims.ps1
# No real orders are ever placed in --sim mode.
# Child windows inherit the API key from this window's environment.

Set-Location -Path $PSScriptRoot

# Use UTF-8 so log lines with symbols (──, →, emoji) don't crash on Windows' default encoding.
$env:PYTHONUTF8 = "1"

if (-not $env:ANTHROPIC_API_KEY) {
    $env:ANTHROPIC_API_KEY = Read-Host "Paste your Anthropic API key (used for the Claude veto checks)"
}

Write-Host "Installing/updating dependencies..."
python -m pip install --quiet ccxt pandas anthropic requests

$variants = @(
    @{ tag = "sim_live";  args = "" },
    @{ tag = "sim_sym";   args = "--slip-down 0.02" },
    @{ tag = "sim_clob";  args = "--price-source clob" },
    @{ tag = "sim_floor"; args = "--floor 0.30" }
)

foreach ($v in $variants) {
    $cmd = "`$host.UI.RawUI.WindowTitle = '$($v.tag)'; " +
           "Set-Location '$PSScriptRoot'; " +
           "python crypto_bot_market.py --sim --tag $($v.tag) $($v.args)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Started $($v.tag)"
}

Write-Host ""
Write-Host "All 4 running. Keep this PC awake (Settings > System > Power > Sleep: Never)."
Write-Host "Compare results any time with:  python sim_report.py"
