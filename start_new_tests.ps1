# start_new_tests.ps1 — earlier-entry and 15-minute-market tests, plus a control.
# No real orders are ever placed in --sim mode.
#   sim_ctrl  : unchanged 5-min strategy (bets 30-240s before close) — the baseline
#   sim_early : 5-min markets, but only bets in the first 2 minutes (180-285s left)
#   sim_15m   : bets on 15-min markets (120-840s left), 5-min crowd as confirmation

Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"

if (-not $env:ANTHROPIC_API_KEY) {
    $env:ANTHROPIC_API_KEY = Read-Host "Paste your Anthropic API key (used for the Claude veto checks)"
}

python -m pip install --quiet ccxt pandas anthropic requests

$variants = @(
    @{ tag = "sim_ctrl";  args = "" },
    @{ tag = "sim_early"; args = "--window 180,285" },
    @{ tag = "sim_15m";   args = "--interval 15" }
)

foreach ($v in $variants) {
    $cmd = "`$host.UI.RawUI.WindowTitle = '$($v.tag)'; " +
           "Set-Location '$PSScriptRoot'; " +
           "python crypto_bot_market.py --sim --tag $($v.tag) $($v.args)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Started $($v.tag)"
}

Write-Host ""
Write-Host "Running. Keep this PC awake. Compare with:  python sim_report.py sim_ctrl sim_early sim_15m"
