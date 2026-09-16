@echo off
setlocal
cd /d "%~dp0"
if exist "TG115-Deployer-Modern.exe" (
  start "" "TG115-Deployer-Modern.exe"
  exit /b 0
)
if exist "dist\TG115-Deployer-Modern.exe" (
  start "" "dist\TG115-Deployer-Modern.exe"
  exit /b 0
)
if exist "TG115-Deployer-Classic.exe" (
  start "" "TG115-Deployer-Classic.exe"
  exit /b 0
)
if exist "dist\TG115-Deployer-Classic.exe" (
  start "" "dist\TG115-Deployer-Classic.exe"
  exit /b 0
)
if exist "TG115-Deployer.exe" (
  start "" "TG115-Deployer.exe"
  exit /b 0
)
if exist "dist\TG115-Deployer.exe" (
  start "" "dist\TG115-Deployer.exe"
  exit /b 0
)
echo [ERROR] TG115-Deployer-Modern.exe or TG115-Deployer-Classic.exe not found.
echo Please keep this launcher and the EXE in the same folder.
pause
exit /b 1
