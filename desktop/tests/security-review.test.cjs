'use strict';
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const test = require('node:test');
const {Updates} = require('../updater.cjs');
const {authenticatedHeaders, trustedIPC, validateReleaseConfig} = require('../security.cjs');

function fakeUpdates({packaged = true, config} = {}) {
  const updater = new EventEmitter();
  const calls = [], notices = [], logs = [];
  updater.setFeedURL = options => { updater.feed = options; calls.push(['feed', options]); };
  updater.checkForUpdates = async () => { calls.push(['check']); updater.emit('checking-for-update'); };
  updater.downloadUpdate = async () => { calls.push(['download']); return []; };
  const updates = new Updates({
    updater, packaged, verifyAccess:async () => {},
    config: config || {enabled:true,provider:'github-private',owner:'Violet-Example',repo:'desktop',channel:'stable'},
    credentials: {read: () => 'github_pat_personal_fake_test_token'},
    notify: message => notices.push(message), log: message => logs.push(message)
  });
  return {updater, updates, calls, notices, logs};
}

test('signed normal-installer update policy disables web-installer payloads and implicit installation', () => {
  const {updater} = fakeUpdates();
  assert.equal(updater.disableWebInstaller, true,
    'electron-updater 6.x otherwise accepts secondary web-installer packages');
  assert.equal(updater.autoDownload, false);
  assert.equal(updater.autoInstallOnAppQuit, false);
  assert.equal(updater.allowDowngrade, false);
  assert.equal(updater.allowPrerelease, false);
  assert.equal(updater.logger, null);
});

test('only the per-user private repository token is used, never ambient publishing credentials', async () => {
  const original = {GH_TOKEN:process.env.GH_TOKEN,GITHUB_TOKEN:process.env.GITHUB_TOKEN};
  process.env.GH_TOKEN = 'publishing_secret_test_only';
  process.env.GITHUB_TOKEN = 'another_publishing_secret_test_only';
  try {
    const {updates, updater} = fakeUpdates();
    await updates.check(true);
    assert.equal(updater.feed.token, 'github_pat_personal_fake_test_token');
    assert.equal(updater.feed.private, true);
    assert.equal(process.env.GH_TOKEN, undefined);
    assert.equal(process.env.GITHUB_TOKEN, undefined);
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test('private provider error details cannot disclose access secrets in logs or notifications', async () => {
  const {updates, updater, logs, notices} = fakeUpdates();
  await updates.check(true);
  updater.emit('error', new Error('authorization: token github_pat_personal_fake_test_token'));
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
