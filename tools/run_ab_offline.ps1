# run_ab_offline.ps1 - the cache-vs-drafts A/B, with the chat server out of the way.
#
# An arm has to load the model itself on an otherwise empty GPU, and the Strata serving this chat is holding most of
# the card.  So this script stops that server (and the engine and vision worker it spawned), runs the four arms, and
# starts the server again when it is done.  Double-click Run-AB.cmd, or run this file from a PowerShell window.

# 2026-07-30: the fifth arm (per-layer cache slots) asks a question the first four cannot, and re-running all five at
# 8 runs each costs hours to learn something arms 2-4 already answered.  These three parameters let one call name the
# arms and the run count; with none of them this script does exactly what it did before.  $armName takes a SEMICOLON-
# separated list (not commas: the arm labels contain them) because the model load costs minutes per arm and the server
# must only be stopped once for them all.
param([string]$armName = "", [int]$runs = 3, [string]$outOverride = "")

$repo = "<work>/Strata-0140"
$cfg  = "<work>/strata.json"
$py   = "<home>/AppData\Local\Programs\Python\Python311\python.exe"
$srv  = "C:\Program Files\Python311\python.exe"
$port = 8080
# the agent session, not the fresh prompts: the first run of this found a 0.0% cache hit rate, because a prompt that
# never repeats gives the cache nothing to hit.  This suite resends the conversation turn after turn, which is the one
# case the cache is built for - and the only one that can say whether the first run's conclusion holds.
$tests = "agent"
$out   = if ($outOverride) { $outOverride } else { "bench\results\ab-cache-vs-drafts-agent" }
$log  = "<work>/ab-offline-agent.log"

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
             Where-Object { $_.CommandLine -and ($_.CommandLine -match $mine -or $_.CommandLine -match '<work>') } |
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
$armList = @()
if ($armName) { $armList = @($armName -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
$armArgs = @()
if ($runs -ne 3) { $armArgs += @("--runs", "$runs") }
Say ("running on '$tests' (runs $runs, out $out)" + $(if ($armList) { ", arms: " + ($armList -join ' | ') } else { ": all arms" }))
if (-not $armList) {
    & $py -u tools\strata_ab.py --config $cfg --port $port --tests $tests --out $out --yes @armArgs 2>&1 | ForEach-Object {
        $t = "$_"
        Write-Host $t
        Add-Content -Path $log -Value $t -Encoding UTF8
    }
    $rc = $LASTEXITCODE
} else {
    # one process per arm, inside the same window where the server is stopped: strata_ab.py takes one --arm-name
    $rc = 0
    foreach ($arm in $armList) {
        Say ("arm: $arm")
        & $py -u tools\strata_ab.py --config $cfg --port $port --tests $tests --out $out --yes `
            --arm-name $arm @armArgs 2>&1 | ForEach-Object {
            $t = "$_"
            Write-Host $t
            Add-Content -Path $log -Value $t -Encoding UTF8
        }
        if ($LASTEXITCODE -ne 0) { $rc = $LASTEXITCODE }
    }
}
Say "the arms finished with exit $rc"

# 5. bring the chat back up, in its own window so it survives this script
Say "starting the server again"
Start-Process -FilePath $srv -ArgumentList "-u","-m","serve.server","--engine","strata","--config",$cfg,"--port",$port `
    -WorkingDirectory $repo -WindowStyle Minimized
Say "done - results in $out, log at $log"
Write-Host ""
Write-Host "Finished.  Close this window, or read the log:"
Write-Host "  notepad $log"
Read-Host "Press Enter to close"
