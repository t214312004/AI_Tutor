# 架構與資料邊界

Electron 是桌面與裝置入口，React 負責課前、陪讀、家長設定與學習記憶介面。Electron 啟動 Python FastAPI 核心並透過隨機 bearer token 連接 loopback HTTP／WebSocket；核心管理學生、課堂、影像處理與供應商生命週期。

`server/storage.py` 維護學生 registry 及各自的 SQLite 學習資料。`basic_workspace.py`、`live_runtime.py` 與 `post_lesson.py` 管理工作區、即時教學及課後整理。白板由隔離 web contents 顯示，學生資料不應由其他學生的課堂工作區讀取。

`server/providers.py` 實作 Codex／agy adapter；`server/live.py` 接即時音訊；`openai_vision.py` 接圖片辨識。供應商可用性、登入、模型及 effort 依實際設定檢查；功能接線不代表每個模型或教學情境均已驗收。

`electron/storage-paths.cjs` 與 `server/paths.py` 解析資料位置。桌面端先解析並把 TUTOR_HOME、TUTOR_DATA_DIR、TUTOR_CURRICULUM_DIR 傳給核心，避免兩邊使用不同資料根。資料庫新增、schema 升級及課程中斷恢復由 Store 管理。

公開新安裝不自動產生學生或學校；家長新增學生後開始使用。合成 demo 只可明確載入空資料庫。`examples/demo.json` 與 `server/test_support.py` 僅含虛構資料；自動測試明確指定獨立資料根，不讀作者的家庭設定或課綱資料庫。

課綱匯入保留來源 URL、hash、頁碼及解析狀態；候選條目自動擷取不等於人工核對或教學品質驗證。課綱 cache 缺少時 UI 顯示尚未匯入，不要求使用者取得作者本機資料。
