# 資料位置、傳輸與清除

公開新安裝預設資料根為 `%LOCALAPPDATA%\AI-Tutor\`；可以設定絕對路徑 `TUTOR_HOME`。資料根下的 `data` 保存 registry、學生 SQLite、課堂工作區與復盤紀錄；`browser` 保存 Electron 個人檔與鏡頭／介面偏好；`accounts` 保存依 Windows 帳號隔離的 Codex 授權；`temp` 保存暫存。

`TUTOR_DATA_DIR` 可覆寫學生資料位置，`TUTOR_CURRICULUM_DIR` 可覆寫課綱快取。兩者與 TUTOR_HOME 應一致設定；一般使用只需設定 TUTOR_HOME。明確設定錯誤時程式報錯，不切換到另一套資料。

開發目錄可使用被 Git 排除的 `app-data/local-settings.json`：

```json
{
  "home": "D:\\MyTutor\\app-data",
  "curriculum_dir": "D:\\MyTutor\\curriculum"
}
```

設定的 home 必須是現有資料夾。環境變數優先於這份私有設定；測試模式忽略它。資料可以與原始碼留在同一目錄，但不得進入 Git、Release 或診斷附件。

| 路由 | 會傳送的資料 |
|---|---|
| 本機專注工作區 | 未設定 AI 服務時不送 AI 教學請求；課程紀錄仍在本機保存 |
| 基本陪讀 | 語音送 Groq 轉錄，課堂文字及可讀照片工作區交給所選 Codex／agy；教學語句交給 Edge TTS |
| OpenAI Live | 麥克風音訊與即時工具／文字送 OpenAI；校正後照片交給所選圖片服務，教學脈絡交給所選委派路由 |
| Gemini Live | 麥克風與校正後照片送 Gemini；需要文字教學／白板時使用已接線的 CLI 路由 |

課前鏡頭預覽本身不建立課堂或上傳影像。試聽聲音會使用 Edge TTS 網路服務。API 使用及訂閱費用由使用者負擔；服務方的保存及使用政策以各自最新條款為準。

API 金鑰使用 Windows **同機 DPAPI** 保存於 `credentials.machine`。這不是每帳號隔離；目錄 ACL 仍重要。Codex 授權資料按 Windows SID 分開。若選家庭共用資料根，其他獲授權使用者可看到學生紀錄；請自行設定相應的檔案存取權限。

一般連線診斷只記狀態、錯誤類別與耗時，輪替並在啟動／運作時清除過期檔案。測試學生的逐堂 audit 則可能包含完整提示、模型回覆、照片及工作區副本，即使名字寫「測試」也可能包含實際輸入，不適合公開上傳。一般學生的課程照片／工作區也可能持久保存；不要以 audit 的 72 小時清理推定全部學生資料都會到期。

家長介面提供清除學生學習紀錄；這不代表供應商端、外部備份、登入資料及鏡頭設定一併刪除。完整移除本機資料前應先關閉程式並備份，依資料類別選擇清除。備份 SQLite 時請用一致性備份或關閉程式後連同 WAL／SHM 妥善處理。

公開預設老師是「伴讀老師」。既有個人設定可留在 `data/bootstrap.json` 的 teacher_profiles；每堂課亦可調整老師及聲音。此初始化檔屬私有設定，程式不以其內容反覆覆蓋已修改的學生檔案或學校背景。
