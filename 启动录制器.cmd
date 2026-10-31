@echo off
cd /d "%~dp0"
if not exist "D:\Python314\pythonw.exe" (
  echo Python was not found: D:\Python314\pythonw.exe
  pause
  exit /b 1
)
start "" "D:\Python314\pythonw.exe" -m af_recorder.gui
