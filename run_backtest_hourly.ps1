# run_backtest_hourly.ps1 - downloads past HOURLY crypto Up/Down markets (result,
# every trade, Binance candles), then runs the trade-based backtest. Public data only.
# Safe to close and re-run: downloading resumes where it stopped.
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$host.UI.RawUI.WindowTitle = "Polybot hourly backtest"
python -m pip install --quiet requests pandas numpy tzdata

Write-Host "Step 1/3: quick check on 3 hourly markets..."
cmd /c "python backtest_hourly_collect.py --probe > nul 2>&1"
Get-Content backtest_hourly.log -Tail 4
if ((Get-Content backtest_hourly.log -Tail 1) -match "worked: none") {
    Write-Host "The quick check found no hourly markets. Tell Claude what this window shows."
    exit
}

Write-Host ""
Write-Host "Step 2/3: downloading (1-3 hours). Progress is also in backtest_hourly.log"
while ($true) {
    $p = Start-Process python -ArgumentList "backtest_hourly_collect.py --n 1500" -NoNewWindow -PassThru `
         -RedirectStandardOutput "backtest_hourly_stdout.txt" -RedirectStandardError "backtest_hourly_stderr.txt"
    $null = $p.Handle   # Windows only reports the exit code if the handle is read early
    while (-not $p.HasExited) {
        Start-Sleep -Seconds 30
        Write-Host (Get-Content backtest_hourly.log -Tail 1 -ErrorAction SilentlyContinue)
    }
    $p.WaitForExit()
    if ($p.ExitCode -eq 0) { break }
    if ($p.ExitCode -eq 2) { Write-Host "Stopped: no usable data came back. Tell Claude."; exit }
    Write-Host "Downloader stopped (exit $($p.ExitCode)) - resuming in 10s"
    Start-Sleep -Seconds 10
}

Write-Host ""
Write-Host "Step 3/3: analysing..."
cmd /c "python backtest_hourly_analyze.py > backtest_hourly_report.txt 2>&1"
Write-Host "Done. Results are in backtest_hourly_report.txt - tell Claude."
