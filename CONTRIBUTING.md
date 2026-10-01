# 參與開發

首版以 Windows、Node.js 24、Python 3.12 為測試環境，繁體中文為主要介面語言。請先閱讀 [架構](docs/public/architecture.md) 及 [資料與隱私](docs/public/privacy.md)。

```powershell
npm ci
npm run build
npm test
.\.venv\Scripts\python.exe -m unittest discover -s server -t .
npm run test:storage
npm run test:first-run
npm run test:ui
```

Python 環境先透過 `scripts/setup.ps1` 建立。UI 測試使用 `--test-mode`、合成相機與獨立資料位置；初次啟動測試另外使用全新資料根。受限測試機可設定 `$env:TUTOR_TEST_NO_SANDBOX='1'`，此選項僅用於測試啟動。

預設自動測試不需要 API key 或真實 CLI 登入。`probe-*`、`check-speech`、`check-pipeline`、`--native-camera` 等屬手動服務／裝置診斷，可能使用帳號、開啟裝置或產生費用，不屬於一般 CI。

提交前執行 `npm run check:public`。請使用合成 fixture 重現問題；不要提交學生記憶、學習單、聯絡簿、照片、音訊、金鑰、CLI 授權、日誌或整包個人資料。新來源檔案須加入 `scripts/public-files.json` 的逐檔允許清單並經審核。不要直接對含私人檔案的開發目錄打包。

PR 說明問題、行為變更及測試；schema 變更須包含原處升級及回復方式。第三方程式或素材須保留來源及授權。維護者審核 PR、merge 與 Release。貢獻以本專案 MIT 條件提供；不要求額外 CLA。
