# AI Tutor｜伴讀

Windows 上的開源家庭作業陪讀助手，結合書桌相機、語音、教學白板與可修正的學習記憶。繁體中文優先，供家長陪同試用。

**目前是實驗版。** 本機工作區與合成測試可運作；外部 AI 路由的接線、真實服務測試與真人教學驗收分開標示。原始碼免費，AI 訂閱及 API 費用由使用者負擔。

| 模式 | 所需服務 | 狀態 |
|---|---|---|
| 本機專注工作區 | 無 AI 金鑰 | 相機校正、白板與課堂紀錄可使用；沒有 AI 教學 |
| 基本陪讀 | Groq 轉錄、Codex 或 agy、Edge TTS | 已接線；既有真實短測及合成整合測試，仍需完整教學驗收 |
| OpenAI Live | OpenAI key，依設定選教學及圖片服務 | 已接線；完成真實 API 短測，持續收音與大圖片用 mock 驗證 |
| Gemini Live | Gemini key 與 CLI 委派 | 已接線但尚未完成真實 key 驗收；用途資格需另核對 |

Google Gemini Developer API 的 [現行條款](https://ai.google.dev/gemini-api/terms) 對未滿 18 歲使用者及可能由其使用的應用有限制（核對日期：2026-10-01）；此接線不可視為已確認適用兒童陪讀。其他服務也須按最新條款與帳號權限確認用途。公開程式碼不代表供應商已核准所有使用情境。

## 安裝與啟動

首版測試環境：Windows、Node.js 24、Python 3.12。尚未驗收 macOS／Linux，也尚未提供 Windows 安裝包。

```powershell
git clone https://github.com/t214312004/AI_Tutor.git
cd AI_Tutor
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
npm start
```

`setup.ps1` 使用已安裝的 Python 3.12、建立 `.venv` 並從 lockfile 安裝套件。Python 不在 PATH 時可指定：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1 -PythonExecutable "D:\Python312\python.exe"
```

安裝完成後也可雙擊 `啟動AI陪讀老師.bat`。關閉程式後啟動視窗會結束，錯誤時保留訊息。

Electron 執行檔由官方 Release 下載，並以 npm 套件附帶的 SHA-256 核對。網路受限時可將官方同版本 Windows x64 zip 透過 `-ElectronArchive` 提供給 setup；離線檔仍須通過同一個 checksum，不使用作者的執行資料或帳號。

## 第一次使用

1. 新安裝是空白學生名單，輸入暱稱與年級後按「新增學生」。
2. 可先關閉不需要的相機，或校正方向與拍攝範圍。
3. 未設定 AI key 時，基本模式可進入本機專注工作區。
4. 若只想看展示，空白資料庫可按「載入合成展示資料」，建立虛構學生與學校。
5. 家長設定可修改學生、查看紀錄與設定服務金鑰；AI 模式開始前會核對所選服務。

無金鑰展示不提供模擬老師冒充真正的 AI 教學。開發者可執行 `test:first-run`、`test:ui` 及其他 mock 腳本驗證整合流程。

基本模式需設定 Groq key，並登入選用的 CLI。Codex 登入輔助指令為 `scripts/login-codex.ps1`；agy 為外部可選 CLI，需由使用者另行安裝及登入。GPT 模式的教學與圖片路由各自指定 provider、模型及 effort。模型可用性依服務端及帳號權限決定，缺少服務時會顯示錯誤。

## 資料位置

預設私有資料在 `%LOCALAPPDATA%\AI-Tutor\`，**不隨程式碼上傳 GitHub**。可自訂位置：

```powershell
$env:TUTOR_HOME = 'D:\MyTutor\app-data'
npm start
```

既有使用者可以保留原本資料位置、學生 ID 與紀錄；亦可使用不公開的 `app-data/local-settings.json` 指定本機資料根。資料夾可留在原始碼旁，但仍須排除於版本控制與發布。詳見 [資料位置、傳輸與清除](docs/public/privacy.md)。

課前預覽不會上傳照片；課中 AI 模式會依路由傳送音訊、文字或校正後的照片。API key 使用 Windows 同機 DPAPI 加密，CLI 授權按 Windows 帳號隔離。學習記憶、照片、語音、金鑰及診斷資料應留在自己的資料根。

## 課綱

提供官方課綱下載、頁碼與來源索引，自動擷取條目仍待人工核對，不等於課本全文或已驗證所有年級的教學品質。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/import_curriculum.py
```

下載成果與 SQLite 保存於私有課綱快取；可用 `TUTOR_CURRICULUM_DIR` 指定位置。倉庫包含匯入程式及合成 fixture，不包含作者本機資料庫或第三方課本。

## 開發與驗證

```powershell
npm run build
npm test
.\.venv\Scripts\python.exe -m unittest discover -s server -t .
npm run test:storage
npm run test:first-run
npm run test:ui
npm run test:camera
.\.venv\Scripts\python.exe scripts/audit_public.py
```

自動測試使用合成學生／相機及隔離資料根，不需要真人資料或 API key。需要外部服務、真實相機或可能收費的診斷腳本屬手動測試，見 [參與開發](CONTRIBUTING.md)。

## 文件與後續工作

- [架構](docs/public/architecture.md)
- [資料與隱私](docs/public/privacy.md)
- [發布與稽核](docs/public/releasing.md)
- [安全回報](SECURITY.md)
- [第三方來源與授權](THIRD_PARTY_NOTICES.md)
- [更新紀錄](CHANGELOG.md)

後續：Windows 安裝包、原處升級／回復操作體驗、更多真實服務及教學驗收、課綱條目人工校訂。其他平台須完成裝置與儲存流程驗收後才列入正式支援。

MIT 授權。設計與部分底層實作參考同作者的 [AI_Governess](https://github.com/t214312004/AI_Governess)，本倉庫採獨立乾淨歷史，不包含家庭私有記憶。
