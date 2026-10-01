param([string]$PythonExecutable = '', [string]$PythonSource = '', [string]$ElectronArchive = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot
$env:TEMP = $env:TMP = Join-Path $projectRoot '.local/setup-temp'
$env:npm_config_cache = Join-Path $projectRoot '.local/npm-cache'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$bundledPython = Join-Path $projectRoot '.runtime/python/python.exe'
$pythonArgs = @()
if ($PythonExecutable) { $pythonCommand = $PythonExecutable }
elseif ($PythonSource) { $pythonCommand = Join-Path $PythonSource 'python.exe' }
elseif (Test-Path -LiteralPath $bundledPython) { $pythonCommand = $bundledPython }
elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $pythonCommand = 'py.exe'
    $pythonArgs = @('-3.12')
}
elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { $pythonCommand = 'python.exe' }
else { throw '請先安裝 Python 3.12，或以 -PythonExecutable 指定 python.exe。' }
& $pythonCommand @pythonArgs -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'
if ($LASTEXITCODE -ne 0) { throw '首版已驗證 Python 3.12，請使用此版本。' }
$venv = Join-Path $projectRoot '.venv'
if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts/python.exe'))) {
    & $pythonCommand @pythonArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw '建立 Python 虛擬環境失敗' }
}
& ./.venv/Scripts/python.exe -m pip install --cache-dir .local/pip-cache -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python 套件安裝失敗' }
npm.cmd ci
if ($LASTEXITCODE -ne 0) { throw 'Node 套件安裝失敗' }
# Electron 44 downloads its executable lazily. Install explicitly with the
# official package checksum so first launch does not hang on Node's downloader.
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules/electron/path.txt'))) {
    if ($ElectronArchive) {
        & ./.venv/Scripts/python.exe -X utf8 scripts/install-electron-fallback.py --archive $ElectronArchive
    } else {
        & ./.venv/Scripts/python.exe -X utf8 scripts/install-electron-fallback.py
    }
    if ($LASTEXITCODE -ne 0) { throw 'Electron 官方執行檔下載或驗證失敗' }
}
npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw '介面編譯失敗' }
if (Test-Path -LiteralPath (Join-Path $projectRoot '.git')) {
    git config --local core.hooksPath .githooks
    if ($LASTEXITCODE -ne 0) { throw '設定公開檔案檢查 hook 失敗' }
}
Write-Host '準備完成。執行 npm start 或點擊啟動AI陪讀老師.bat。'
