# start_chainlink.ps1 — fair-value strategy using Polymarket's live Chainlink feed
# (the price these markets settle on). No real orders. No API key needed.
#   sim_cl      : 5-min markets, bets 20-280s before close (same as sim_fv, new prices)
#   sim_cl_late : 5-min markets, skips the first minute (bets 20-240s before close)
#   sim_cl15    : 15-min markets

Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"

python -m pip install --quiet ccxt pandas anthropic requests websocket-client

$variants = @(
    @{ tag = "sim_cl";      args = "--price-feed chainlink" },
    @{ tag = "sim_cl_late"; args = "--price-feed chainlink --window 20,240" },
    @{ tag = "sim_cl15";    args = "--price-feed chainlink --interval 15" }
)

foreach ($v in $variants) {
    $cmd = "`$host.UI.RawUI.WindowTitle = '$($v.tag)'; " +
           "Set-Location '$PSScriptRoot'; " +
           "python fair_value_bot.py --tag $($v.tag) $($v.args)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Started $($v.tag)"
}

Write-Host ""
Write-Host "Running. Keep this PC awake. Compare with:  python sim_report.py sim_cl sim_cl_late sim_cl15 sim_fv sim_fv15"
