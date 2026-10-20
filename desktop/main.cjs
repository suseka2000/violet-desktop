'use strict';
const { app, BrowserWindow, Menu, dialog, session, shell, ipcMain, safeStorage } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { BackendProcess } = require('./backend-process.cjs');
const { Credentials } = require('./credentials.cjs');
const { Updates } = require('./updater.cjs');
const { isAppURL, isSafeExternal, authenticatedHeaders, trustedIPC } = require('./security.cjs');

app.setName('Violet');
app.enableSandbox();
if (process.platform === 'win32') app.setAppUserModelId('com.violet.interior-rd');
const hasLock = app.requestSingleInstanceLock();
let window, accessWindow, backend, updates, credentials, logStream;
let origin = null, allowQuit = false, closeAuthorized = false, closePending = false, installOnClose = false;
let refreshTimer, finishClosePromise;
function scheduleUpdates() {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => updates?.check(false), 6*60*60*1000);
  refreshTimer.unref();
}
function log(message) { logStream?.write(`${new Date().toISOString()} ${String(message).slice(0, 16000)}\n`); }
function notify(message) {
  const options = {type:'info', title:'Violet', message, buttons:['Понятно'], noLink:true};
  if (window && !window.isDestroyed()) return dialog.showMessageBox(window, options);
  return dialog.showMessageBox(options);
}
function readReleaseConfig() {
  const filename = app.isPackaged ? path.join(process.resourcesPath, 'release-config.json') : path.join(__dirname, 'stage/release-config.json');
  try { return JSON.parse(fs.readFileSync(filename, 'utf8')); }
  catch (error) { log(`Канал обновлений отключён: ${error.code || 'некорректная конфигурация'}`); return {enabled:false}; }
}
function refreshMenu() {
  const application = [
    {label:'О программе Violet', click:() => notify(`Violet ${app.getVersion()}\nРедактор помещений, монтаж и рабочая документация.\nДанные проектов хранятся локально.`)},
    {type:'separator'},
    {label:updates?.label() || 'Проверить обновления…', click:() => updates?.check(true),
      enabled:!['checking','downloading'].includes(updates?.state)},
    {label:'Установить обновление и перезапустить', enabled:updates?.state === 'downloaded', click:() => requestClose(true)},
    {label:'Доступ к закрытым обновлениям…', click:showAccess},
    {type:'separator'},
    {label:'Открыть папку проектов', click:() => shell.openPath(path.join(app.getPath('userData'), 'workspace'))},
    {label:'Открыть журналы', click:() => shell.openPath(path.join(app.getPath('userData'), 'logs'))},
    {type:'separator'}, {label:'Выйти', accelerator:'CmdOrCtrl+Q', click:() => requestClose(false)}
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label:'Violet', submenu:application},
    {label:'Правка', submenu:[{role:'undo',label:'Отменить'},{role:'redo',label:'Повторить'}, {type:'separator'},
      {role:'cut',label:'Вырезать'},{role:'copy',label:'Копировать'},{role:'paste',label:'Вставить'},{role:'selectAll',label:'Выделить всё'}]},
    {label:'Вид', submenu:[{role:'resetZoom',label:'Обычный масштаб'},{role:'zoomIn',label:'Увеличить'},{role:'zoomOut',label:'Уменьшить'},
      {type:'separator'},{role:'togglefullscreen',label:'Полный экран'}]}
  ]));
}
function secureWindow(win, allowedOrigin, loadingURL = null) {
  win.webContents.on('will-navigate', (event, url) => {
    if (!isAppURL(url, allowedOrigin()) && url !== loadingURL) event.preventDefault();
  });
  win.webContents.on('will-redirect', (event, url) => {
    if (!isAppURL(url, allowedOrigin())) event.preventDefault();
  });
  win.webContents.on('will-attach-webview', event => event.preventDefault());
  win.webContents.setWindowOpenHandler(({url}) => {
    if (isAppURL(url, allowedOrigin())) {
      // CAD/PDF downloads remain in the protected session.
      win.webContents.downloadURL(url);
    } else if (isSafeExternal(url)) {
      dialog.showMessageBox(win, {type:'question', title:'Открыть сайт?', message:new URL(url).hostname,
        detail:'Ссылка откроется в вашем обычном браузере.', buttons:['Отмена','Открыть'], defaultId:0, cancelId:0})
        .then(({response}) => { if (response === 1) shell.openExternal(url); });
    }
    return {action:'deny'};
  });
}
function showAccess() {
  if (accessWindow && !accessWindow.isDestroyed()) { accessWindow.focus(); return; }
  accessWindow = new BrowserWindow({width:560,height:455,parent:window,modal:true,resizable:false,
    title:'Доступ к обновлениям',autoHideMenuBar:true,
    webPreferences:{preload:path.join(__dirname,'access-preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}});
  const url = pathToFileURL(path.join(__dirname, 'access.html')).href;
  secureWindow(accessWindow, () => null, url);
  accessWindow.loadURL(url);
  accessWindow.on('closed', () => { accessWindow = null; });
}
function trustedAccess(event) {
  return Boolean(accessWindow && !accessWindow.isDestroyed() && event.sender === accessWindow.webContents &&
    event.senderFrame === accessWindow.webContents.mainFrame &&
    event.senderFrame.url === pathToFileURL(path.join(__dirname, 'access.html')).href);
}
ipcMain.handle('violet:info', event => {
  if (!trustedIPC(event, window, origin)) throw new Error('Запрос не из приложения');
  return {version:app.getVersion(),platform:process.platform,updatesEnabled:updates?.enabled === true};
});
ipcMain.handle('violet:check-updates', event => {
  if (!trustedIPC(event, window, origin)) throw new Error('Запрос не из приложения');
  return updates?.check(true);
});
ipcMain.handle('violet:access-save', (event, token) => {
  if (!trustedAccess(event)) throw new Error('Запрос не из окна доступа');
  try {
    credentials.save(token);
    accessWindow.close();
    updates?.check(true);
    return {ok:true};
  } catch (error) { return {ok:false,message:error.message}; }
});
ipcMain.on('violet:access-cancel', event => { if (trustedAccess(event)) accessWindow.close(); });

async function requestClose(install) {
  if (closePending || finishClosePromise || allowQuit) return;
  if (install && updates?.state !== 'downloaded') return;
  closePending = true;
  try {
    const health = backend?.child && backend.origin ? await backend.health() : {active_jobs:0};
    if (health.active_jobs > 0) {
      if (install) {
        await notify('Сейчас выполняется расчёт или экспорт. Дождитесь завершения, затем установите обновление.');
        return;
      }
      const {response} = await dialog.showMessageBox(window, {type:'warning',title:'Остановить расчёты?',
        message:'Перед выходом текущие расчёты будут остановлены.',
        detail:'Сохранённые проекты останутся на месте. Незавершённые сборки можно запустить снова.',
        buttons:['Остаться','Остановить и выйти'],defaultId:0,cancelId:0});
      if (response !== 1) return;
    }
    if (install) {
      const {response} = await dialog.showMessageBox(window, {type:'question',title:'Установить обновление?',
        message:`Установить Violet ${updates.version} и перезапустить?`,
        detail:'Сначала сохраните проект. Сохранённые данные находятся отдельно от приложения.',
        buttons:['Позже','Перезапустить'],defaultId:0,cancelId:0});
      if (response !== 1) return;
    }
    installOnClose = install;
    closeAuthorized = true;
    if (window && !window.isDestroyed()) window.close(); // Runs the editor's real beforeunload guard.
    else await finishClose();
  } catch (error) { await notify(error.message); }
  finally { closePending = false; }
}
function finishClose() {
  if (finishClosePromise) return finishClosePromise;
  finishClosePromise = finishCloseOnce().finally(() => { finishClosePromise = null; });
  return finishClosePromise;
}
async function finishCloseOnce() {
  clearInterval(refreshTimer);
  try {
    await backend?.stop({requireIdle:installOnClose});
    allowQuit = true;
    if (installOnClose) require('electron-updater').autoUpdater.quitAndInstall(false, true);
    else app.quit();
  } catch (error) {
    installOnClose = false; closeAuthorized = false;
    await notify(error.message);
    // Do not install over a live backend. Its parent monitor will close it if
    // the OS later terminates this process.
    if (!window || window.isDestroyed()) await createWindow(false);
    scheduleUpdates();
  }
}
async function createWindow(startBackend = true) {
  const appSession = session.fromPartition('persist:violet');
  appSession.setPermissionRequestHandler((_contents,_permission,callback) => callback(false));
  appSession.setPermissionCheckHandler(() => false);
  appSession.webRequest.onBeforeSendHeaders((details, callback) => callback({
    requestHeaders:authenticatedHeaders(details.url, origin, backend?.token || '', details.requestHeaders)
  }));
  appSession.webRequest.onBeforeRequest((details, callback) => {
    // Renderer content cannot contact Internet services; only main-process
    // updater traffic and explicitly opened external browser links may do so.
    const protocol = new URL(details.url).protocol;
    callback({cancel:['http:','https:','ws:','wss:','ftp:'].includes(protocol) && !isAppURL(details.url, origin)});
  });
  window = new BrowserWindow({width:1480,height:960,minWidth:900,minHeight:640,title:'Violet',
    backgroundColor:'#f6f5fc',show:false,
    webPreferences:{session:appSession,preload:path.join(__dirname,'preload.cjs'),nodeIntegration:false,
      contextIsolation:true,sandbox:true,webSecurity:true,allowRunningInsecureContent:false,webviewTag:false}});
  const loadingURL = pathToFileURL(path.join(__dirname,'loading.html')).href;
  secureWindow(window, () => origin, loadingURL);
  window.webContents.on('will-prevent-unload', event => {
    const choice = dialog.showMessageBoxSync(window, {type:'warning',title:'Есть несохранённые изменения',
      message:'Выйти без сохранения черновика или незавершённого построения?',
      buttons:['Вернуться к работе','Выйти без сохранения'],defaultId:0,cancelId:0});
    if (choice === 1) event.preventDefault();
    else { closeAuthorized = false; installOnClose = false; }
  });
  window.on('close', event => {
    if (!closeAuthorized) { event.preventDefault(); requestClose(false); }
  });
  window.on('closed', () => { window = null; finishClose(); });
  window.once('ready-to-show', () => window?.show());
  await window.loadURL(loadingURL);
  if (startBackend) origin = await backend.start();
  if (closeAuthorized || allowQuit || !window || window.isDestroyed()) return;
  await window.loadURL(origin + '/');
}

if (!hasLock) app.quit();
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
  app.on('before-quit', event => { if (!allowQuit) { event.preventDefault(); requestClose(false); } });
  app.on('window-all-closed', () => { if (allowQuit) app.quit(); });
  app.whenReady().then(async () => {
    const userData = app.getPath('userData');
    fs.mkdirSync(path.join(userData,'logs'), {recursive:true});
    logStream = fs.createWriteStream(path.join(userData,'logs',`desktop-${new Date().toISOString().slice(0,10)}.log`), {flags:'a',mode:0o600});
    credentials = new Credentials(userData, safeStorage);
    process.env.VIOLET_DESKTOP_VERSION = app.getVersion();
    backend = new BackendProcess({packaged:app.isPackaged,resourcesPath:process.resourcesPath,
      appPath:app.getAppPath(),dataDir:path.join(userData,'workspace'),log});
    backend.on('unexpected-exit', async () => notify('3D-движок неожиданно остановился. Перезапустите Violet; сохранённые проекты останутся на месте. Подробности находятся в журнале.'));
    try {
      updates = new Updates({updater:require('electron-updater').autoUpdater,config:readReleaseConfig(),
        packaged:app.isPackaged,credentials,notify,changed:refreshMenu,log});
      refreshMenu();
      await createWindow();
      if (closeAuthorized || allowQuit) return;
      updates.check(false);
      scheduleUpdates();
    } catch (error) {
      if (closeAuthorized || allowQuit) return;
      log(error.message);
      await notify(`Не удалось запустить Violet.\n${error.message}`);
      try { await backend.stop(); } catch {}
      allowQuit = true; app.quit();
    }
  });
}
