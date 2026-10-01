# 發布與資料稽核

維護者在本機使用 `scripts/public-files.json` 的逐檔清單匯出；未知檔案不自動納入。新檔案需要明確審核並修改清單。執行 `npm run check:public` 掃描允許清單的文字與路徑；既有 Git 歷史可用 `python scripts/audit_public.py --history` 另外掃描。安裝流程會在 Git checkout 設定 `.githooks/pre-commit`，提交前檢查實際 staged 內容；CI 再做一次稽核。

維護者可另指定 `--private-terms` 指向本機辨識詞清單。此清單不進公開倉庫，報告不印匹配到的文字。一般掃描不等於已辨識全部私人資料；新增二進位、照片、PDF 或壓縮檔須另外人工審核。首版允許清單僅包含 UTF-8 source、文件與合成 JSON。

先完成逐檔／staged blob／完整歷史與產物稽核、無金鑰測試及乾淨安裝驗收，再 push 或發 Release。Release 使用 Git tag 的乾淨 source；不將作者的 dist、node_modules、app-data、日誌或整包工作目錄壓縮後上傳。

GitHub 啟用 secret scanning、push protection 及 private vulnerability reporting。CI 僅使用合成資料且不配置外部服務金鑰；fork PR 不以 pull_request_target 執行未受信程式。CI artifact 只收取經核准的測試報告，不上傳私人 runtime。

安裝包、自動更新與其他平台列為後續工作。包含 Python、CLI、模型或其他執行檔的安裝包需要另外核對授權、來源、checksum 與安裝後權限。
