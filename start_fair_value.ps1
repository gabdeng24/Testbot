# start_fair_value.ps1 — independent fair-value strategy (no crowd-following).
# No real orders are ever placed. No API key needed (no Claude veto).
#   sim_fv   : 5-min markets
#   sim_fv15 : 15-min markets

Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"

python -m pip install --quiet ccxt pandas anthropic requests

$variants = @(
    @{ tag = "sim_fv";   args = "" },
    @{ tag = "sim_fv15"; args = "--interval 15" }
)

foreach ($v in $variants) {
    $cmd = "`$host.UI.RawUI.WindowTitle = '$($v.tag)'; " +
           "Set-Location '$PSScriptRoot'; " +
           "python fair_value_bot.py --tag $($v.tag) $($v.args)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Started $($v.tag)"
}

Write-Host ""
Write-Host "Running. Keep this PC awake. Compare with:  python sim_report.py sim_fv sim_fv15 sim_ctrl"
