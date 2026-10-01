$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot
$privateHome = & ./.venv/Scripts/python.exe -m server.cli_home
if ($LASTEXITCODE -ne 0) { throw '無法建立此 Windows 帳號的訂閱目錄' }
# Change only this helper process; never mutate the user's global environment.
$env:CODEX_HOME = $privateHome.Trim()
& node.exe ./node_modules/@openai/codex/bin/codex.js login
if ($LASTEXITCODE -ne 0) { throw 'Codex 登入未完成' }
