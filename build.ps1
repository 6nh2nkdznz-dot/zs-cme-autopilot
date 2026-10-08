# Build the exe and sync the WHOLE output directory to the project root.
#
# Why this script exists
# ---------------------
# The build used to be a few hand-typed commands, and the "copy dist to the
# project root" step copied only ZSCMEAutopilot.exe -- a single file. But the
# onedir build keeps the Python sources under _internal\scripts\, and the exe
# is just a shell. So:
#
#     edit script -> rebuild -> copy exe -> still running the OLD code
#
# That bit us for real once: a core.py change made it into dist/, but the
# project-root _internal\scripts\core.py was still half an hour old, so the
# app started with the old logic. The copy must be the WHOLE directory, and
# it must be verified afterwards.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads a BOM-less
# file as ANSI and mangles non-ASCII text, which breaks string terminators
# and the script fails to parse.
#
# Usage:
#     powershell -File build.ps1

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

Write-Host "[build] 1/4 stop any running exe (otherwise: file in use)"
Get-Process -Name ZSCMEAutopilot -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Host "[build] 2/4 PyInstaller (takes a few minutes)"
& python -m PyInstaller build.spec --noconfirm --clean
# PyInstaller logs to stderr, so $LASTEXITCODE can be a bogus 1 under a
# PowerShell pipe. Trust the product file, not the exit code.
$prod = Join-Path $root 'dist\ZSCMEAutopilot\ZSCMEAutopilot.exe'
if (-not (Test-Path $prod)) {
    Write-Error "[build] no product at $prod -- the build failed, scroll up"
    exit 1
}

Write-Host "[build] 3/4 sync the WHOLE dist dir to the project root (not just the exe)"
Copy-Item -Path 'dist\ZSCMEAutopilot\*' -Destination '.' -Recurse -Force

Write-Host "[build] 4/4 verify packaged scripts match the sources"
# Only the files that build.spec ships as *readable* datas (the scripts/ dir).
# launcher.py / launcher_ui.py become compiled modules inside the PYZ archive,
# so there is no plain file on disk to hash -- do not add them here.
$pairs = @(
    @{ src = 'scripts\core.py';     dst = '_internal\scripts\core.py' }
    @{ src = 'scripts\course.py';   dst = '_internal\scripts\course.py' }
    @{ src = 'scripts\main.py';     dst = '_internal\scripts\main.py' }
    @{ src = 'scripts\exam.py';     dst = '_internal\scripts\exam.py' }
    @{ src = 'scripts\progress.py'; dst = '_internal\scripts\progress.py' }
)
$bad = 0
foreach ($p in $pairs) {
    if (-not (Test-Path $p.src) -or -not (Test-Path $p.dst)) {
        Write-Host ("  SKIP {0} (missing file)" -f $p.src)
        continue
    }
    $a = (Get-FileHash $p.src -Algorithm MD5).Hash
    $b = (Get-FileHash $p.dst -Algorithm MD5).Hash
    if ($a -eq $b) {
        Write-Host ("  OK   {0}" -f $p.src)
    } else {
        Write-Host ("  STALE {0} -- packaged copy is an older version!" -f $p.src)
        $bad++
    }
}

$exe = Get-Item 'ZSCMEAutopilot.exe'
Write-Host ("[build] done: {0} bytes  {1}" -f $exe.Length, $exe.LastWriteTime)
if ($bad -gt 0) {
    Write-Error "[build] $bad file(s) did not sync -- do not run it yet"
    exit 1
}
Write-Host "[build] next: .\ZSCMEAutopilot.exe --selftest  (needs all 6 checks OK)"
exit 0
