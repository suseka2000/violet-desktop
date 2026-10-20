'use strict';
const { URL } = require('node:url');
const { createPublicKey } = require('node:crypto');

function localOrigin(port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Неверный порт приложения');
  return `http://127.0.0.1:${port}`;
}

function isAppURL(value, origin) {
  try {
    const url = new URL(value);
    return url.origin === origin && !url.username && !url.password;
  } catch { return false; }
}

function isSafeExternal(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password;
  } catch { return false; }
}

function validateReleaseConfig(value) {
  if (!value || value.enabled !== true) return Object.freeze({enabled: false});
  if (value.provider === 'github-private') {
    if (!/^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,38})$/.test(value.owner || '') ||
        !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$/.test(value.repo || '') ||
        value.repo === '..' || value.channel !== 'stable') throw new Error('Некорректный закрытый канал GitHub');
    if (value.updateProtocol !== 'violet-ed25519-v1' || typeof value.publicKeyPem !== 'string' ||
        value.publicKeyPem.length > 4096 || !value.publicKeyPem.startsWith('-----BEGIN PUBLIC KEY-----'))
      throw new Error('Для обновлений нужен открытый ключ выпуска Violet');
    let key;
    try { key = createPublicKey(value.publicKeyPem); } catch { throw new Error('Некорректный открытый ключ выпуска'); }
    if (key.asymmetricKeyType !== 'ed25519') throw new Error('Ключ выпуска должен иметь тип Ed25519');
    return Object.freeze({enabled: true, provider: 'github-private', owner: value.owner, repo: value.repo,
      channel: 'stable', updateProtocol:'violet-ed25519-v1',
      publicKeyPem:key.export({type:'spki',format:'pem'})});
  }
  throw new Error('Поддерживаются только закрытые подписанные выпуски Violet');
}

function authenticatedHeaders(url, origin, token, headers = {}) {
  const result = {};
  // Never forward credentials supplied by a renderer or a redirect.
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() !== 'x-violet-desktop-token') result[key] = value;
  }
  if (isAppURL(url, origin)) result['X-Violet-Desktop-Token'] = token;
  return result;
}

function trustedIPC(event, window, origin) {
  return Boolean(window && !window.isDestroyed() && event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame && isAppURL(event.senderFrame?.url, origin));
}

module.exports = {localOrigin, isAppURL, isSafeExternal, validateReleaseConfig, authenticatedHeaders, trustedIPC};
