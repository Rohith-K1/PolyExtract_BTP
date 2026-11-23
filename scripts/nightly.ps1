# Nightly incremental extraction (run by the Windows scheduled task "polyextract-nightly").
# Reads at most $Limit NEW papers with the LLM, then applies the verification policy. Safe to run any time:
# finished papers are skipped, LLM answers are cached, and a usage/billing limit stops the run cleanly.
# Remove the schedule:  Unregister-ScheduledTask -TaskName polyextract-nightly -Confirm:$false
param([int]$Limit = 60)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$log = Join-Path $root "data/runs/nightly_$(Get-Date -Format yyyyMMdd_HHmm).log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

$busy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*polyextract.cli extract*" }
if ($busy) {
    "skipped: another extraction is still running (pid $($busy.ProcessId -join ','))" | Out-File $log -Encoding utf8
    exit 0
}

"start $(Get-Date -Format s) limit=$Limit" | Out-File $log -Encoding utf8
& "$root/.venv/Scripts/python.exe" -m polyextract.cli extract --include-review --llm --llm-todo --limit $Limit 2>&1 |
    ForEach-Object { "$_" } | Out-File -Append $log -Encoding utf8
"=== extract exit $LASTEXITCODE at $(Get-Date -Format s) ===" | Out-File -Append $log -Encoding utf8
