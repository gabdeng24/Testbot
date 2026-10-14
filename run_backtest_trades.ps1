# run_backtest_trades.ps1 - downloads every real trade in the past markets found
# by RUN_BACKTEST, then runs the trade-based backtest. Public data only.
# Safe to close and re-run: downloading resumes where it stopped.
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$host.UI.RawUI.WindowTitle = "Polybot trade backtest"
python -m pip install --quiet requests pandas numpy

Write-Host "Step 1/3: quick check on 3 markets..."
cmd /c "python backtest_trades_collect.py --probe > nul 2>&1"
Get-Content backtest_trades.log -Tail 3
if (-not (Get-Content backtest_trades.log -Tail 3 | Select-String "trades=[1-9]")) {
    Write-Host "The quick check found no trades. Tell Claude what this window shows."
    exit
}

Write-Host ""
Write-Host "Step 2/3: downloading trades (about an hour). Progress is also in backtest_trades.log"
while ($true) {
    $p = Start-Process python -ArgumentList "backtest_trades_collect.py --max 2000" -NoNewWindow -PassThru `
         -RedirectStandardOutput "backtest_trades_stdout.txt" -RedirectStandardError "backtest_trades_stderr.txt"
    $null = $p.Handle   # Windows only reports the exit code if the handle is read early
    while (-not $p.HasExited) {
        Start-Sleep -Seconds 30
        Write-Host (Get-Content backtest_trades.log -Tail 1 -ErrorAction SilentlyContinue)
    }
    $p.WaitForExit()
    if ($p.ExitCode -eq 0) { break }
    if ($p.ExitCode -eq 2) { Write-Host "Stopped: no trades came back. Tell Claude."; exit }
    Write-Host "Downloader stopped (exit $($p.ExitCode)) - resuming in 10s"
    Start-Sleep -Seconds 10
}

Write-Host ""
Write-Host "Step 3/3: analysing..."
cmd /c "python backtest_trades_analyze.py > backtest_trades_report.txt 2>&1"
Write-Host "Done. Results are in backtest_trades_report.txt - tell Claude."
