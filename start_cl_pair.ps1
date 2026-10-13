# start_cl_pair.ps1 — runs the two Chainlink fair-value tests, robustly:
#   sim_cl_late : skips the first minute of each 5-min window (the promising one)
#   sim_cl      : bets from the start of the window (comparison)
# - Output goes only to <tag>_bot.log, never the console, so clicking a
#   window can't freeze a bot.
# - If a bot stops for any reason, it restarts by itself after 10 seconds.
# Close a window to stop that bot. No real orders. No API key needed.

Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
python -m pip install --quiet ccxt pandas anthropic requests websocket-client

$variants = @(
    @{ tag = "sim_cl_late"; args = "--price-feed chainlink --window 20,240" },
    @{ tag = "sim_cl";      args = "--price-feed chainlink" }
)

foreach ($v in $variants) {
    $cmd = "`$host.UI.RawUI.WindowTitle = '$($v.tag) (auto-restart)'; " +
           "Set-Location '$PSScriptRoot'; `$env:PYTHONUTF8 = '1'; " +
           "while (`$true) { " +
           "  Write-Host (Get-Date -Format 'MM-dd HH:mm:ss') 'running $($v.tag) - progress is in $($v.tag)_bot.log'; " +
           "  cmd /c 'python fair_value_bot.py --tag $($v.tag) $($v.args) > nul 2>&1'; " +
           "  Write-Host (Get-Date -Format 'MM-dd HH:mm:ss') '$($v.tag) stopped (exit' `$LASTEXITCODE') - restarting in 10s'; " +
           "  Start-Sleep -Seconds 10 " +
           "}"
    Start-Process powershell -WindowStyle Minimized -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Started $($v.tag)"
}

Write-Host ""
Write-Host "Running (minimized). Keep this PC awake."
