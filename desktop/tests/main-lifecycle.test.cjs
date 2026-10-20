'use strict';
// Exercise the real main-process lifecycle, replacing Electron and side effects
// only. No app process, HTTP listener, user directory or updater is started.
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise,resolve,reject};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for (let i = 0; i < 8; i++) await tick(); }
function event() { return {defaultPrevented:false,preventDefault() { this.defaultPrevented = true; }}; }

function harness({startup = null, stop = null, activeJobs = 0, response = 1, discard = 0,
  prepareError = null, launchError = null, provider = 'github-private'} = {}) {
  const windows = [], notices = [], stops = [], timers = new Set(), order = [];
  const app = new EventEmitter();
  let menu, credentialConstructors = 0;
  let backend, updates, quitCount = 0, installCount = 0, healthCount = 0, startCount = 0;
  const ready = deferred();
  Object.assign(app, {
    isPackaged:true,setName() {},enableSandbox() {},setAppUserModelId() {},
    requestSingleInstanceLock:() => true,whenReady:() => ready.promise,
    getPath:() => '/isolated-test-user',getVersion:() => '0.4.0',getAppPath:() => '/isolated-app',
    quit() { const e = event(); app.emit('before-quit',e); if (!e.defaultPrevented) { quitCount++; order.push('quit'); } }
  });
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.destroyed = false; this.unsaved = false;
      this.urls = []; this.webContents = new EventEmitter();
      Object.assign(this.webContents, {
        mainFrame:{url:''},setWindowOpenHandler() {},downloadURL() {}
      });
      windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    isMinimized() { return false; }
    show() {}
    focus() {}
    restore() {}
    async loadURL(url) { this.urls.push(url); this.webContents.mainFrame.url = url; }
    close() {
      if (this.destroyed) return;
      const e = event();
      this.emit('close',e);
      if (e.defaultPrevented) return;
      if (this.unsaved) {
        const guard = event();
        this.webContents.emit('will-prevent-unload',guard);
        if (!guard.defaultPrevented) return;
      }
      this.destroyed = true;
      order.push('window-closed');
      this.emit('closed');
      app.emit('window-all-closed');
    }
  }
  class Backend extends EventEmitter {
    constructor() { super(); backend = this; this.child = null; this.origin = null; this.token = 'mock-token'; }
    async start() {
      startCount++; this.child = {};
      if (startup) await startup.promise;
      this.origin = 'http://127.0.0.1:43210';
      return this.origin;
    }
    async health() { healthCount++; return {active_jobs:activeJobs}; }
    async stop(options) {
      stops.push(options); order.push('stop-begin');
      if (stop) await stop(options, this);
      this.child = null; order.push('stop-complete');
    }
  }
  class Updates {
    constructor(options) { updates = this; this.config = options.config; this.state = 'downloaded'; this.version = '0.5.0'; this.enabled = true; this.checks = []; }
    check(manual) { this.checks.push(manual); }
    label() { return 'mock update'; }
    async prepareInstall() {
      if (prepareError) throw prepareError;
      return {prepared:true};
    }
    async launchInstall(plan) {
      assert.equal(plan.prepared,true);
      if (launchError) throw launchError;
      installCount++; order.push('install');
    }
  }
  const electron = {
    app,BrowserWindow:Window,
    Menu:{buildFromTemplate:value => value,setApplicationMenu(value) { menu = value; }},
    dialog:{
      async showMessageBox(...args) {
        const options = args.at(-1); notices.push(options);
        return {response:typeof response === 'function' ? await response(options) : response};
      },
      showMessageBoxSync(...args) { notices.push(args.at(-1)); return discard; }
    },
    session:{fromPartition:() => ({
      setPermissionRequestHandler() {},setPermissionCheckHandler() {},
      webRequest:{onBeforeSendHeaders() {},onBeforeRequest() {}}
    })},
    shell:{openPath() {},openExternal() {}},safeStorage:{},
    ipcMain:{handle() {},on() {}}
  };
  const context = vm.createContext({
    require(name) {
      if (name === 'electron') return electron;
      if (name === './update-install.cjs') return {async acknowledgeUpdateReady() {}};
      if (name === './backend-process.cjs') return {BackendProcess:Backend};
      if (name === './credentials.cjs') return {Credentials:class { constructor() { credentialConstructors++; } }};
      if (name === './updater.cjs') return {Updates};
      if (name === './security.cjs') return require('../security.cjs');
      if (name === 'node:fs') return {
        mkdirSync() {},createWriteStream:() => ({write() {}}),
        readFileSync:() => JSON.stringify({enabled:false,provider})
      };
      return require(name);
    },
    __dirname:path.resolve(__dirname,'..'),URL,console,
    process:{platform:process.platform,resourcesPath:'/isolated-resources',env:{}},
    setInterval(callback) { const timer = {callback,unref() {}}; timers.add(timer); return timer; },
    clearInterval(timer) { timers.delete(timer); }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../main.cjs'),'utf8'),context,{filename:'main.cjs'});
  ready.resolve();
  return {
    app,windows,notices,stops,timers,order,
    menu:() => menu,credentialConstructors:() => credentialConstructors,
    access:() => vm.runInContext('showAccess()',context),
    close:install => vm.runInContext(`requestClose(${Boolean(install)})`,context),
    state:() => vm.runInContext('({closeAuthorized,closePending,installOnClose,allowQuit})',context),
    backend:() => backend,updates:() => updates,
    counts:() => ({quitCount,installCount,healthCount,startCount})
  };
}

test('public channel starts without credential storage or an access-key menu', async () => {
  const h = harness({provider:'github-public'});
  await settle();
  assert.equal(h.credentialConstructors(),0);
  assert.equal(h.menu()[0].submenu.some(item => item.label?.includes('Доступ к закрытым')),false);
  h.access();
  assert.equal(h.windows.length,1);
});

test('private channel retains its protected access-key menu', async () => {
  const h = harness({provider:'github-private'});
  await settle();
  assert.equal(h.credentialConstructors(),1);
  assert.equal(h.menu()[0].submenu.some(item => item.label?.includes('Доступ к закрытым')),true);
  h.access();
  assert.equal(h.windows.length,2);
});

test('beforeunload cancellation keeps window, backend and downloaded update untouched', async () => {
  const h = harness({discard:0});
  await settle();
  h.windows[0].unsaved = true;
  await h.close(true);
  await settle();
  assert.equal(h.windows[0].isDestroyed(),false);
  assert.equal(h.stops.length,0);
  assert.equal(h.counts().installCount,0);
  assert.equal(h.updates().state,'downloaded');
  assert.equal(h.state().closeAuthorized,false);
  assert.equal(h.state().installOnClose,false);
  assert.equal(h.timers.size,1);
});

test('confirmed discard reaches updater only after actual window close and graceful idle stop', async () => {
  const shutdown = deferred();
  const h = harness({discard:1,stop:() => shutdown.promise});
  await settle();
  h.windows[0].unsaved = true;
  await h.close(true);
  await settle();
  assert.equal(h.windows[0].isDestroyed(),true);
  assert.equal(h.stops.length,1);
  assert.equal(h.stops[0].requireIdle,true);
  assert.equal(h.counts().installCount,0);
  shutdown.resolve();
  await settle();
  assert.equal(h.counts().installCount,1);
  assert.deepEqual(h.order,['window-closed','stop-begin','stop-complete','install','quit']);
});

test('two quit gestures during shutdown share one stop and cannot switch an accepted update to plain quit', async () => {
  const shutdown = deferred();
  const h = harness({stop:() => shutdown.promise});
  await settle();
  await h.close(true);
  await settle();
  const secondQuit = event();
  h.app.emit('before-quit',secondQuit);
  await Promise.all([h.close(false),h.close(true)]);
  assert.equal(secondQuit.defaultPrevented,true);
  assert.equal(h.stops.length,1);
  shutdown.resolve();
  await settle();
  assert.equal(h.counts().installCount,1);
  assert.equal(h.counts().quitCount,1);
});

test('plain close can cancel backend startup without asking the nonexistent health endpoint', async () => {
  const startup = deferred();
  const h = harness({startup,stop:() => { startup.reject(new Error('cancelled startup')); }});
  await settle();
  assert.equal(h.windows[0].urls.length,1);
  assert.equal(h.backend().origin,null);
  await h.close(false);
  await settle();
  assert.equal(h.counts().healthCount,0);
  assert.equal(h.stops.length,1);
  assert.equal(h.stops[0].requireIdle,false);
  assert.equal(h.counts().quitCount,1);
  assert.equal(h.counts().installCount,0);
  assert.equal(h.notices.length,0);
  assert.equal(h.timers.size,0);
});

test('atomic busy409 after frontend close reopens existing backend without installing', async () => {
  const h = harness({stop:() => { throw Object.assign(new Error('busy409'),{status:409}); }});
  await settle();
  const backend = h.backend();
  await h.close(true);
  await settle();
  assert.equal(h.windows.length,2);
  assert.equal(h.windows[1].isDestroyed(),false);
  assert.equal(h.windows[1].urls.at(-1),'http://127.0.0.1:43210/');
  assert.equal(h.backend(),backend);
  assert.equal(h.counts().startCount,1);
  assert.equal(h.counts().installCount,0);
  assert.equal(h.counts().quitCount,0);
  assert.equal(h.state().installOnClose,false);
  assert.equal(h.state().allowQuit,false);
  assert.equal(h.stops[0].requireIdle,true);
  assert.ok(h.notices.some(item => item.message === 'busy409'));
});

test('initial busy state defers update without closing anything', async () => {
  const h = harness({activeJobs:2});
  await settle();
  await h.close(true);
  assert.equal(h.windows[0].isDestroyed(),false);
  assert.equal(h.stops.length,0);
  assert.equal(h.counts().installCount,0);
  assert.equal(h.state().closePending,false);
});

test('cancelled update confirmation never prompts to discard drafts', async () => {
  const h = harness({response:0,discard:1});
  await settle();
  h.windows[0].unsaved = true;
  await h.close(true);
  assert.equal(h.notices.length,1);
  assert.equal(h.windows[0].isDestroyed(),false);
  assert.equal(h.stops.length,0);
});

test('concurrent quit while update confirmation is open cannot bypass confirmation', async () => {
  const answer = deferred();
  const h = harness({response:() => answer.promise});
  await settle();
  const update = h.close(true);
  await settle();
  await h.close(false);
  assert.equal(h.notices.length,1);
  assert.equal(h.windows[0].isDestroyed(),false);
  answer.resolve(0);
  await update;
  assert.equal(h.stops.length,0);
  assert.equal(h.counts().quitCount,0);
});

test('invalid update artifact reopens editor without stopping backend or installing', async () => {
  const h = harness({prepareError:new Error('hash mismatch')});
  await settle();
  await h.close(true);
  await settle();
  assert.equal(h.stops.length,0);
  assert.equal(h.windows.length,2);
  assert.equal(h.counts().startCount,1);
  assert.equal(h.counts().installCount,0);
  assert.equal(h.counts().quitCount,0);
  assert.equal(h.state().allowQuit,false);
});

test('installer launch failure restarts stopped backend and restores periodic checks', async () => {
  const h = harness({launchError:new Error('helper could not start')});
  await settle();
  await h.close(true);
  await settle();
  assert.equal(h.stops.length,1);
  assert.equal(h.windows.length,2);
  assert.equal(h.counts().startCount,2);
  assert.equal(h.counts().installCount,0);
  assert.equal(h.counts().quitCount,0);
  assert.equal(h.state().allowQuit,false);
  assert.equal(h.timers.size,1);
});
