param([string]$Python = 'python', [string]$Iscc = '', [string]$FfmpegZip = '', [switch]$BundleOnly)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$version = & $Python -c "import runpy; print(runpy.run_path('app/app_version.py')['VERSION'])"
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid app version' }
# 转换组件随安装包分发：缺省时自动暂存固定版本的 LGPL FFmpeg。
$prepare = @{ Destination = (Join-Path (Get-Location) 'tools/ffmpeg/bin') }
if ($FfmpegZip) { $prepare.ZipPath = $FfmpegZip }
& (Join-Path $PSScriptRoot 'prepare_ffmpeg.ps1') @prepare
& $Python -m PyInstaller --clean --noconfirm packaging/desktop-gallery.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
foreach ($exe in @('DesktopGallery.exe', 'DesktopGalleryLogin.exe')) {
    if (!(Test-Path -LiteralPath "dist/DesktopGallery/$exe")) { throw "Missing $exe" }
}
foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
    $bundled = "dist/DesktopGallery/_internal/tools/ffmpeg/bin/$tool"
    if (!(Test-Path -LiteralPath $bundled)) { throw "Bundled converter missing: $tool" }
}
if ($BundleOnly) { return }
if (!$Iscc) {
    $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($compiler) { $Iscc = $compiler.Source }
    else { $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
}
if (!(Test-Path -LiteralPath $Iscc)) { throw 'Install Inno Setup 6 or pass -Iscc <path>' }
& $Iscc "/DAppVersion=$version" packaging/installer.iss
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
$installer = "dist/installer/DesktopGallery-$version-Setup.exe"
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $(Split-Path $installer -Leaf)" | Set-Content -LiteralPath "$installer.sha256" -Encoding ascii
Write-Host "Built $installer"
