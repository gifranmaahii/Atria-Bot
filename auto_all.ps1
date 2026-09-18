#requires -Version 5.1
<#
AUTO-ALL PIPELINE -- satu perintah untuk semuanya.
  proxy (auto panen + auto-heal) -> daftar (gbatch, rotasi proxy) -> 9Router (auto-inject + sync)

Pakai:
  .\auto_all.ps1              # daftarkan SEMUA sisa akun di gsuite.txt (default)
  .\auto_all.ps1 -N 10        # cuma 10 akun
  .\auto_all.ps1 -Loop 30     # mode ternak AFK: ulang tiap 30 menit sampai gsuite.txt habis
  .\auto_all.ps1 -NoProxy     # paksa jalan tanpa proxy (direct)
  .\auto_all.ps1 -Skip9Router # lewati sync 9Router di akhir
  .\auto_all.ps1 -DryRun      # lihat rencana saja, tidak eksekusi

Satu-satunya input manual yang tersisa: isi gsuite.txt (email|password per baris).
#>
param(
    [int]$N = 0,
    [int]$MinPool = 5,
    [int]$Loop = 0,
    [switch]$NoProxy,
    [switch]$Skip9Router,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"
$Root    = $PSScriptRoot
$Petani  = Join-Path $Root "petani-proxy"
$Gsuite  = Join-Path $Root "gsuite.txt"
$KeysF   = Join-Path $Root "api_keys.json"
$LogDir  = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("auto_all_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$GwUrl   = "http://127.0.0.1:8888/api/status"
$Py      = "python"

function Log([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    $line | Out-File -FilePath $LogFile -Append -Encoding utf8
}

function Run([string[]]$rargs, [string]$wd = $Root) {
    & $Py @rargs 2>&1 | Tee-Object -FilePath $LogFile -Append
}

function Get-Gateway {
    try { return (Invoke-RestMethod -Uri $GwUrl -TimeoutSec 5 -ErrorAction Stop) }
    catch { return $null }
}

function Ensure-Gateway {
    $s = Get-Gateway
    if ($s) {
        Log "OK  gateway hidup | pool_size=$($s.stats.pool_size) | evicted=$($s.stats.health_checker.total_evicted) refilled=$($s.stats.health_checker.total_refilled)"
        return $s
    }
    Log "WAIT gateway mati -> menyalakan --daemon-gateway (auto-healer) di background..."
    if ($DryRun) { Log "DRY-RUN: (skip start gateway)"; return (Get-Gateway) }
    $p = Start-Process -FilePath $Py -ArgumentList "main.py", "--daemon-gateway" `
        -WorkingDirectory $Petani -WindowStyle Minimized -PassThru
    $p.Id | Out-File -FilePath (Join-Path $LogDir "gateway.pid") -Encoding utf8
    Log "     gateway PID $($p.Id) (logs\gateway.pid)"
    $deadline = (Get-Date).AddSeconds(240)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $s = Get-Gateway
        if ($s) { Log "OK  gateway naik | pool_size=$($s.stats.pool_size)"; return $s }
    }
    Log "WARN gateway belum naik setelah 240s. Lanjut pakai cadangan (output/live_all.txt + statik, direct)."
    return $null
}

function Warm-Pool {
    param([object]$Status)
    if (-not $Status) { return }
    $sz = [int]$Status.stats.pool_size
    if ($sz -ge $MinPool) { Log "OK  pool cukup hangat ($sz >= $MinPool)"; return }
    Log "WAIT pool tipis ($sz < $MinPool) -> top-up --fast-harvest 15..."
    if ($DryRun) { Log "DRY-RUN: (skip fast-harvest)"; return }
    Run @("main.py", "--fast-harvest", "15") $Petani | Out-Null
    $s = Get-Gateway
    if ($s) { Log "OK  pool sekarang: $($s.stats.pool_size)" }
}

function Get-RemainingAccounts {
    if (-not (Test-Path $Gsuite)) { return @() }
    $emails = Get-Content $Gsuite | Where-Object { $_ -match "\S" -and $_ -notmatch "^\s*#" } |
        ForEach-Object { (($_ -split "\|")[0]).Trim().ToLower() } | Where-Object { $_ }
    $done = @()
    if (Test-Path $KeysF) {
        $done = @((Get-Content $KeysF -Raw | ConvertFrom-Json) |
            Where-Object { $_.email } | ForEach-Object { $_.email.ToString().Trim().ToLower() })
    }
    return @($emails | Where-Object { $done -notcontains $_ })
}


function Invoke-Pipeline {
    Log "==================== AUTO-ALL PIPELINE ===================="
    if ($NoProxy) { $env:ATRIA_GSUITE_PROXY = "0"; Log "OPT proxy DIMATIKAN untuk alur gsuite (direct)" }
    else          { $env:ATRIA_GSUITE_PROXY = $null }

    # 1) proxy: pastikan gateway + hangatkan pool
    Log "1/4 PROXY  :: cek & nyalakan gateway PetaniProxy (auto panen + auto-heal)"
    $s = Ensure-Gateway
    Warm-Pool $s

    # 2) tentukan jumlah akun
    $remain = Get-RemainingAccounts
    $count  = if ($N -gt 0) { [Math]::Min($N, $remain.Count) } else { $remain.Count }
    $note   = if ($N -gt 0) { " (diminta $N -> dipakai $count)" } else { "" }
    Log "2/4 AKUN   :: sisa gsuite.txt = $($remain.Count)$note"
    if ($count -lt 1) {
        Log "STOP tidak ada akun tersisa. Tambahkan 'email|password' ke gsuite.txt lalu jalankan ulang."
        return 0
    }

    # 3) 9Router: cek target (lokal auto-discover; panel online via menu T / ninerouter.py add)
    Log "3/4 9ROUTER :: cek target inject"
    if ($DryRun) { Log "DRY-RUN: (skip scan)" }
    else         { Run @("ninerouter.py", "scan") | Out-Null }

    # 4) daftar!  (gbatch: proxy dulu -> gagal rotasi -> habis direct; save + auto-inject 9Router per key)
    Log "4/4 DAFTAR :: python atria_register.py gbatch $count"
    if ($DryRun) { Log "DRY-RUN: (skip gbatch)"; return 0 }
    Run @("atria_register.py", "gbatch", "$count")

    # 5) safety net: sync ulang SEMUA key ke SEMUA 9Router
    if (-not $Skip9Router) {
        Log "SYNC   :: python atria_register.py sync (re-sync semua key -> semua 9Router)"
        if (-not $DryRun) { Run @("atria_register.py", "sync") }
    }
    $keys = @()
    if (Test-Path $KeysF) { $keys = @((Get-Content $KeysF -Raw | ConvertFrom-Json) | Where-Object { $_.key }) }
    Log "SELESAI :: total key di api_keys.json = $($keys.Count)"
    return $count
}

# ── Main ──────────────────────────────────────────────────────────
$round = 0
while ($true) {
    $round++
    if ($Loop -gt 0 -and $round -gt 1) {
        Log "Mode AFK: menunggu $Loop menit sebelum ronde berikutnya..."
        Start-Sleep -Seconds ($Loop * 60)
    }
    $did = Invoke-Pipeline
    if ($Loop -le 0) { break }                       # mode biasa: sekali jalan
    if ((Get-RemainingAccounts).Count -lt 1) { Log "gsuite.txt habis -> berhenti."; break }
}
Log "Log lengkap: $LogFile"
