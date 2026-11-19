# AI Tutor｜伴讀

Windows 上的開源家庭作業陪讀助手，結合書桌相機、語音、教學白板與可修正的學習記憶。繁體中文優先，供家長陪同試用。

桌面提供兩個訂閱語音模式。課堂流程、白板、作業清單與照片索引共用一套程式。

| 模式 | 登入 | 驗證範圍 |
|---|---|---|
| Codex Live | ChatGPT／Codex | 真實訂閱合成語音、照片與白板流程已測 |
| agy Live | Google／Antigravity | 桌面以真實訂閱及合成輸入驗證雙向語音、獨立 ACP 看圖、白板確認及停播重連。實體裝置與長課堂待驗收 |

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

1. 新安裝會顯示空白學生名單，可新增學生或載入合成展示資料。
2. 選 Codex Live 或 agy Live。Codex 可用 [登入腳本](scripts/login-codex.ps1)。使用 agy 時先安裝官方 CLI，從桌面按「agy 首次設定」，在開啟的視窗自行完成選擇及必要的登入。進入主畫面後輸入「/exit」返回桌面。
3. 依需要校正相機，選擇「檢查訂閱並開始陪讀」。只有所選的訂閱服務會處理這堂課。
4. 照片辨識完成後，Codex 的課堂脈絡更新短引用。agy 在下一次原生語音交接時取得短引用。老師需要時才查詢辨識內容，agy 的白板提案會在課堂畫面等待確認。
5. 可查看舊課堂、作業和學習記憶。具備有效檢查點的中斷課堂可從已提交資料接續。舊模式接續時會使用課前選定的訂閱語音。

在「家長設定 → 學習紀錄 → 練習與獨立作答」可選用短檢查、核對教學示例，以及補記／複核孩子的作答。短檢查預設關閉，啟用後仍先徵求孩子同意；每次一兩題，結束時立即停止。AI 紀錄先列待核對，沒有觀察到不算答錯，也不自動判定已掌握。詳見 [陪讀與教學驗收](docs/public/teaching-validation.md)。

## 資料位置

預設私有資料在 `%LOCALAPPDATA%\AI-Tutor\`，**不隨程式碼上傳 GitHub**。可自訂位置：

```powershell
$env:TUTOR_HOME = 'D:\MyTutor\app-data'
npm start
```

既有使用者可以保留原本資料位置、學生 ID 與紀錄；亦可使用不公開的 `app-data/local-settings.json` 指定本機資料根。資料夾可留在原始碼旁，但仍須排除於版本控制與發布。詳見 [資料位置、傳輸與清除](docs/public/privacy.md)。

課前預覽只在本機顯示。課堂音訊與文字交所選的語音服務，原圖交同一訂閱的獨立看圖角色。程式不讀取舊版保存的 API 金鑰。

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
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s server -t .
npm run test:storage
npm run test:first-run
npm run test:native-voice-ui
npm run test:ui
npm run test:lifecycle
npm run test:post-lesson
npm run test:learning-memory
npm run test:usability
npm run test:ui-polish
npm run test:camera
.\.venv\Scripts\python.exe scripts/audit_public.py
```

自動測試使用合成學生／相機及隔離資料根，不需要真人資料或 API key。需要外部服務、真實相機或可能收費的診斷腳本屬手動測試，見 [參與開發](CONTRIBUTING.md)。

桌面測試腳本共用預設測試資料根，請依序執行。

## 文件與後續工作

- [架構](docs/public/architecture.md)
- [Codex 訂閱語音使用方式與驗證範圍](docs/public/native-subscription-voice.md)
- [資料與隱私](docs/public/privacy.md)
- [發布與稽核](docs/public/releasing.md)
- [安全回報](SECURITY.md)
- [第三方來源與授權](THIRD_PARTY_NOTICES.md)
- [更新紀錄](CHANGELOG.md)

後續：Windows 安裝包、原處升級／回復操作體驗、更多真實服務及教學驗收、課綱條目人工校訂。其他平台須完成裝置與儲存流程驗收後才列入正式支援。

MIT 授權。設計與部分底層實作參考同作者的 [AI_Governess](https://github.com/t214312004/AI_Governess)，本倉庫採獨立乾淨歷史，不包含家庭私有記憶。
