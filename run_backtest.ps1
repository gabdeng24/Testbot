# run_backtest.ps1 - downloads ~8 months of Polymarket 5-minute market history
# (public data only, no orders, no keys), then analyses it.
# Safe to close and re-run: downloading resumes where it stopped.
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$host.UI.RawUI.WindowTitle = "Polybot backtest"
python -m pip install --quiet requests pandas numpy

Write-Host "Step 1/3: quick check on 4 markets..."
cmd /c "python backtest_collect.py --probe > nul 2>&1"
Get-Content backtest_collect.log -Tail 5
if ((Get-Content backtest_collect.log -Tail 1) -match "worked: none") {
    Write-Host "The quick check found no markets. Tell Claude what this window shows."
    exit
}

Write-Host ""
Write-Host "Step 2/3: downloading (about an hour). Progress is also in backtest_collect.log"
while ($true) {
    $p = Start-Process python -ArgumentList "backtest_collect.py --n 3000" -NoNewWindow -PassThru `
         -RedirectStandardOutput "backtest_stdout.txt" -RedirectStandardError "backtest_stderr.txt"
    $null = $p.Handle   # Windows only reports the exit code if the handle is read early
    while (-not $p.HasExited) {
        Start-Sleep -Seconds 30
        $last = Get-Content backtest_collect.log -Tail 1 -ErrorAction SilentlyContinue
        Write-Host $last
    }
    $p.WaitForExit()
    if ($p.ExitCode -eq 0) { break }
    if ($p.ExitCode -eq 2) { Write-Host "Stopped: no usable data came back. Tell Claude."; exit }
    Write-Host "Downloader stopped (exit $($p.ExitCode)) - resuming in 10s"
    Start-Sleep -Seconds 10
}

Write-Host ""
Write-Host "Step 3/3: analysing..."
cmd /c "python backtest_analyze.py backtest_data.jsonl > backtest_report.txt 2>&1"
Write-Host "Done. Results are in backtest_report.txt and backtest_results.json - tell Claude."
