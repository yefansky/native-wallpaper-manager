param(
    [string]$Url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-30-13-01/ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-8.1.zip',
    [string]$Sha256 = 'bf346925ea085c262d15a5ceacd521ccbde52f4e3fc6a97f1bf273685fc1a539',
    [string]$ZipPath = '',
    [string]$Destination = '',
    [switch]$Force
)
# 把 LGPL 构建的 ffmpeg.exe/ffprobe.exe 暂存到 build/converter，供独立转换组件包使用。
# 转换组件单独发布一次（converter-v1），安装包与日常更新不再重复携带约 90 MB 的二进制。
$ErrorActionPreference = 'Stop'
if (-not ('System.IO.Compression.ZipFile' -as [type])) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
}
$root = Split-Path $PSScriptRoot -Parent
if (!$Destination) { $Destination = Join-Path $root 'build/converter' }
$Destination = [IO.Path]::GetFullPath($Destination)

$ffmpeg = Join-Path $Destination 'ffmpeg.exe'
$ffprobe = Join-Path $Destination 'ffprobe.exe'
if (!$Force -and (Test-Path -LiteralPath $ffmpeg) -and (Test-Path -LiteralPath $ffprobe)) {
    Write-Host "FFmpeg converter already staged in $Destination"
    return
}

$work = Join-Path $root ('build/.ffmpeg-stage-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    $zip = Join-Path $work 'ffmpeg.zip'
    if ($ZipPath) {
        Copy-Item -LiteralPath $ZipPath -Destination $zip
    } else {
        Write-Host "Downloading FFmpeg build from $Url"
        Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing -MaximumRetryCount 3 -RetryIntervalSec 5
    }
    $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        throw "FFmpeg archive checksum mismatch: expected $Sha256, got $actual"
    }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, (Join-Path $work 'unpacked'))
    $unpacked = Join-Path $work 'unpacked'
    $bundle = Get-ChildItem -LiteralPath $unpacked -Directory | Select-Object -First 1
    if (!$bundle) { throw 'FFmpeg archive layout unexpected: no top-level directory' }
    $bundleBin = Join-Path $bundle.FullName 'bin'
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
        $source = Join-Path $bundleBin $name
        if (!(Test-Path -LiteralPath $source)) { throw "FFmpeg archive is missing $name" }
        Copy-Item -LiteralPath $source -Destination (Join-Path $Destination $name) -Force
    }
    $license = Join-Path $bundle.FullName 'LICENSE.txt'
    if (Test-Path -LiteralPath $license) {
        Copy-Item -LiteralPath $license -Destination (Join-Path $Destination 'LICENSE.txt') -Force
    }
    Write-Host "Staged converter binaries in $Destination"
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
