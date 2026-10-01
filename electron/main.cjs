const {
  app,
  BrowserWindow,
  WebContentsView,
  ipcMain,
  screen,
  session,
  powerMonitor,
  dialog,
} = require('electron');
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

app.setName('HomeworkCompanion');
const storage = require('./shared-storage.cjs');
const testing = process.argv.some((arg) =>
  ['--smoke', '--device-probe', '--test-mode'].includes(arg),
);
const dataHome = storage.configure(app, testing);
const nativePhoto = require('./native-photo.cjs').createNativePhoto({
  testing: testing && !process.argv.includes('--native-camera-probe'),
});
const diagnosticPath = path.join(dataHome, 'desktop.log');
const diagnosticEvents = new Set([
  'core_ready', 'core_exit', 'core_spawn_error', 'socket_lost', 'socket_retry',
  'socket_restored', 'socket_recovery_failed', 'camera_error', 'microphone_error',
  'media_suspend', 'session_start', 'session_end',
  'photo_upload', 'transport_backpressure',
  'native_photo',
]);
const diagnosticDayMs = 24 * 60 * 60 * 1000;
const diagnosticRetentionMs = 3 * diagnosticDayMs;
function pruneDiagnosticFiles() {
  const cutoff = Date.now() - diagnosticRetentionMs;
  try {
    for (const name of fs.readdirSync(dataHome)) {
      if (!/^desktop\.log(?:\.[12])?\.[0-9a-f]{16}\.pruning$/.test(name)) continue;
      const leftover = path.join(dataHome, name);
      if (!fs.lstatSync(leftover).isSymbolicLink() && fs.statSync(leftover).mtimeMs <= cutoff)
        fs.rmSync(leftover, { force: true });
    }
  } catch (error) {
    console.error('Unable to prune desktop diagnostic leftovers:', error?.name);
  }
  for (const file of [diagnosticPath, `${diagnosticPath}.1`, `${diagnosticPath}.2`]) {
    try {
      if (!fs.existsSync(file) || fs.lstatSync(file).isSymbolicLink()) continue;
      const fallback = fs.statSync(file).mtimeMs;
      const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean);
      const kept = lines.filter((line) => {
        const parsed = Date.parse(line.slice(0, 24));
        return (Number.isNaN(parsed) ? fallback : parsed) > cutoff;
      });
      if (kept.length === lines.length) continue;
      if (!kept.length) fs.rmSync(file, { force: true });
      else {
        const replacement = `${file}.${randomBytes(8).toString('hex')}.pruning`;
        try {
          fs.writeFileSync(replacement, `${kept.join('\n')}\n`, { encoding: 'utf8', flag: 'wx' });
          fs.renameSync(replacement, file);
        } finally {
          fs.rmSync(replacement, { force: true });
        }
      }
    } catch (error) {
      console.error('Unable to prune desktop diagnostics:', error?.name);
    }
  }
}
function writeDiagnostic(event, detail = '') {
  if (!diagnosticEvents.has(event)) return;
  const safeDetail = String(detail).slice(0, 40).replace(/[^A-Za-z0-9_-]/g, '_');
  try {
    pruneDiagnosticFiles();
    if (fs.existsSync(diagnosticPath) && fs.lstatSync(diagnosticPath).isSymbolicLink())
      throw Error('Desktop diagnostic path is a link');
    if (fs.existsSync(diagnosticPath) &&
        (fs.statSync(diagnosticPath).size > 1_000_000 ||
         Date.now() - fs.statSync(diagnosticPath).birthtimeMs >= diagnosticDayMs)) {
      fs.rmSync(`${diagnosticPath}.2`, { force: true });
      for (let index = 2; index >= 1; index--) {
        const previous = index === 1 ? diagnosticPath : `${diagnosticPath}.${index - 1}`;
        const next = `${diagnosticPath}.${index}`;
        if (fs.existsSync(previous)) fs.renameSync(previous, next);
      }
    }
    fs.appendFileSync(diagnosticPath, `${new Date().toISOString()} ${event} ${safeDetail}\n`, 'utf8');
  } catch (error) {
    console.error('Unable to write desktop diagnostics:', error?.name);
  }
}
if (!app.requestSingleInstanceLock()) app.exit(0);
else {
  pruneDiagnosticFiles();
  setInterval(pruneDiagnosticFiles, 60 * 1000).unref();
}
const root = storage.root;
let main,
  child,
  connection,
  board,
  boardTimer,
  focusing = false,
  masks = [],
  exiting = false;
let finalizing = false;
let closeAllowed = false;
const trustedURL = pathToFileURL(path.join(root, 'dist/index.html')).href;
const smoke = process.argv.includes('--smoke');
const diagnostic = process.argv.includes('--device-probe');
function trusted(event) {
  if (
    !main ||
    event.sender !== main.webContents ||
    event.senderFrame !== main.webContents.mainFrame
  )
    throw Error('Untrusted sender');
}
function closeBoard() {
  clearTimeout(boardTimer);
  if (board) {
    main?.contentView.removeChildView(board);
    board.webContents.close();
    board = null;
  }
}
function masksClose() {
  for (const win of masks) if (!win.isDestroyed()) win.destroy();
  masks = [];
}
function focus(enabled) {
  if (!main || main.isDestroyed()) return;
  const changed = focusing !== enabled;
  focusing = enabled;
  masksClose();
  main.setFullScreen(enabled);
  if (enabled) {
    const primary = screen.getPrimaryDisplay();
    main.setBounds(primary.bounds);
    for (const display of screen.getAllDisplays().filter((d) => d.id !== primary.id)) {
      const win = new BrowserWindow({
        ...display.bounds,
        frame: false,
        fullscreen: true,
        skipTaskbar: true,
        show: false,
        backgroundColor: '#edf0e8',
        webPreferences: { sandbox: true, nodeIntegration: false, contextIsolation: true },
      });
      attachKeys(win.webContents);
      win.loadFile(path.join(__dirname, 'focus.html'));
      win.once('ready-to-show', () => win.showInactive());
      masks.push(win);
    }
  }
  if (changed) main.webContents.send('focus:changed', enabled);
}
function attachKeys(contents) {
  contents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && (input.key === 'Escape' || input.key === 'F11')) {
      event.preventDefault();
      focus(input.key === 'Escape' ? false : !focusing);
    }
  });
}
function loadCore() {
  return new Promise((resolve, reject) => {
    const python = path.join(root, '.venv/Scripts/python.exe');
    if (!fs.existsSync(python))
      return reject(Error('請先執行 scripts/setup.ps1 建立 Python 執行環境。'));
    const token = randomBytes(32).toString('hex');
    child = spawn(python, ['-m', 'server.app'], {
      cwd: root,
      env: {
        ...process.env,
        TUTOR_CORE_TOKEN: token,
        TUTOR_PARENT_WATCH: '1',
        TUTOR_HOME: dataHome,
        TUTOR_DATA_DIR: smoke ? path.join(root, '.local/smoke-data') : storage.locations.data,
        TUTOR_CURRICULUM_DIR: storage.locations.curriculum,
        TUTOR_BOOTSTRAP_FILE: testing && process.env.TUTOR_TEST_EMPTY !== '1' ? path.join(root, 'examples/demo.json') : '',
        TUTOR_TEST_MODE: testing ? '1' : '0',
      },
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    const timer = setTimeout(() => reject(Error('本機核心啟動逾時')), 20000);
    child.stdout.on('data', (chunk) => {
      output += chunk;
      const line = output.split('\n')[0];
      try {
        const ready = JSON.parse(line);
        if (ready.port) {
          clearTimeout(timer);
          connection = { base: `http://127.0.0.1:${ready.port}`, token };
          writeDiagnostic('core_ready');
          resolve(connection);
        }
      } catch {}
    });
    child.stderr.on('data', (chunk) => process.stderr.write(chunk));
    child.on('error', (error) => {
      writeDiagnostic('core_spawn_error', error?.name);
      reject(error);
    });
    child.on('exit', (code, signal) => {
      writeDiagnostic('core_exit', exiting ? 'expected' : code ?? signal ?? 'unknown');
      clearTimeout(timer);
      reject(Error('本機核心無法啟動；請確認其他 Windows 帳號已關閉伴讀。'));
      if (!exiting) {
        connection = undefined;
        main?.webContents.send('media:suspend', 'core');
        focus(false);
      }
    });
  });
}
async function restoreKeys() {
  const keys = storage.readKeys(dataHome);
  if (Object.keys(keys).length) {
    await fetch(connection.base + '/credentials', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + connection.token, 'Content-Type': 'application/json' },
      body: JSON.stringify(keys),
    });
  }
}
app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback) =>
    callback(contents === main?.webContents && permission === 'media'),
  );
  session.defaultSession.setPermissionCheckHandler(
    (contents, permission) => contents === main?.webContents && permission === 'media',
  );
  main = new BrowserWindow({
    width: 1380,
    height: 900,
    minWidth: 940,
    minHeight: 640,
    show: false,
    backgroundColor: '#f5f4ef',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  attachKeys(main.webContents);
  main.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  main.webContents.on('will-navigate', (e, url) => {
    if (url !== trustedURL) e.preventDefault();
  });
  main.on('leave-full-screen', () => {
    if (focusing) focus(false);
  });
  main.on('closed', () => {
    masksClose();
    app.quit();
  });
  main.on('close', (event) => {
    if (!finalizing || closeAllowed) return;
    event.preventDefault();
    const choice = dialog.showMessageBoxSync(main, {
      type: 'warning',
      title: '課後整理進行中',
      message: '老師正在整理學習資料',
      detail: '現在關閉，整理可能要等下次開啟程式才能完成。若尚未確認紀錄已保存，最後資料可能遺失。',
      buttons: ['繼續等待', '仍要關閉'],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
    if (choice === 1) {
      closeAllowed = true;
      main.close();
    }
  });
  ipcMain.on('finalization:guard-sync', (event, value) => {
    trusted(event);
    finalizing = value === true;
    event.returnValue = true;
  });
  main.on('unresponsive', () => focus(false));
  ipcMain.handle('core:connection', async (event) => {
    trusted(event);
    return connection;
  });
  ipcMain.handle('camera:photo', async (event, options) => {
    trusted(event);
    writeDiagnostic('native_photo', 'start');
    try {
      const photo = await nativePhoto.take(options);
      writeDiagnostic('native_photo', `${photo.width}x${photo.height}`);
      return photo;
    } catch (error) {
      writeDiagnostic('native_photo', 'error');
      throw error;
    }
  });
  ipcMain.handle('camera:cancel-photo', (event) => {
    trusted(event);
    nativePhoto.cancel();
  });
  if (testing && !process.argv.includes('--native-camera-probe')) ipcMain.handle('camera:test-photo', (event, image) => {
    trusted(event);
    nativePhoto.setFixture(image);
  });
  ipcMain.handle('diagnostics:write', (event, name, detail) => {
    trusted(event);
    writeDiagnostic(name, detail);
  });
  ipcMain.handle('focus:set', (event, value) => {
    trusted(event);
    focus(value);
  });
  ipcMain.handle('keys:save', async (event, keys) => {
    trusted(event);
    const allowed = storage.readKeys(dataHome);
    for (const key of ['GROQ_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY']) {
      if (typeof keys[key] !== 'string' || keys[key].length > 300) throw Error('無效金鑰');
      if (keys[key].trim()) allowed[key] = keys[key].trim();
    }
    storage.writeKeys(allowed, dataHome);
    const res = await fetch(connection.base + '/credentials', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + connection.token, 'Content-Type': 'application/json' },
      body: JSON.stringify(allowed),
    });
    if (!res.ok) throw Error('核心未儲存金鑰');
    return true;
  });
  ipcMain.handle('board:close', (event) => {
    trusted(event);
    closeBoard();
  });
  ipcMain.handle('board:bounds', (event, bounds) => {
    trusted(event);
    if (!board) return;
    for (const key of ['x', 'y', 'width', 'height'])
      if (!Number.isFinite(bounds[key]) || bounds[key] < 0 || bounds[key] > 20000)
        throw Error('無效白板範圍');
    board.setBounds(
      Object.fromEntries(
        Object.entries(bounds)
          .filter(([key]) => ['x', 'y', 'width', 'height'].includes(key))
          .map(([key, value]) => [key, Math.round(value)]),
      ),
    );
  });
  ipcMain.handle('board:show', (event, html, bounds) => {
    trusted(event);
    if (typeof html !== 'string' || html.length > 200000) throw Error('白板內容過大');
    for (const k of ['x', 'y', 'width', 'height'])
      if (!Number.isFinite(bounds[k]) || bounds[k] < 0) throw Error('白板範圍錯誤');
    closeBoard();
    board = new WebContentsView({
      webPreferences: {
        sandbox: true,
        nodeIntegration: false,
        contextIsolation: true,
        disableDialogs: true,
        navigateOnDragDrop: false,
        partition: 'board-' + randomBytes(8).toString('hex'),
      },
    });
    board.webContents.session.setPermissionRequestHandler((_, __, cb) => cb(false));
    board.webContents.session.setPermissionCheckHandler(() => false);
    board.webContents.session.on('will-download', (event) => event.preventDefault());
    board.webContents.session.webRequest.onBeforeRequest((details, cb) =>
      cb({ cancel: !details.url.startsWith('data:') && !details.url.startsWith('blob:') }),
    );
    board.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    board.webContents.on('will-navigate', (e) => e.preventDefault());
    attachKeys(board.webContents);
    main.contentView.addChildView(board);
    board.setBounds(Object.fromEntries(Object.entries(bounds).map(([k, v]) => [k, Math.round(v)])));
    const policy =
      "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src 'none'; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'";
    const doc = `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${policy}"><meta charset="utf-8"><style>body{font-family:Microsoft JhengHei,sans-serif;color:#203d32;padding:32px;background:#fffefa}button{font:inherit;padding:12px}</style></head><body>${html}</body></html>`;
    board.webContents.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(doc));
    const watched = board;
    boardTimer = setInterval(() => {
      if (board !== watched) return;
      Promise.race([
        watched.webContents.executeJavaScript('1'),
        new Promise((_, reject) => setTimeout(() => reject(Error('timeout')), 1500)),
      ]).catch(() => {
        if (board === watched) closeBoard();
      });
    }, 3000);
    return true;
  });
  screen.on('display-added', () => {
    if (focusing) focus(true);
  });
  screen.on('display-removed', () => {
    if (focusing) focus(true);
  });
  screen.on('display-metrics-changed', () => {
    if (focusing) focus(true);
  });
  for (const name of ['suspend', 'lock-screen'])
    powerMonitor.on(name, () => {
      main.webContents.send('media:suspend', name);
      focus(false);
    });
  try {
    await loadCore();
    await restoreKeys();
    try {
      const pending = await fetch(connection.base + '/finalizations/pending', {
        headers: { Authorization: 'Bearer ' + connection.token },
      });
      if (pending.ok) finalizing = (await pending.json()).items.length > 0;
    } catch {}
    await main.loadFile(path.join(root, 'dist/index.html'));
    if (!smoke && !diagnostic && !process.argv.includes('--background-diagnostic')) main.show();
    if (smoke || diagnostic) {
      setTimeout(async () => {
        try {
          const result = await main.webContents.executeJavaScript(
            diagnostic
              ? `(async()=>{const result={};for(const kind of ['video','audio']){try{const s=await navigator.mediaDevices.getUserMedia({[kind]:true});result[kind]=s.getTracks().map(t=>({kind:t.kind,label:t.label,settings:t.getSettings()}));s.getTracks().forEach(t=>t.stop());}catch(e){result[kind]={error:e.name};}}return result;})()`
              : `({title:document.title,body:document.body.innerText,bridge:!!window.desktop})`,
          );
          console.log(JSON.stringify({ diagnostic, result }));
          app.quit();
        } catch (e) {
          console.error(e);
          app.exit(1);
        }
      }, 1500);
    }
  } catch (e) {
    console.error(e.message);
    const { dialog } = require('electron');
    if (!smoke) dialog.showErrorBox('伴讀無法啟動', e.message);
    app.exit(1);
  }
});
app.on('second-instance', () => {
  main?.show();
  main?.focus();
});
app.on('before-quit', () => {
  exiting = true;
  nativePhoto.cancel();
  closeBoard();
  masksClose();
  child?.stdin.end();
  child?.kill();
});
app.on('window-all-closed', () => app.quit());
