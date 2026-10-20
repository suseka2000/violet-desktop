'use strict';
const assert = require('node:assert/strict');
const {generateKeyPairSync} = require('node:crypto');
const test = require('node:test');
const {Updates} = require('../updater.cjs');
const {authenticatedHeaders, trustedIPC, validateReleaseConfig} = require('../security.cjs');

const publicKeyPem = generateKeyPairSync('ed25519').publicKey.export({type:'spki',format:'pem'});
const releaseConfig = {enabled:true,provider:'github-private',owner:'Violet-Example',repo:'desktop',
  channel:'stable',updateProtocol:'violet-ed25519-v1',publicKeyPem};
function fakeUpdates({packaged = true, config, result = null, downloadError = null} = {}) {
  const calls = [], notices = [], logs = [];
  let clientOptions;
  const updates = new Updates({
    packaged,currentVersion:'0.4.0',cacheDir:'/isolated-test-cache',
    config: config || releaseConfig,
    clientFactory:options => {
      clientOptions = options;
      return {
        async check() { calls.push(['check']); return result; },
        async download(info) {
          assert.equal(info,result);
          calls.push(['download']);
          if (downloadError) throw downloadError;
          return '/isolated-test-cache/update.zip';
        }
      };
    },
    installer:{
      async prepareUpdate(options) { calls.push(['prepare',options]); return {plan:true}; },
      async launchUpdate(plan) { calls.push(['launch',plan]); }
    },
    credentials: {read: () => 'github_pat_personal_fake_test_token'},
    notify: message => notices.push(message), log: message => logs.push(message)
  });
  return {updates, calls, notices, logs,options:() => clientOptions};
}

test('verified update downloads but cannot silently install on ordinary app quit', async () => {
  const {updates,calls} = fakeUpdates({result:{version:'0.4.1'}});
  await updates.check();
  assert.equal(updates.state,'downloaded');
  assert.deepEqual(calls,[['check'],['download']]);
  const plan = await updates.prepareInstall({userData:'/isolated-user'});
  assert.equal(calls[2][1].info.version,'0.4.1');
  assert.equal(calls[2][1].file,'/isolated-test-cache/update.zip');
  assert.equal(calls[2][1].token,undefined);
  await updates.launchInstall(plan);
  assert.equal(calls[3][0],'launch');
});

test('only the per-user private repository token is used, never ambient publishing credentials', async () => {
  const original = {GH_TOKEN:process.env.GH_TOKEN,GITHUB_TOKEN:process.env.GITHUB_TOKEN};
  process.env.GH_TOKEN = 'publishing_secret_test_only';
  process.env.GITHUB_TOKEN = 'another_publishing_secret_test_only';
  try {
    const {updates, options} = fakeUpdates();
    await updates.check(true);
    assert.equal(options().token, 'github_pat_personal_fake_test_token');
    assert.equal(options().config.provider, 'github-private');
    assert.equal(options().config.publicKeyPem,publicKeyPem);
    assert.equal(process.env.GH_TOKEN, 'publishing_secret_test_only');
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test('public updates do not read or require an encrypted credential store', async () => {
  const {updates, options, calls} = fakeUpdates({
    config:{...releaseConfig,provider:'github-public'},result:{version:'0.4.1'}
  });
  updates.credentials = {read() { throw new Error('Public updates must not touch credentials'); }};
  await updates.check(true);
  assert.equal(updates.state,'downloaded');
  assert.equal(options().token,undefined);
  assert.equal(options().config.provider,'github-public');
  assert.deepEqual(calls,[['check'],['download']]);
});

test('public update errors never ask for invitations or access tokens', async () => {
  const {updates, notices} = fakeUpdates({config:{...releaseConfig,provider:'github-public'},
    result:{version:'0.4.1'},downloadError:new Error('rate limited')});
  updates.credentials = null;
  await updates.check(true);
  assert.equal(updates.state,'error');
  assert.doesNotMatch(notices.join('\n'),/персональный|ключ доступа|приглашение/);
});

test('private provider error details cannot disclose access secrets in logs or notifications', async () => {
  const {updates, logs, notices} = fakeUpdates({result:{version:'0.4.1'},
    downloadError:new Error('authorization: token github_pat_personal_fake_test_token')});
  await updates.check(true);
  assert.equal(updates.state, 'error');
  assert.equal(logs.length, 1);
  assert.equal(notices.length, 1);
  assert.doesNotMatch([...logs, ...notices].join('\n'), /github_pat|authorization:/);
});

test('preview build neither checks feed nor reads credentials even when configured', async () => {
  const {updates, calls} = fakeUpdates({packaged:false});
  updates.credentials.read = () => { throw new Error('Do not read credentials in development'); };
  await updates.check(true);
  assert.equal(updates.state, 'disabled');
  assert.deepEqual(calls, []);
});

test('missing per-user key does not use an ambient publisher token', async () => {
  const {updates, calls} = fakeUpdates();
  updates.credentials.read = () => null;
  await updates.check(true);
  assert.equal(updates.state, 'access-required');
  assert.deepEqual(calls, []);
});

test('redirected requests cannot retain the backend session credential', () => {
  const headers = {'x-VIOLET-Desktop-Token':'renderer_value',Accept:'image/png'};
  assert.deepEqual(authenticatedHeaders('https://example.com/picture', 'http://127.0.0.1:4321', 'secret', headers),
    {Accept:'image/png'});
  assert.deepEqual(authenticatedHeaders('http://127.0.0.1:4321/image', 'http://127.0.0.1:4321', 'secret', headers),
    {Accept:'image/png','X-Violet-Desktop-Token':'secret'});
  assert.deepEqual(authenticatedHeaders('http://127.0.0.1:4322/image', 'http://127.0.0.1:4321', 'secret', headers),
    {Accept:'image/png'});
});

test('IPC trust requires the specific current main frame, not merely an origin match', () => {
  const mainFrame = {url:'http://127.0.0.1:4321/'};
  const webContents = {mainFrame};
  const window = {webContents,isDestroyed:() => false};
  assert.equal(trustedIPC({sender:webContents,senderFrame:mainFrame}, window, 'http://127.0.0.1:4321'), true);
  assert.equal(trustedIPC({sender:webContents,senderFrame:{...mainFrame}}, window, 'http://127.0.0.1:4321'), false);
  assert.equal(trustedIPC({sender:{},senderFrame:mainFrame}, window, 'http://127.0.0.1:4321'), false);
  mainFrame.url = 'https://example.com/';
  assert.equal(trustedIPC({sender:webContents,senderFrame:mainFrame}, window, 'http://127.0.0.1:4321'), false);
});

test('release config cannot introduce an HTTP feed or embed credentials in its URL', () => {
  for (const feedUrl of ['http://updates.example.com/', 'https://u:p@updates.example.com/', 'https://updates.example.com/?token=secret']) {
    assert.throws(() => validateReleaseConfig({enabled:true,feedUrl,channel:'stable'}));
  }
});

test('private updates require the pinned Ed25519 public key and cannot switch to an unsigned feed', () => {
  assert.equal(validateReleaseConfig(releaseConfig).publicKeyPem,publicKeyPem);
  for (const patch of [{publicKeyPem:''},{updateProtocol:'electron-updater'},{provider:'generic'},
    {publicKeyPem:generateKeyPairSync('rsa',{modulusLength:2048}).publicKey.export({type:'spki',format:'pem'})}]) {
    assert.throws(() => validateReleaseConfig({...releaseConfig,...patch}));
  }
});

test('public release configuration preserves its explicit provider and pinned signing key', () => {
  const publicConfig = {...releaseConfig,provider:'github-public'};
  assert.equal(validateReleaseConfig(publicConfig).provider,'github-public');
  assert.equal(validateReleaseConfig(publicConfig).publicKeyPem,publicKeyPem);
  assert.throws(() => validateReleaseConfig({...publicConfig,publicKeyPem:''}));
  assert.throws(() => validateReleaseConfig({...publicConfig,updateProtocol:'unsigned'}));
});

test('failed download cannot create an install plan', async () => {
  const {updates,calls} = fakeUpdates({result:{version:'0.4.1'},downloadError:new Error('broken transport')});
  await updates.check();
  await assert.rejects(updates.prepareInstall({}),/дождитесь/);
  assert.equal(calls.some(call => call[0] === 'prepare'),false);
});

test('concurrent checks share the active operation and completed update is not fetched twice', async () => {
  const {updates,calls} = fakeUpdates({result:{version:'0.4.1'}});
  await Promise.all([updates.check(),updates.check()]);
  await updates.check();
  assert.deepEqual(calls,[['check'],['download']]);
});
