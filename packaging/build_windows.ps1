# Builds dist\SessionRecord\ with SessionRecord.exe (desktop app) and session-record.exe (console).
# Usage (PowerShell, from the repository root):
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 [-FfmpegDir C:\ffmpeg\bin]
# With -FfmpegDir, ffmpeg.exe and ffprobe.exe are copied next to the app so nothing else needs installing.
param([string]$FfmpegDir = "")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

python -m venv .venv-build
.\.venv-build\Scripts\python -m pip install --upgrade pip
.\.venv-build\Scripts\python -m pip install . pyinstaller

$common = @("--noconfirm", "--clean", "--paths", "src",
            "--add-data", "src\session_record\config.example.toml;session_record",
            "--collect-submodules", "pythonosc")
.\.venv-build\Scripts\pyinstaller @common --windowed --name SessionRecord packaging\gui_entry.py
.\.venv-build\Scripts\pyinstaller @common --console --name session-record --distpath build\cli packaging\cli_entry.py

# ship the console tool inside the same folder (shares the _internal runtime)
Copy-Item build\cli\session-record\session-record.exe dist\SessionRecord\
Copy-Item src\session_record\config.example.toml dist\SessionRecord\config.example.toml
Copy-Item songs.example.csv dist\SessionRecord\songs.example.csv
Copy-Item README.md dist\SessionRecord\README.md

if ($FfmpegDir) {
    Copy-Item (Join-Path $FfmpegDir "ffmpeg.exe") dist\SessionRecord\
    Copy-Item (Join-Path $FfmpegDir "ffprobe.exe") dist\SessionRecord\
}
Write-Host "Built dist\SessionRecord\SessionRecord.exe"
