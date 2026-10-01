# 第三方來源與授權

本專案程式使用 MIT；第三方套件、外部服務及自行匯入的教材依各自條件提供。本倉庫不包含出版社課本、原始課綱 PDF、語音模型、Python runtime 或第三方 CLI 執行檔。

設計及 CLI／語音／生命週期實作參考 [AI_Governess](https://github.com/t214312004/AI_Governess/tree/6fb89f1e529b4ad369a87bc80986058b4017f153)，主要涉及 `server/providers.py`、`server/basic.py`、`server/transport.py` 及工作區／白板流程。這份公開版本不依賴上游的家庭資料或私有工作目錄。上游指定版本的程式授權聲明如下；上游素材不納入本倉庫。

```text
MIT License

Copyright (c) 2026 AI Governess contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

前端直接依賴 React、React DOM、lucide-react 及官方 Codex CLI；桌面使用 Electron。後端使用 FastAPI、Starlette、Uvicorn、httpx、edge-tts、python-multipart、pypdf、websockets。精確版本與遞迴依賴見 `package-lock.json` 及 `requirements-lock.txt`；Node 套件授權欄位在 lockfile 中可查。安裝時由各套件來源取得其授權檔，沒有將 node_modules 或 site-packages 再散布在本倉庫。

圖示來源為 lucide-react；展示作業由 `scripts/synthetic-worksheet.mjs` 產生。合成學生、學校及課綱 fixture 由本專案撰寫，不取自真人紀錄。介面使用系統字型，未附字型檔。

課綱匯入程式從 [國家教育研究院](https://www.naer.edu.tw/PageSyllabus?fid=52) 取得官方文件，保留來源、checksum 與頁碼。匯入資料留在使用者快取內，不因本專案 MIT 授權而變更其來源權利。新增素材或發布含第三方執行檔的安裝包前須另外核對再散布條件。
