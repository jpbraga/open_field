@echo off
rem openField installer for cmd.exe: runs the PowerShell installer (install.ps1).
rem
rem   curl -fsSL https://raw.githubusercontent.com/jpbraga/open_field/main/install.cmd -o "%TEMP%\openfield-install.cmd" && "%TEMP%\openfield-install.cmd"
rem
rem Options: set OPENFIELD_WITH_TOOLS=1, OPENFIELD_PORT=8765, OPENFIELD_NO_SERVICE=1, OPENFIELD_DIR=... before running it.
setlocal
where powershell >nul 2>nul
if errorlevel 1 (
  echo error: PowerShell is required ^(it ships with Windows 10 and 11^).
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/jpbraga/open_field/main/install.ps1 | iex"
exit /b %errorlevel%
