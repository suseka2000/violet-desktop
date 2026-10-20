'use strict';
const { URL } = require('node:url');

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
    return Object.freeze({enabled: true, provider: 'github-private', owner: value.owner, repo: value.repo, channel: 'stable'});
  }
  if (!isSafeExternal(value.feedUrl)) throw new Error('Для обновлений необходим HTTPS-адрес без пароля');
  const url = new URL(value.feedUrl);
  if (url.search || url.hash || url.hostname === 'localhost' || /^127\./.test(url.hostname) ||
      ['[::1]', '0.0.0.0'].includes(url.hostname)) throw new Error('Некорректный адрес обновлений');
  if (value.channel !== 'stable') throw new Error('Неизвестный канал обновлений');
  return Object.freeze({enabled: true, feedUrl: url.href.replace(/\/?$/, '/'), channel: 'stable'});
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
