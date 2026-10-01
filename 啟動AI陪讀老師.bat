@echo off
chcp 65001 >nul
setlocal
title AI陪讀老師 - 啟動中
pushd "%~dp0"
set "npm_config_cache=%~dp0.local\npm-cache"
set "TEMP=%~dp0app-data\temp"
set "TMP=%~dp0app-data\temp"
if not exist "%TEMP%" mkdir "%TEMP%"
if errorlevel 1 goto directory_error

where node.exe >nul 2>&1
if errorlevel 1 goto node_missing
where npm.cmd >nul 2>&1
if errorlevel 1 goto node_missing
if not exist "node_modules\.bin\electron.cmd" goto setup_missing
if not exist ".venv\Scripts\python.exe" goto setup_missing

echo 正在準備AI陪讀老師，請稍候...
echo 程式開啟後，請保留這個視窗；結束程式後它會自動關閉。
echo.
call npm.cmd start
if errorlevel 1 goto launch_error
popd
endlocal
exit /b 0

:node_missing
echo 找不到 Node.js 或 npm。請先安裝 Node.js 24，再重新開啟此檔案。
goto failed

:setup_missing
echo 專案環境尚未安裝完整。
echo 請在本目錄開啟 PowerShell，執行：
echo powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
goto failed

:launch_error
echo.
echo AI陪讀老師啟動失敗，請查看上方錯誤訊息。
goto failed

:directory_error
echo 無法開啟專案目錄。請確認檔案仍位於完整的專案資料夾內。
pause
endlocal
exit /b 1

:failed
echo.
pause
popd
endlocal
exit /b 1
