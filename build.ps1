param(
    [ValidateSet('All', 'Modern', 'Classic')]
    [string]$Edition = 'All'
)

$ErrorActionPreference = 'Stop'

$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyInstallerArgs = @(
    '--noconfirm'
    '--clean'
    '--onefile'
    '--windowed'
    '--add-data', 'payload;payload'
    '--collect-all', 'paramiko'
)

$BuildTargets = @()
if ($Edition -in @('All', 'Modern')) {
    $BuildTargets += [PSCustomObject]@{
        Name = 'TG115-Deployer-Modern'
        Source = 'installer.py'
        Runtime = 'PySide6'
    }
}
if ($Edition -in @('All', 'Classic')) {
    $BuildTargets += [PSCustomObject]@{
        Name = 'TG115-Deployer-Classic'
        Source = 'installer_classic.py'
        Runtime = 'Tkinter'
    }
}

function Get-Tg115IsolatedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ToolPath
    )

    # PyInstaller resolves binary dependencies through PATH. Third-party tools
    # can expose incompatible DLLs there (for example another ICU build), which
    # are then accidentally bundled and take precedence over Windows system
    # libraries in the generated EXE.
    $entries = @(
        (Split-Path -Parent $ToolPath)
        "$env:SystemRoot\System32"
        $env:SystemRoot
        "$env:SystemRoot\System32\Wbem"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

    return $entries -join [System.IO.Path]::PathSeparator
}

function Initialize-Tg115TkEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonBase
    )

    $tclRoot = Join-Path $PythonBase 'tcl'
    if (-not (Test-Path -LiteralPath $tclRoot)) {
        return
    }
    $tclLibrary = Get-ChildItem -LiteralPath $tclRoot -Directory -Filter 'tcl*' |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'init.tcl') } |
        Sort-Object Name -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    $tkLibrary = Get-ChildItem -LiteralPath $tclRoot -Directory -Filter 'tk*' |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'tk.tcl') } |
        Sort-Object Name -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if ($tclLibrary -and $tkLibrary) {
        $env:TCL_LIBRARY = $tclLibrary
        $env:TK_LIBRARY = $tkLibrary
    }
}

function Invoke-Tg115PythonBuild {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExecutable
    )

    $originalPath = $env:Path
    $originalTclLibrary = $env:TCL_LIBRARY
    $originalTkLibrary = $env:TK_LIBRARY
    try {
        $env:Path = Get-Tg115IsolatedPath -ToolPath $PythonExecutable
        $pythonBase = (& $PythonExecutable -c 'import sys; print(sys.base_prefix)').Trim()
        if ($LASTEXITCODE -ne 0) {
            throw '无法确定 Python 基础安装目录'
        }
        Initialize-Tg115TkEnvironment -PythonBase $pythonBase
        foreach ($target in $BuildTargets) {
            Write-Host "Building $($target.Name) with $($target.Runtime)..."
            if ($target.Runtime -eq 'PySide6') {
                & $PythonExecutable -c 'from PySide6.QtWidgets import QApplication; app = QApplication([]); app.quit()'
            }
            else {
                & $PythonExecutable -c 'import tkinter as tk; root = tk.Tk(); root.withdraw(); root.update_idletasks(); root.destroy()'
            }
            if ($LASTEXITCODE -ne 0) {
                throw "$($target.Runtime) GUI 运行时不可用，停止生成无法启动的部署器"
            }
            $targetArgs = $PyInstallerArgs + @('--name', $target.Name, $target.Source)
            & $PythonExecutable -m PyInstaller @targetArgs
            if ($LASTEXITCODE -ne 0) {
                throw "$($target.Name) 构建失败"
            }
        }
    }
    finally {
        $env:Path = $originalPath
        $env:TCL_LIBRARY = $originalTclLibrary
        $env:TK_LIBRARY = $originalTkLibrary
    }
}

function Invoke-Tg115UvBuild {
    param(
        [Parameter(Mandatory = $true)]
        [string]$UvExecutable
    )

    $originalPath = $env:Path
    $originalTclLibrary = $env:TCL_LIBRARY
    $originalTkLibrary = $env:TK_LIBRARY
    try {
        $env:Path = Get-Tg115IsolatedPath -ToolPath $UvExecutable
        $pythonBaseOutput = & $UvExecutable run --with-requirements requirements-build.txt `
            python -c 'import sys; print(sys.base_prefix)'
        if ($LASTEXITCODE -ne 0) {
            throw '无法确定 Python 基础安装目录'
        }
        $pythonBase = ($pythonBaseOutput | Select-Object -Last 1).Trim()
        Initialize-Tg115TkEnvironment -PythonBase $pythonBase
        foreach ($target in $BuildTargets) {
            Write-Host "Building $($target.Name) with $($target.Runtime)..."
            if ($target.Runtime -eq 'PySide6') {
                & $UvExecutable run --with-requirements requirements-build.txt `
                    python -c 'from PySide6.QtWidgets import QApplication; app = QApplication([]); app.quit()'
            }
            else {
                & $UvExecutable run --with-requirements requirements-build.txt `
                    python -c 'import tkinter as tk; root = tk.Tk(); root.withdraw(); root.update_idletasks(); root.destroy()'
            }
            if ($LASTEXITCODE -ne 0) {
                throw "$($target.Runtime) GUI 运行时不可用，停止生成无法启动的部署器"
            }
            $targetArgs = $PyInstallerArgs + @('--name', $target.Name, $target.Source)
            & $UvExecutable run --with-requirements requirements-build.txt `
                python -m PyInstaller @targetArgs
            if ($LASTEXITCODE -ne 0) {
                throw "$($target.Name) 构建失败"
            }
        }
    }
    finally {
        $env:Path = $originalPath
        $env:TCL_LIBRARY = $originalTclLibrary
        $env:TK_LIBRARY = $originalTkLibrary
    }
}

Push-Location $SourceDir
try {
    if ($Edition -eq 'All') {
        $staleArtifact = Join-Path $SourceDir 'dist\TG115-Deployer.exe'
        if (Test-Path -LiteralPath $staleArtifact) {
            Remove-Item -LiteralPath $staleArtifact -Force
        }
    }
    if ($env:TG115_BUILD_PYTHON) {
        & $env:TG115_BUILD_PYTHON -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) {
            throw '构建依赖安装失败'
        }
        Invoke-Tg115PythonBuild -PythonExecutable $env:TG115_BUILD_PYTHON
    }
    elseif ($uvCommand = Get-Command uv -ErrorAction SilentlyContinue) {
        Invoke-Tg115UvBuild -UvExecutable $uvCommand.Source
    }
    else {
        & python -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) {
            throw '构建依赖安装失败；请安装 uv 或提供 TG115_BUILD_PYTHON'
        }
        $pythonCommand = Get-Command python -ErrorAction Stop
        Invoke-Tg115PythonBuild -PythonExecutable $pythonCommand.Source
    }
    foreach ($target in $BuildTargets) {
        Write-Host "Build succeeded: $SourceDir\dist\$($target.Name).exe"
    }
}
finally {
    Pop-Location
}
