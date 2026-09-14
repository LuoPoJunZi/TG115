$ErrorActionPreference = 'Stop'

$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyInstallerArgs = @(
    '--noconfirm'
    '--clean'
    '--onefile'
    '--windowed'
    '--name', 'TG115-Deployer'
    '--add-data', 'payload;payload'
    '--collect-all', 'paramiko'
    'installer.py'
)

Push-Location $SourceDir
try {
    if ($env:TG115_BUILD_PYTHON) {
        & $env:TG115_BUILD_PYTHON -c 'import tkinter; tkinter.Tcl()'
        if ($LASTEXITCODE -ne 0) {
            throw 'Tcl/Tk 不可用；请修复 Python 安装或设置正确的 TCL_LIBRARY/TK_LIBRARY 后重试'
        }
        & $env:TG115_BUILD_PYTHON -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) {
            throw '构建依赖安装失败'
        }
        & $env:TG115_BUILD_PYTHON -m PyInstaller @PyInstallerArgs
    }
    elseif (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv run --with-requirements requirements-build.txt python -c 'import tkinter; tkinter.Tcl()'
        if ($LASTEXITCODE -ne 0) {
            throw 'Tcl/Tk 不可用，停止生成无法启动的 GUI 部署器'
        }
        & uv run --with-requirements requirements-build.txt `
            python -m PyInstaller @PyInstallerArgs
    }
    else {
        & python -c 'import tkinter; tkinter.Tcl()'
        if ($LASTEXITCODE -ne 0) {
            throw 'Tcl/Tk 不可用，停止生成无法启动的 GUI 部署器'
        }
        & python -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) {
            throw '构建依赖安装失败；请安装 uv 或提供 TG115_BUILD_PYTHON'
        }
        & python -m PyInstaller @PyInstallerArgs
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller 构建失败'
    }
    Write-Host "Build succeeded: $SourceDir\dist\TG115-Deployer.exe"
}
finally {
    Pop-Location
}
