'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {BackendProcess, launchArguments, backendEnvironment} = require('../backend-process.cjs');

test('frozen backend uses resources and separate user directory without source paths or secrets in arguments', () => {
  for (const platform of ['darwin','win32']) {
    const launch = launchArguments({packaged:true,resourcesPath:'/installed/resources',dataDir:'/user/workspace',platform});
    assert.equal(launch.command, `/installed/resources/backend/violet-backend${platform === 'win32' ? '.exe' : ''}`);
    assert.deepEqual(launch.args,['--data-dir','/user/workspace','--parent-pid',String(process.pid)]);
    assert.equal(launch.cwd, undefined);
  }
});

test('sidecar does not inherit publisher or access secrets', () => {
  const env = backendEnvironment({PATH:'/os/bin',TEMP:'/tmp',VIOLET_DESKTOP_VERSION:'0.4.0',
    GH_TOKEN:'secret',GITHUB_TOKEN:'secret',CSC_KEY_PASSWORD:'secret',APPLE_APP_SPECIFIC_PASSWORD:'secret',
    AWS_SECRET_ACCESS_KEY:'secret',ELECTRON_RUN_AS_NODE:'1'}, 'session-secret');
  assert.deepEqual(env,{PATH:'/os/bin',TEMP:'/tmp',VIOLET_DESKTOP_VERSION:'0.4.0',
    VIOLET_DESKTOP_TOKEN:'session-secret',PYTHONUNBUFFERED:'1',PYTHONUTF8:'1'});
});

function fakeBackend() {
  const backend = new BackendProcess({dataDir:'/test-only'});
  const child = new EventEmitter();
  let kills = 0;
  child.kill = () => { kills++; queueMicrotask(() => child.emit('exit',0)); };
  backend.child = child;
  return {backend,child,kills:() => kills};
}

test('update busy rejection never kills the running backend and remains retryable', async () => {
  const {backend,child,kills} = fakeBackend();
  backend.request = async (endpoint,method,body) => {
    assert.deepEqual(body,{require_idle:true});
    throw Object.assign(new Error('busy'),{status:409});
  };
  await assert.rejects(backend.stop({requireIdle:true}),/busy/);
  assert.equal(kills(),0);
  assert.equal(backend.stopping,false);
  assert.equal(child.listenerCount('exit'),0);
  backend.request = async () => { setImmediate(() => child.emit('exit',0)); return {closing:true}; };
  await backend.stop({requireIdle:true});
  assert.equal(kills(),0);
});

test('concurrent close requests share one graceful shutdown', async () => {
  const {backend,child,kills} = fakeBackend();
  let requests = 0;
  backend.request = async () => { requests++; setImmediate(() => child.emit('exit',0)); return {}; };
  await Promise.all([backend.stop({requireIdle:true}),backend.stop({requireIdle:true})]);
  assert.equal(requests,1);
  assert.equal(kills(),0);
});

test('plain quit can cancel startup before health endpoint exists', async () => {
  const {backend,kills} = fakeBackend();
  await backend.stop();
  assert.equal(kills(),1);
});
