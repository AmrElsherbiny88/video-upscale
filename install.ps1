$ErrorActionPreference = "Stop"

$sourceRoot = $PSScriptRoot
$sourceAssets = Join-Path $sourceRoot "assets"
$iconSource = Join-Path $sourceAssets "media_upscaler.ico"

foreach ($requiredFile in @(
    (Join-Path $sourceRoot "app.py"),
    (Join-Path $sourceRoot "README.md"),
    $iconSource
)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required installer file was not found: $requiredFile"
    }
}

$pythonExecutable = & py -3 -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0 -or -not $pythonExecutable) {
    throw "Python 3 was not found. Install Python 3 with Tkinter, then run Install.bat again."
}
$pythonExecutable = $pythonExecutable.Trim()
$pythonWindowed = Join-Path (Split-Path -Parent $pythonExecutable) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonWindowed -PathType Leaf)) {
    throw "pythonw.exe was not found beside Python. Reinstall Python with Tkinter enabled."
}
& $pythonExecutable -c "import tkinter"
if ($LASTEXITCODE -ne 0) {
    throw "Tkinter is missing from this Python installation."
}

$installRoot = Join-Path $env:LOCALAPPDATA "Programs\AI Media Upscaler"
$installAssets = Join-Path $installRoot "assets"
New-Item -ItemType Directory -Path $installAssets -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceRoot "app.py") -Destination $installRoot -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "README.md") -Destination $installRoot -Force
Copy-Item -LiteralPath $iconSource -Destination $installAssets -Force

$appPath = Join-Path $installRoot "app.py"
$iconPath = Join-Path $installAssets "media_upscaler.ico"
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPaths = @(
    (Join-Path $desktop "AI Media Upscaler.lnk"),
    (Join-Path $startMenu "AI Media Upscaler.lnk")
)

$shell = New-Object -ComObject WScript.Shell
foreach ($shortcutPath in $shortcutPaths) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonWindowed
    $shortcut.Arguments = "`"$appPath`""
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.IconLocation = "$iconPath,0"
    $shortcut.Description = "AI-enhance videos and images with Real-ESRGAN"
    $shortcut.Save()
}

Write-Output "Installed to: $installRoot"
Write-Output "Desktop and Start Menu shortcuts were created."
Write-Output "Video2X and FFmpeg are separate requirements; open the app's Instructions button for setup."
