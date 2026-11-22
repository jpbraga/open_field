# openField installer for Windows (PowerShell 5.1 or 7+).
#
#   irm https://raw.githubusercontent.com/jpbraga/open_field/main/install.ps1 | iex
#
# Options (environment variables, set before running it):
#   $env:OPENFIELD_DIR          where the app goes (default %LOCALAPPDATA%\openfield\app); running again updates it
#   $env:OPENFIELD_REF          git branch or tag (default main)
#   $env:OPENFIELD_PORT         port of the daemon (default 8765)
#   $env:OPENFIELD_WITH_TOOLS=1 also download Real-ESRGAN and RIFE for the video services
#   $env:OPENFIELD_NO_SERVICE=1 do not start it at logon (Task Scheduler)
#   $env:OPENFIELD_REPO         git URL to install from (default https://github.com/jpbraga/open_field.git)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Get-Option([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}
function Say([string]$Message) { Write-Host '==> ' -ForegroundColor DarkYellow -NoNewline; Write-Host $Message }
function Warn([string]$Message) { Write-Host "warning: $Message" -ForegroundColor Yellow }
function Invoke-Native([string]$What, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

$Repo = Get-Option 'OPENFIELD_REPO' 'https://github.com/jpbraga/open_field.git'
$Ref = Get-Option 'OPENFIELD_REF' 'main'
$Dir = Get-Option 'OPENFIELD_DIR' (Join-Path $env:LOCALAPPDATA 'openfield\app')
$Port = Get-Option 'OPENFIELD_PORT' ''
$WithTools = (Get-Option 'OPENFIELD_WITH_TOOLS' '0') -eq '1'
$NoService = (Get-Option 'OPENFIELD_NO_SERVICE' '0') -eq '1'
$BinDir = Join-Path $env:LOCALAPPDATA 'openfield\bin'

Write-Host '  ~~~  ' -ForegroundColor DarkYellow -NoNewline
Write-Host 'openField installer (Windows)'

# uv manages Python (it downloads 3.12 when needed) and the app's dependencies.
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path $uv)) {
        Say 'installing uv (Python package manager, from astral.sh)'
        $env:UV_NO_MODIFY_PATH = '1'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    }
    if (-not (Test-Path $uv)) { throw 'uv was not found after installing it' }
}

$git = Get-Command git -ErrorAction SilentlyContinue
if ((Test-Path (Join-Path $Dir '.git')) -and $git) {
    Say "updating $Dir ($Ref)"
    Invoke-Native 'git fetch' { git -C $Dir fetch --quiet origin $Ref }
    Invoke-Native 'git checkout' { git -C $Dir checkout --quiet $Ref }
    Invoke-Native 'git pull' { git -C $Dir pull --quiet --ff-only origin $Ref }
} elseif ($git) {
    if (Test-Path $Dir) { throw "$Dir exists and is not an openField checkout (set OPENFIELD_DIR to another folder)" }
    Say "downloading openField into $Dir"
    Invoke-Native 'git clone' { git clone --quiet --branch $Ref $Repo $Dir }
} else {
    # No git: download the branch as a zip (keeps .venv and tools when updating).
    if ($Repo -notmatch 'github\.com[/:]([^/]+)/([^/.]+)') { throw 'git is required for a non-GitHub OPENFIELD_REPO' }
    $zipUrl = "https://github.com/$($Matches[1])/$($Matches[2])/archive/refs/heads/$Ref.zip"
    Say "downloading openField ($Ref) into $Dir"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("openfield-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Invoke-WebRequest -Uri $zipUrl -OutFile (Join-Path $tmp 'src.zip') -UseBasicParsing
    Expand-Archive -Path (Join-Path $tmp 'src.zip') -DestinationPath $tmp -Force
    $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    Copy-Item -Path (Join-Path $src.FullName '*') -Destination $Dir -Recurse -Force
    Remove-Item -Recurse -Force $tmp
}

Say 'installing dependencies (uv sync)'
Push-Location $Dir
try { Invoke-Native 'uv sync' { & $uv sync --frozen --no-dev --quiet } } finally { Pop-Location }
$of = Join-Path $Dir '.venv\Scripts\openfield.exe'
if (-not (Test-Path $of)) { throw 'the openfield command was not created' }

# `openfield` on PATH through a small .cmd shim in %LOCALAPPDATA%\openfield\bin.
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Set-Content -Path (Join-Path $BinDir 'openfield.cmd') -Value "@echo off`r`n`"$of`" %*" -Encoding ASCII
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $BinDir) {
    [Environment]::SetEnvironmentVariable('Path', ((@($userPath, $BinDir) | Where-Object { $_ }) -join ';'), 'User')
    $env:Path = "$env:Path;$BinDir"
}

if ($Port) { Invoke-Native 'openfield init' { & $of init --port $Port } } else { Invoke-Native 'openfield init' { & $of init } }

if ($WithTools) {
    Say 'downloading Real-ESRGAN and RIFE'
    & $of install-tools
    if ($LASTEXITCODE -ne 0) { Warn 'could not download the video tools; try again later with: openfield install-tools' }
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Warn 'ffmpeg is not installed; the media services need it (winget install Gyan.FFmpeg)'
}

if (-not $NoService) {
    Say 'starting openField at logon (Task Scheduler)'
    & $of service install
    if ($LASTEXITCODE -ne 0) { Warn 'could not create the scheduled task; run it yourself with: openfield serve' }
}

$conf = Get-Option 'OPENFIELD_CONFIG' (Join-Path $env:APPDATA 'openfield')
$portNow = '8765'
$daemonYaml = Join-Path $conf 'daemon.yaml'
if (Test-Path $daemonYaml) {
    $line = Select-String -Path $daemonYaml -Pattern '^port:\s*(\d+)' | Select-Object -First 1
    if ($line) { $portNow = $line.Matches[0].Groups[1].Value }
}

Write-Host ''
Write-Host 'openField is installed.' -ForegroundColor DarkYellow
Write-Host "  Web UI on this machine:  http://localhost:$portNow/  (no key needed)"
Write-Host "  From other machines:     http://<this machine>:$portNow/  with a key (create keys in the web UI,"
Write-Host "                           Access tab, or with  openfield key --new NAME)"
Write-Host "  Config:                  $conf"
Write-Host "  App:                     $Dir"
Write-Host '  Open a new terminal to use the openfield command.'
