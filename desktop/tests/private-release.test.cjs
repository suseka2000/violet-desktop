'use strict';
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {Readable} = require('node:stream');
const test = require('node:test');
const {PrivateReleaseClient, newerVersion} = require('../private-release.cjs');

const keys = crypto.generateKeyPairSync('ed25519');
const publicKeyPem = keys.publicKey.export({type: 'spki', format: 'pem'});
const TOKEN = 'github_pat_only_a_test_secret';
const API = 'https://api.github.com/repos/Violet-Example/private-releases';
const bytes = Buffer.from('Private installer bytes; not a real executable.');
function response(body = '', statusCode = 200, headers = {}) {
  if (!Buffer.isBuffer(body) && typeof body !== 'string') body = JSON.stringify(body);
  const stream = Readable.from([Buffer.from(body)]);
  stream.statusCode = statusCode;
  stream.headers = headers;
  return stream;
}
function streamResponse(chunks, statusCode = 200, headers = {}) {
  const stream = Readable.from(chunks);
  stream.statusCode = statusCode;
  stream.headers = headers;
  return stream;
}
function fixture({platform = 'darwin', arch = 'arm64', version = '0.4.1', manifest = {}, raw,
                  signature, release: releasePatch = {}, binary: binaryPatch = {}} = {}) {
  const document = {schema: 'violet-update/1', version, platform, arch,
    filename: `Violet-${version}-${platform === 'darwin' ? 'mac' : 'win'}-${arch}.${platform === 'darwin' ? 'zip' : 'exe'}`,
    size: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex'), ...manifest};
  const content = raw || Buffer.from(JSON.stringify(document));
  const signed = signature || Buffer.from(crypto.sign(null, content, keys.privateKey).toString('base64') + '\n');
  const name = `violet-update-${platform}-${arch}.json`;
  const asset = (id, assetName, size) => ({id, name: assetName, size, state: 'uploaded', url: `${API}/releases/assets/${id}`});
  const release = {id: 101, tag_name: `v${version}`, draft: false, prerelease: false,
    assets: [asset(1, name, content.length), asset(2, `${name}.sig`, signed.length),
      {...asset(3, document.filename, bytes.length), ...binaryPatch}], ...releasePatch};
  const calls = [];
  const responses = new Map([
    [API, () => response({private: true, full_name: 'Violet-Example/private-releases'})],
    [`${API}/releases/latest`, () => response(release)],
    [`${API}/releases/assets/1`, () => response(content)],
    [`${API}/releases/assets/2`, () => response(signed)],
    [`${API}/releases/assets/3`, () => response(bytes)]
  ]);
  const request = async (url, options) => {
    calls.push({url: url.href, ...options});
    assert.ok(responses.has(url.href), `Unexpected URL: ${url.origin}${url.pathname}`);
    return responses.get(url.href)(options);
  };
  return {document, content, signed, release, responses, calls, request, platform, arch};
}
async function setup(t, options = {}) {
  const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), 'violet-private-update-test-'));
  t.after(() => fs.rm(cacheDir, {recursive: true, force: true}));
  const source = fixture(options);
  const progress = [];
  const config = {enabled: true, provider: 'github-private', owner: 'Violet-Example',
    repo: 'private-releases', channel: 'stable', updateProtocol: 'violet-ed25519-v1', publicKeyPem};
  const client = new PrivateReleaseClient({config, token: TOKEN, currentVersion: '0.4.0',
    platform: source.platform, arch: source.arch, cacheDir, request: source.request,
    progress: value => progress.push(value)});
  return {...source, client, cacheDir, progress, config};
}
const rejects = (operation, code) => assert.rejects(operation, error => error.code === code && !error.message.includes(TOKEN));

test('signed macOS update streams to an atomic verified cache; subsequent download rechecks bytes', async t => {
  const item = await setup(t);
  const info = await item.client.check();
  assert.equal(info.version, '0.4.1');
  assert.ok(Object.isFrozen(info));
  const filename = await item.client.download(info);
  assert.deepEqual(await fs.readFile(filename), bytes);
  assert.equal(item.progress.at(-1).percent, 100);
  assert.deepEqual(await fs.readdir(item.cacheDir), [path.basename(filename)]);
  assert.equal(await item.client.download(info), filename);
  assert.equal(item.calls.filter(call => call.url.endsWith('/3')).length, 1);
  await fs.writeFile(filename, 'tampered local cache');
  assert.equal(await item.client.download(info), filename);
  assert.deepEqual(await fs.readFile(filename), bytes);
  assert.equal(item.calls.filter(call => call.url.endsWith('/3')).length, 2);
});

test('Windows x64 uses the signed exe and numeric semver ordering, never lexical ordering', async t => {
  const item = await setup(t, {platform: 'win32', arch: 'x64', version: '0.10.0'});
  const info = await item.client.check();
  assert.equal(info.filename, 'Violet-0.10.0-win-x64.exe');
  assert.match(await item.client.download(info), /\.exe$/);
  assert.equal(newerVersion('0.10.0', '0.9.9'), true);
  assert.equal(newerVersion('0.4.0', '0.4.0'), false);
  assert.equal(newerVersion('0.3.99', '0.4.0'), false);
  for (const invalid of ['v0.4.1', '00.4.1', '0.4.1-beta.1', '0.4.1+other', '../../file']) {
    assert.throws(() => newerVersion(invalid, '0.4.0'));
  }
});

test('same, older, or absent releases are not offered', async t => {
  for (const version of ['0.4.0', '0.3.9']) {
    const item = await setup(t, {version});
    assert.equal(await item.client.check(), null);
    assert.equal(item.calls.length, 2);
  }
  const item = await setup(t);
  item.responses.set(`${API}/releases/latest`, () => response({}, 404));
  assert.equal(await item.client.check(), null);
});

test('private access, exact repository identity, draft and prerelease checks are mandatory', async t => {
  for (const body of [{private: false, full_name: 'Violet-Example/private-releases'},
    {private: true, full_name: 'Attacker/private-releases'}]) {
    const item = await setup(t);
    item.responses.set(API, () => response(body));
    await rejects(item.client.check(), 'access');
    assert.equal(item.calls.length, 1);
  }
  for (const release of [{draft: true}, {prerelease: true}, {tag_name: 'other'}, {tag_name: 'v0.4.1-beta.1'}]) {
    const item = await setup(t, {release});
    await assert.rejects(item.client.check());
    assert.equal(item.calls.length, 2);
  }
});

test('wrong signature is rejected before malformed manifest JSON can be interpreted', async t => {
  const item = await setup(t, {raw: Buffer.from('not-json with a dangerous URL'),
    signature: Buffer.from(Buffer.alloc(64).toString('base64'))});
  await rejects(item.client.check(), 'signature');
  const signedInvalid = await setup(t, {raw: Buffer.from('{signed invalid json')});
  await rejects(signedInvalid.client.check(), 'manifest');
});

test('a different signing key, corrupt base64, and changed raw whitespace all fail authentication', async t => {
  const other = crypto.generateKeyPairSync('ed25519');
  const raw = fixture().content;
  for (const signature of [Buffer.from('invalid'),
    Buffer.from(crypto.sign(null, raw, keys.privateKey).toString('base64')).map(byte => byte | 128),
    Buffer.from(crypto.sign(null, raw, other.privateKey).toString('base64')),
    Buffer.from(crypto.sign(null, Buffer.concat([raw, Buffer.from('\n')]), keys.privateKey).toString('base64'))]) {
    const item = await setup(t, {signature});
    await rejects(item.client.check(), 'signature');
  }
});

test('authenticated manifest must match platform, architecture, tag, exact filename, and strict schema', async t => {
  for (const manifest of [
    {platform: 'win32'}, {arch: 'x64'}, {version: '0.5.0'}, {filename: '../../escape.exe'},
    {filename: 'other.zip'}, {sha256: 'xyz'}, {size: -1}, {size: Number.MAX_SAFE_INTEGER + 1},
    {schema: 'unknown'}, {url: 'https://attacker.invalid/file'}
  ]) {
    const item = await setup(t, {manifest});
    await rejects(item.client.check(), 'manifest');
  }
});

test('release metadata cannot replace asset identity, size, or signed digest', async t => {
  for (const binary of [{id: -2}, {state: 'new'}, {url: 'https://attacker.invalid/payload'}]) {
    const item = await setup(t, {binary});
    await rejects(item.client.check(), 'metadata');
  }
  const size = await setup(t, {binary: {size: bytes.length + 1}});
  await rejects(size.client.check(), 'size');
  const digest = await setup(t, {binary: {digest: `sha256:${'0'.repeat(64)}`}});
  await rejects(digest.client.check(), 'digest');
  const duplicate = await setup(t);
  duplicate.release.assets.push(duplicate.release.assets[0]);
  await rejects(duplicate.client.check(), 'metadata');
});

test('download accepts only an authenticated object returned by this client', async t => {
  const item = await setup(t);
  await rejects(item.client.download({...item.document, assetId: 3}), 'unverified');
  const info = await item.client.check();
  await rejects(item.client.download({...info}), 'unverified');
  const second = await setup(t);
  await rejects(second.client.download(info), 'unverified');
  assert.equal(item.calls.filter(call => call.url.endsWith('/3')).length, 0);
});

test('redirected assets receive no authorization, cookie or API headers', async t => {
  const item = await setup(t);
  const cdn = 'https://release-assets.githubusercontent.com/asset?signed=temporary';
  item.responses.set(`${API}/releases/assets/3`, () => response('', 302, {location: cdn}));
  item.responses.set(cdn, () => response(bytes));
  const info = await item.client.check();
  await item.client.download(info);
  const api = item.calls.find(call => call.url.endsWith('/3'));
  assert.equal(api.headers.Authorization, `Bearer ${TOKEN}`);
  const redirected = item.calls.find(call => call.url === cdn);
  assert.equal(redirected.headers.Authorization, undefined);
  assert.equal(redirected.headers.Cookie, undefined);
  assert.equal(redirected.headers['X-GitHub-Api-Version'], undefined);
});

test('arbitrary hosts, downgrade, credentials, ports and return-to-API redirects are rejected without requests', async t => {
  for (const location of [
    'https://attacker.invalid/file', 'http://release-assets.githubusercontent.com/file',
    'https://release-assets.githubusercontent.com.attacker.invalid/file',
    'https://user:password@release-assets.githubusercontent.com/file',
    'https://release-assets.githubusercontent.com:8443/file',
    `${API}/other`, 'file:///tmp/payload'
  ]) {
    const item = await setup(t);
    item.responses.set(`${API}/releases/assets/3`, () => response('', 302, {location}));
    const info = await item.client.check();
    await rejects(item.client.download(info), 'redirect');
    assert.equal(item.calls.at(-1).url, `${API}/releases/assets/3`);
  }
  const item = await setup(t);
  const cdn = 'https://release-assets.githubusercontent.com/asset';
  item.responses.set(`${API}/releases/assets/3`, () => response('', 302, {location: cdn}));
  item.responses.set(cdn, () => response('', 302, {location: `${API}/releases/assets/3`}));
  await rejects(item.client.download(await item.client.check()), 'redirect');
  assert.equal(item.calls.filter(call => call.url.endsWith('/3')).length, 1);
});

test('metadata redirects and infinite asset redirects fail closed', async t => {
  const item = await setup(t);
  item.responses.set(API, () => response('', 302, {location: API}));
  await rejects(item.client.check(), 'redirect');
  assert.equal(item.calls.length, 1);
  const loop = await setup(t);
  loop.responses.set(`${API}/releases/assets/3`, () => response('', 302, {location: `${API}/releases/assets/3`}));
  await rejects(loop.client.download(await loop.client.check()), 'redirect');
  assert.equal(loop.calls.filter(call => call.url.endsWith('/3')).length, 6);
});

test('streamed length and digest checks reject corrupt payloads without ready files', async t => {
  for (const body of [bytes.subarray(0, -1), Buffer.concat([bytes, Buffer.from('extra')]), Buffer.alloc(bytes.length)]) {
    const item = await setup(t);
    item.responses.set(`${API}/releases/assets/3`, () => response(body));
    await assert.rejects(item.client.download(await item.client.check()));
    assert.deepEqual(await fs.readdir(item.cacheDir), []);
  }
  for (const headers of [{'content-length': `${bytes.length + 1}`}, {'content-encoding': 'gzip'}]) {
    const item = await setup(t);
    item.responses.set(`${API}/releases/assets/3`, () => response(bytes, 200, headers));
    await assert.rejects(item.client.download(await item.client.check()));
    assert.deepEqual(await fs.readdir(item.cacheDir), []);
  }
});

test('interrupted download resumes with Range, recomputes hash, and atomically completes', async t => {
  const item = await setup(t);
  const split = 13;
  item.responses.set(`${API}/releases/assets/3`, () => streamResponse((async function* () {
    yield bytes.subarray(0, split);
    await new Promise(resolve => setTimeout(resolve, 5));
    throw new Error(`request leaked ${TOKEN}`);
  })()));
  const info = await item.client.check();
  await rejects(item.client.download(info), 'network');
  const files = await fs.readdir(item.cacheDir);
  assert.equal(files.length, 1);
  assert.ok(files[0].endsWith('.part'));
  assert.equal((await fs.stat(path.join(item.cacheDir, files[0]))).size, split);
  item.responses.set(`${API}/releases/assets/3`, options => {
    assert.equal(options.headers.Range, `bytes=${split}-`);
    return response(bytes.subarray(split), 206, {
      'content-length': `${bytes.length - split}`,
      'content-range': `bytes ${split}-${bytes.length - 1}/${bytes.length}`
    });
  });
  const filename = await item.client.download(info);
  assert.deepEqual(await fs.readFile(filename), bytes);
  assert.deepEqual(await fs.readdir(item.cacheDir), [path.basename(filename)]);
});

test('a server ignoring Range restarts cleanly; a wrong Content-Range is rejected', async t => {
  for (const honorsRange of [false, true]) {
    const item = await setup(t);
    const info = await item.client.check();
    const pending = path.join(item.cacheDir, `${info.sha256}-${info.filename}.part`);
    await fs.writeFile(pending, bytes.subarray(0, 5));
    item.responses.set(`${API}/releases/assets/3`, () => honorsRange ?
      response(bytes.subarray(5), 206, {'content-range': `bytes 0-${bytes.length - 1}/${bytes.length}`}) :
      response(bytes, 200));
    if (honorsRange) {
      await rejects(item.client.download(info), 'size');
      assert.deepEqual(await fs.readdir(item.cacheDir), []);
    } else {
      assert.deepEqual(await fs.readFile(await item.client.download(info)), bytes);
    }
  }
});

test('an interrupted fully written payload is verified and reused, not downloaded again', async t => {
  const item = await setup(t);
  const info = await item.client.check();
  await fs.writeFile(path.join(item.cacheDir, `${info.sha256}-${info.filename}.part`), bytes);
  assert.deepEqual(await fs.readFile(await item.client.download(info)), bytes);
  assert.equal(item.calls.filter(call => call.url.endsWith('/3')).length, 0);
});

test('simultaneous requests share one bounded download', async t => {
  const item = await setup(t);
  item.responses.set(`${API}/releases/assets/3`, async () => {
    await new Promise(resolve => setTimeout(resolve, 10));
    return response(bytes);
  });
  const info = await item.client.check();
  const files = await Promise.all([item.client.download(info), item.client.download(info)]);
  assert.equal(files[0], files[1]);
  assert.equal(item.calls.filter(call => call.url.endsWith('/3')).length, 1);
});

test('oversized metadata is rejected before parsing; provider errors never surface secrets or URLs', async t => {
  const item = await setup(t);
  item.responses.set(`${API}/releases/assets/1`, () => response('x'.repeat(17000)));
  await rejects(item.client.check(), 'size');
  const erroring = await setup(t);
  erroring.responses.set(API, () => { throw new Error(`https://signed.invalid/?token=${TOKEN}`); });
  await rejects(erroring.client.check(), 'network');
  assert.ok(!JSON.stringify(erroring.client).includes(TOKEN));
});

test('unsafe configuration, algorithm substitution, and cache symlinks fail closed', async t => {
  const item = await setup(t);
  for (const config of [{...item.config, owner: '../owner'}, {...item.config, repo: '..'},
    {...item.config, owner: undefined}, {...item.config, repo: undefined},
    {...item.config, updateProtocol: 'none'}, {...item.config, publicKeyPem: 'invalid'},
    {...item.config, publicKeyPem: crypto.generateKeyPairSync('rsa', {modulusLength: 2048}).publicKey.export({type: 'spki', format: 'pem'})}]) {
    assert.throws(() => new PrivateReleaseClient({config, token: TOKEN, platform: 'darwin',
      arch: 'arm64', currentVersion: '0.4.0', cacheDir: item.cacheDir}), {code: 'configuration'});
  }
  const info = await item.client.check();
  const target = path.join(item.cacheDir, 'unrelated');
  await fs.writeFile(target, 'preserve');
  try {
    await fs.symlink(target, path.join(item.cacheDir, `${info.sha256}-${info.filename}.part`));
  } catch (error) {
    if (process.platform === 'win32' && error.code === 'EPERM') { t.diagnostic('Windows symlink privilege unavailable'); return; }
    throw error;
  }
  await rejects(item.client.download(info), 'cache');
  assert.equal(await fs.readFile(target, 'utf8'), 'preserve');
});
