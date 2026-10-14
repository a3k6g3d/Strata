# run_ab_offline.ps1 - the cache-vs-drafts A/B, with the chat server out of the way.
#
# An arm has to load the model itself on an otherwise empty GPU, and the Strata serving this chat is holding most of
# the card.  So this script stops that server (and the engine and vision worker it spawned), runs the four arms, and
# starts the server again when it is done.  Double-click Run-AB.cmd, or run this file from a PowerShell window.

$repo = "J:\Strata-work\Strata-0140"
$cfg  = "J:/Strata-work/strata.json"
$py   = "C:\Users\a3k6g\AppData\Local\Programs\Python\Python311\python.exe"
$srv  = "C:\Program Files\Python311\python.exe"
$port = 8080
$log  = "J:\Strata-work\ab-offline.log"

function Say($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

Set-Location $repo
"=== A/B run started $(Get-Date) ===" | Out-File -FilePath $log -Encoding UTF8
Say "log: $log"

# 1. let the chat finish answering before anything goes away
Say "waiting 20 s so the answer in flight is delivered before the server stops"
Start-Sleep -Seconds 20

# 2. stop the server, the engine it spawned and the vision worker - all three hold VRAM.  Match on the config path so
#    only this install's processes are touched, and check both slash directions: the engine's own command line uses
#    forward slashes, which is how the first attempt at this stopped nothing at all.
$mine = 'serve\.server|strata(-vision)?\.exe'
$targets = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='strata.exe' OR Name='strata-vision.exe'" |
             Where-Object { $_.CommandLine -and ($_.CommandLine -match $mine -or $_.CommandLine -match 'Strata-work') } |
             Where-Object { $_.CommandLine -notmatch 'run_ab_offline' })
if (-not $targets) { Say "nothing matched - no Strata process found to stop" }
foreach ($p in $targets) {
    $brief = ($p.CommandLine -replace '\s+', ' ')
    Say ("stopping {0} (pid {1}): {2}" -f $p.Name, $p.ProcessId, $brief.Substring(0, [Math]::Min(100, $brief.Length)))
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 3. wait until the port really stops answering, so the arms are not started over a dying engine
Say "waiting for port $port to stop answering ..."
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $still = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
             Where-Object { $_.OwningProcess -ne 0 }
    if (-not $still) { break }
}
Say ("port {0} {1}" -f $port, $(if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -ne 0 }) { "STILL BUSY - the arms will be slow" } else { "is free" }))
Say "waiting 15 s more for the driver to give the VRAM back"
Start-Sleep -Seconds 15

# 4. the arms.  --yes because we freed the port ourselves; the check would otherwise read a race as a refusal.
Say "running the arms: as configured, expert-cache off, drafts off, both off (about an hour)"
& $py -u tools\strata_ab.py --config $cfg --port $port --yes 2>&1 | ForEach-Object {
    $t = "$_"
    Write-Host $t
    Add-Content -Path $log -Value $t -Encoding UTF8
}
$rc = $LASTEXITCODE
Say "the arms finished with exit $rc"

# 5. bring the chat back up, in its own window so it survives this script
Say "starting the server again"
Start-Process -FilePath $srv -ArgumentList "-u","-m","serve.server","--engine","strata","--config",$cfg,"--port",$port `
    -WorkingDirectory $repo -WindowStyle Minimized
Say "done - results in bench\results\ab-cache-vs-drafts, log at $log"
Write-Host ""
Write-Host "Finished.  Close this window, or read the log:"
Write-Host "  notepad $log"
Read-Host "Press Enter to close"
