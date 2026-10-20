'use strict';

// A small, independent update trust boundary. GitHub optionally authenticates access;
// the pinned Ed25519 key authenticates the bytes that may be installed.
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const https = require('node:https');
const path = require('node:path');
const {TextDecoder} = require('node:util');

const ASSET_HOSTS = new Set([
  'release-assets.githubusercontent.com',
  'objects.githubusercontent.com',
  'github-releases.githubusercontent.com',
  'github-production-release-asset-2e65be.s3.amazonaws.com'
]);
const MESSAGES = Object.freeze({
  configuration: 'Некорректная настройка защищённых обновлений.',
  access: 'Не удалось подтвердить доступ и настройки репозитория обновлений.',
  network: 'Не удалось загрузить обновление. Проверьте соединение и повторите попытку.',
  redirect: 'Сервер обновлений вернул недопустимое перенаправление.',
  metadata: 'Метаданные выпуска не прошли проверку.',
  signature: 'Подпись обновления не прошла проверку.',
  manifest: 'Обновление не соответствует версии или платформе приложения.',
  size: 'Размер загруженного обновления не соответствует подписанному выпуску.',
  digest: 'Контрольная сумма обновления не совпадает. Повторите загрузку.',
  cache: 'Не удалось безопасно сохранить обновление.',
  unverified: 'Сначала проверьте доступность подписанного обновления.'
});

class ReleaseError extends Error {
  constructor(code) {
    super(MESSAGES[code] || MESSAGES.network);
    this.name = 'ReleaseError';
    this.code = code;
  }
}
const fail = code => { throw new ReleaseError(code); };
const protect = (error, code) => error instanceof ReleaseError ? error : new ReleaseError(code);
const integer = number => Number.isSafeInteger(number) && number > 0;

function versionParts(value) {
  if (typeof value !== 'string' || value.length > 64 ||
      !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(value)) fail('manifest');
  return value.split('.').map(BigInt);
}
function newerVersion(candidate, current) {
  const a = versionParts(candidate), b = versionParts(current);
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}
function assetFilename(version, platform, arch) {
  return `Violet-${version}-${platform === 'darwin' ? 'mac' : 'win'}-${arch}.${platform === 'darwin' ? 'zip' : 'exe'}`;
}
function header(response, name) {
  const value = response.headers?.[name.toLowerCase()];
  return typeof value === 'string' ? value : undefined;
}
function dispose(response) {
  if (typeof response?.destroy === 'function') response.destroy();
}

// Injectable at the HTTP boundary; production always uses Node TLS, with its
// normal certificate checks. No ambient publishing credentials are consulted.
function requestHttps(url, {headers, timeoutMs = 30000} = {}) {
  return new Promise((resolve, reject) => {
    const request = https.request(url, {method: 'GET', headers}, resolve);
    request.setTimeout(timeoutMs, () => request.destroy(new ReleaseError('network')));
    request.once('error', () => reject(new ReleaseError('network')));
    request.end();
  });
}

async function limitedBody(response, maximum) {
  try {
    const length = header(response, 'content-length');
    if (length !== undefined && (!/^\d+$/.test(length) || Number(length) > maximum)) fail('size');
    if (header(response, 'content-encoding') && header(response, 'content-encoding') !== 'identity') fail('metadata');
    let lengthRead = 0;
    const blocks = [];
    for await (const chunk of response) {
      const bytes = Buffer.from(chunk);
      lengthRead += bytes.length;
      if (lengthRead > maximum) fail('size');
      blocks.push(bytes);
    }
    if (length !== undefined && lengthRead !== Number(length)) fail('size');
    return Buffer.concat(blocks, lengthRead);
  } catch (error) {
    dispose(response);
    throw protect(error, 'network');
  }
}

function parseJson(bytes, code) {
  try {
    return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
  } catch {
    fail(code);
  }
}

function verifyManifest(bytes, encodedSignature, publicKey) {
  // Do not parse even one manifest field until the raw bytes are authenticated.
  if (encodedSignature.some(byte => byte > 127)) fail('signature');
  const signatureText = encodedSignature.toString('ascii').trim();
  if (!/^[A-Za-z0-9+/]{86}==$/.test(signatureText)) fail('signature');
  const signature = Buffer.from(signatureText, 'base64');
  if (signature.length !== 64 || signature.toString('base64') !== signatureText ||
      !crypto.verify(null, bytes, publicKey, signature)) fail('signature');
  return parseJson(bytes, 'manifest');
}

class PrivateReleaseClient {
  #token;
  #key;
  #request;
  #accepted = new WeakMap();
  #downloads = new Map();
  constructor({config, token, platform = process.platform, arch = process.arch,
               currentVersion, cacheDir, progress = () => {}, request = requestHttps}) {
    try {
      if (!config || config.enabled !== true || !['github-private', 'github-public'].includes(config.provider) ||
          config.channel !== 'stable' || config.updateProtocol !== 'violet-ed25519-v1' ||
          typeof config.owner !== 'string' || typeof config.repo !== 'string' ||
          !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(config.owner) ||
          !/^[A-Za-z0-9_.-]{1,100}$/.test(config.repo) || ['.', '..'].includes(config.repo) ||
          !['darwin', 'win32'].includes(platform) || !['arm64', 'x64'].includes(arch) ||
          (config.provider === 'github-private' &&
            (typeof token !== 'string' || !/^[\x21-\x7e]{1,512}$/.test(token))) ||
          !path.isAbsolute(cacheDir) || typeof request !== 'function') fail('configuration');
      versionParts(currentVersion);
      this.#key = crypto.createPublicKey(config.publicKeyPem);
      if (this.#key.asymmetricKeyType !== 'ed25519') fail('configuration');
      this.config = Object.freeze({owner: config.owner, repo: config.repo, provider: config.provider});
      this.platform = platform;
      this.arch = arch;
      this.currentVersion = currentVersion;
      this.cacheDir = path.resolve(cacheDir);
      this.progress = progress;
      this.#request = request;
      // Public clients ignore even an accidentally supplied per-user key.
      // They never consult ambient GitHub/CI credentials.
      this.#token = config.provider === 'github-private' ? token : undefined;
    } catch {
      fail('configuration');
    }
  }

  #api(suffix = '') {
    return `https://api.github.com/repos/${this.config.owner}/${this.config.repo}${suffix}`;
  }

  async #response(url, {binary = false, range, redirects = false} = {}) {
    const original = new URL(url);
    let current = original;
    let leftApi = false;
    for (let hop = 0; hop <= 5; hop++) {
      const api = current.origin === 'https://api.github.com';
      if (current.protocol !== 'https:' || current.username || current.password || current.hash ||
          (current.port && current.port !== '443') ||
          (api ? leftApi || current.pathname !== original.pathname || current.search !== original.search
               : !ASSET_HOSTS.has(current.hostname))) fail('redirect');
      const headers = {
        'User-Agent': 'Violet-Updater/1',
        'Accept': binary ? 'application/octet-stream' : 'application/vnd.github+json',
        'Accept-Encoding': 'identity'
      };
      if (api) {
        if (this.#token) headers.Authorization = `Bearer ${this.#token}`;
        headers['X-GitHub-Api-Version'] = '2022-11-28';
      }
      if (range !== undefined) headers.Range = `bytes=${range}-`;
      let response;
      try {
        response = await this.#request(current, {headers, timeoutMs: 30000});
      } catch {
        fail('network');
      }
      if (![301, 302, 303, 307, 308].includes(response.statusCode)) return response;
      const location = header(response, 'location');
      dispose(response);
      if (!redirects || !location || hop === 5) fail('redirect');
      try {
        const next = new URL(location, current);
        if (next.origin !== current.origin) leftApi = true;
        current = next;
      } catch {
        fail('redirect');
      }
    }
    fail('redirect');
  }

  async #json(url, {missing = false} = {}) {
    const response = await this.#response(url);
    if (missing && response.statusCode === 404) { dispose(response); return null; }
    if (response.statusCode !== 200) {
      dispose(response);
      fail(response.statusCode === 401 || response.statusCode === 403 || response.statusCode === 404 ? 'access' : 'network');
    }
    return parseJson(await limitedBody(response, 2 * 1024 * 1024), 'metadata');
  }

  #asset(release, name) {
    const matches = release.assets.filter(item => item?.name === name);
    if (matches.length !== 1) fail('metadata');
    const item = matches[0];
    if (!integer(item.id) || !integer(item.size) || item.state !== 'uploaded' ||
        item.url !== this.#api(`/releases/assets/${item.id}`)) fail('metadata');
    return item;
  }

  async #smallAsset(asset, maximum) {
    if (asset.size > maximum) fail('size');
    const response = await this.#response(this.#api(`/releases/assets/${asset.id}`), {binary: true, redirects: true});
    if (response.statusCode !== 200) { dispose(response); fail('network'); }
    const bytes = await limitedBody(response, maximum);
    if (bytes.length !== asset.size) fail('size');
    return bytes;
  }

  async check() {
    try {
      const repository = await this.#json(this.#api());
      if (repository?.private !== (this.config.provider === 'github-private') || typeof repository.full_name !== 'string' ||
          repository.full_name.toLowerCase() !== `${this.config.owner}/${this.config.repo}`.toLowerCase()) fail('access');
      const release = await this.#json(this.#api('/releases/latest'), {missing: true});
      if (release === null) return null;
      if (!release || release.draft !== false || release.prerelease !== false ||
          !integer(release.id) || typeof release.tag_name !== 'string' || !release.tag_name.startsWith('v') ||
          !Array.isArray(release.assets)) fail('metadata');
      const version = release.tag_name.slice(1);
      versionParts(version);
      if (!newerVersion(version, this.currentVersion)) return null;
      const name = `violet-update-${this.platform}-${this.arch}.json`;
      const manifestAsset = this.#asset(release, name);
      const signatureAsset = this.#asset(release, `${name}.sig`);
      const [raw, signature] = await Promise.all([
        this.#smallAsset(manifestAsset, 16 * 1024),
        this.#smallAsset(signatureAsset, 1024)
      ]);
      const manifest = verifyManifest(raw, signature, this.#key);
      const fields = ['schema', 'version', 'platform', 'arch', 'filename', 'size', 'sha256'];
      if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest) ||
          Object.keys(manifest).length !== fields.length || fields.some(key => !Object.hasOwn(manifest, key)) ||
          manifest.schema !== 'violet-update/1' || manifest.version !== version ||
          manifest.platform !== this.platform || manifest.arch !== this.arch ||
          manifest.filename !== assetFilename(version, this.platform, this.arch) ||
          !integer(manifest.size) || !/^[a-f0-9]{64}$/.test(manifest.sha256)) fail('manifest');
      const binary = this.#asset(release, manifest.filename);
      if (binary.size !== manifest.size) fail('size');
      if (binary.digest && binary.digest !== `sha256:${manifest.sha256}`) fail('digest');
      const info = Object.freeze({...manifest, releaseId: release.id, assetId: binary.id});
      this.#accepted.set(info, Object.freeze({...info}));
      return info;
    } catch (error) {
      throw protect(error, 'network');
    }
  }

  async download(info) {
    const accepted = info && this.#accepted.get(info);
    if (!accepted) fail('unverified');
    const key = accepted.sha256;
    if (!this.#downloads.has(key)) {
      const task = this.#download(accepted).catch(error => { throw protect(error, 'cache'); });
      this.#downloads.set(key, task);
      task.finally(() => this.#downloads.delete(key)).catch(() => {});
    }
    return this.#downloads.get(key);
  }

  async #regular(filename, {create = false} = {}) {
    try {
      const stat = await fsp.lstat(filename).catch(error => error.code === 'ENOENT' ? null : Promise.reject(error));
      if (stat && (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1)) fail('cache');
      if (!stat && !create) return null;
      const flags = (create ? fs.constants.O_RDWR | fs.constants.O_CREAT : fs.constants.O_RDONLY) |
                    (fs.constants.O_NOFOLLOW || 0);
      const file = await fsp.open(filename, flags, 0o600);
      const opened = await file.stat();
      if (!opened.isFile() || opened.nlink !== 1 || (stat && (stat.dev !== opened.dev || stat.ino !== opened.ino))) {
        await file.close();
        fail('cache');
      }
      return file;
    } catch (error) {
      throw protect(error, 'cache');
    }
  }

  async #hash(file, size, hash = crypto.createHash('sha256')) {
    const buffer = Buffer.alloc(1024 * 1024);
    let offset = 0;
    while (offset < size) {
      const {bytesRead} = await file.read(buffer, 0, Math.min(buffer.length, size - offset), offset);
      if (!bytesRead) fail('size');
      hash.update(buffer.subarray(0, bytesRead));
      offset += bytesRead;
    }
    return hash;
  }

  #progress(transferred, total, start, startingBytes) {
    try {
      this.progress({transferred, total, percent: transferred / total * 100,
                     bytesPerSecond: (transferred - startingBytes) / Math.max(0.001, (Date.now() - start) / 1000)});
    } catch { /* UI observers must not invalidate verified bytes. */ }
  }

  async #download(info) {
    await fsp.mkdir(this.cacheDir, {recursive: true, mode: 0o700});
    const directory = await fsp.lstat(this.cacheDir);
    if (!directory.isDirectory() || directory.isSymbolicLink()) fail('cache');
    const ready = path.join(this.cacheDir, `${info.sha256}-${info.filename}`);
    const pending = `${ready}.part`;
    let file = await this.#regular(ready);
    if (file) {
      let valid;
      try {
        valid = (await file.stat()).size === info.size &&
                (await this.#hash(file, info.size)).digest('hex') === info.sha256;
      } finally { await file.close(); }
      if (valid) { this.#progress(info.size, info.size, Date.now(), info.size); return ready; }
      await fsp.unlink(ready);
    }
    file = await this.#regular(pending, {create: true});
    let response;
    let removePartial = false;
    try {
      let offset = (await file.stat()).size;
      if (offset > info.size) { await file.truncate(0); offset = 0; }
      let hash = await this.#hash(file, offset);
      if (offset === info.size) {
        if (hash.digest('hex') === info.sha256) {
          await file.sync();
          await file.close(); file = null;
          await fsp.rename(pending, ready);
          this.#progress(info.size, info.size, Date.now(), info.size);
          return ready;
        }
        await file.truncate(0); offset = 0;
        hash = crypto.createHash('sha256');
      }
      response = await this.#response(this.#api(`/releases/assets/${info.assetId}`),
                                     {binary: true, redirects: true, range: offset || undefined});
      if (response.statusCode === 200) {
        if (offset) { await file.truncate(0); offset = 0; hash = crypto.createHash('sha256'); }
      } else if (response.statusCode === 206 && offset) {
        if (header(response, 'content-range') !== `bytes ${offset}-${info.size - 1}/${info.size}`) fail('size');
      } else {
        fail('network');
      }
      const length = header(response, 'content-length');
      if (length !== undefined && (!/^\d+$/.test(length) || Number(length) !== info.size - offset)) fail('size');
      if (header(response, 'content-encoding') && header(response, 'content-encoding') !== 'identity') fail('metadata');
      const started = Date.now(), startingBytes = offset;
      this.#progress(offset, info.size, started, startingBytes);
      try {
        for await (const chunk of response) {
          const bytes = Buffer.from(chunk);
          if (offset + bytes.length > info.size) fail('size');
          let written = 0;
          while (written < bytes.length) {
            const result = await file.write(bytes, written, bytes.length - written, offset + written);
            if (!result.bytesWritten) fail('cache');
            written += result.bytesWritten;
          }
          hash.update(bytes); offset += bytes.length;
          this.#progress(offset, info.size, started, startingBytes);
        }
      } catch (error) {
        throw protect(error, 'network');
      }
      if (offset !== info.size) fail('size');
      if (hash.digest('hex') !== info.sha256) { removePartial = true; fail('digest'); }
      await file.sync();
      await file.close(); file = null;
      await fsp.rename(pending, ready);
      return ready;
    } catch (error) {
      dispose(response);
      if (error instanceof ReleaseError && ['size', 'metadata'].includes(error.code)) removePartial = true;
      throw error;
    } finally {
      if (file) await file.close();
      if (removePartial) await fsp.unlink(pending).catch(() => {});
    }
  }
}

module.exports = {PrivateReleaseClient, ReleaseError, requestHttps, newerVersion, assetFilename};
