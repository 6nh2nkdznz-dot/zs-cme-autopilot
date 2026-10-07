# Fetch MaaFramework docs via the GitHub Contents API.
#
# Why not git clone: on this machine only api.github.com is reachable.
# github.com / codeload.github.com / raw.githubusercontent.com all time out,
# so `git clone` and tarball downloads fail. The Contents API works and
# returns base64-encoded file bodies we can decode locally.
#
# NOTE: this file is deliberately pure ASCII. Windows PowerShell 5.1 reads
# .ps1 as ANSI unless the file has a UTF-8 BOM, which corrupts non-ASCII
# string literals and causes parse errors.
#
# Scope (only what we actually need):
#   * repo root *.md
#   * docs/zh_cn/**   (Chinese docs, primary reference)
#   * docs/en_us/**   (English docs, some chapters are more detailed)
#   * tools/          (pipeline.schema.json, interface.schema.json, ...)
#   * sample/         (official examples)
#
# Usage:
#   powershell -File scripts\fetch_maafw_docs.ps1

$ErrorActionPreference = 'Stop'

$repo   = 'MaaXYZ/MaaFramework'
$branch = 'main'
$root   = Join-Path $PSScriptRoot '..\vendor\MaaFramework'
$root   = [System.IO.Path]::GetFullPath($root)
$api    = "https://api.github.com/repos/$repo/contents"

$headers = @{
    'User-Agent' = 'dsh-docs-fetcher'
    'Accept'     = 'application/vnd.github+json'
}

$wantedPrefixes = @('', 'docs/zh_cn', 'docs/en_us', 'tools', 'sample')
$wantedExt      = @('.md', '.json', '.jsonc', '.txt')

$script:fileCount  = 0
$script:skipCount  = 0
$script:failCount  = 0
$script:totalBytes = 0

function Get-Contents([string]$path) {
    $url = if ([string]::IsNullOrEmpty($path)) { $api } else { "$api/$path" }
    for ($try = 1; $try -le 3; $try++) {
        try {
            return Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 30
        } catch {
            if ($try -eq 3) { throw }
            Start-Sleep -Seconds (2 * $try)
        }
    }
}

function Save-RemoteFile([string]$path) {
    $url  = "$api/$path"
    $meta = $null
    for ($try = 1; $try -le 3; $try++) {
        try {
            $meta = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 30
            break
        } catch {
            if ($try -eq 3) { throw }
            Start-Sleep -Seconds (2 * $try)
        }
    }

    $bytes = [Convert]::FromBase64String($meta.content)
    $out   = Join-Path $root ($path -replace '/', '\')
    New-Item -ItemType Directory -Force -Path (Split-Path $out -Parent) | Out-Null
    [System.IO.File]::WriteAllBytes($out, $bytes)

    $script:fileCount++
    $script:totalBytes += $bytes.Length
    Write-Host ("  + {0,-56} {1,8:N0} B" -f $path, $bytes.Length)
}

function Walk([string]$path) {
    $items = Get-Contents $path
    foreach ($item in $items) {
        $childPath = if ([string]::IsNullOrEmpty($path)) { $item.name } else { "$path/$($item.name)" }

        if ($item.type -eq 'dir') {
            $wanted = $false
            foreach ($p in $wantedPrefixes) {
                if ($childPath -eq $p -or $childPath.StartsWith("$p/")) { $wanted = $true; break }
            }
            if ($wanted) { Walk $childPath }
            continue
        }

        if ($item.type -ne 'file') { continue }

        $ext = [System.IO.Path]::GetExtension($item.name).ToLower()
        if ($wantedExt -notcontains $ext) { $script:skipCount++; continue }

        # At repo root take only .md, skip build config noise.
        if ([string]::IsNullOrEmpty($path) -and $ext -ne '.md') { $script:skipCount++; continue }

        try {
            Save-RemoteFile $childPath
        } catch {
            $script:failCount++
            Write-Warning "  FAILED: $childPath -- $($_.Exception.Message)"
        }
    }
}

Write-Host "=== Fetching MaaFramework docs via GitHub Contents API ==="
Write-Host "repo   : $repo @ $branch"
Write-Host "target : $root"
Write-Host "scope  : $($wantedPrefixes -join ', ')"
Write-Host ""

New-Item -ItemType Directory -Force -Path $root | Out-Null

$stamp = @"
MaaFramework docs snapshot
repo    : $repo
branch  : $branch
fetched : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
method  : GitHub Contents API (git clone / codeload unreachable from this host)
scope   : $($wantedPrefixes -join ', ')  (only .md / .json / .jsonc / .txt)
"@
[System.IO.File]::WriteAllText((Join-Path $root 'SNAPSHOT.txt'), $stamp)

Walk ''

Write-Host ""
Write-Host "=== done ==="
Write-Host ("  downloaded : {0} files, {1:N1} MB" -f $script:fileCount, ($script:totalBytes / 1MB))
Write-Host ("  skipped    : {0} (non-doc types)" -f $script:skipCount)
Write-Host ("  failed     : {0}" -f $script:failCount)
