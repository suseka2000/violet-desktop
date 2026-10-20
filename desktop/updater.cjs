'use strict';
const { validateReleaseConfig } = require('./security.cjs');
const https = require('node:https');

function verifyPrivateAccess(config, token, transport = https) {
  // Never follow redirects with the key or fall back to a public feed.
  return new Promise((resolve, reject) => {
    const request = transport.get({
      hostname:'api.github.com', path:`/repos/${encodeURIComponent(config.owner)}/${encodeURIComponent(config.repo)}`,
      headers:{Authorization:`Bearer ${token}`, Accept:'application/vnd.github+json',
        'User-Agent':'Violet-Desktop', 'X-GitHub-Api-Version':'2022-11-28'}
    }, response => {
      let size = 0; const chunks = [];
      response.once('error', reject);
      response.once('aborted', () => reject(new Error('Access check interrupted')));
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 256 * 1024) request.destroy(new Error('Access response too large'));
        else chunks.push(chunk);
      });
      response.on('end', () => {
        try {
          const metadata = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (response.statusCode !== 200 || metadata.private !== true ||
              metadata.full_name?.toLowerCase() !== `${config.owner}/${config.repo}`.toLowerCase())
            throw new Error('Private access not confirmed');
          resolve();
        } catch { reject(new Error('Private access not confirmed')); }
      });
    });
    request.setTimeout(15000, () => request.destroy(new Error('Access check timeout')));
    request.once('error', reject);
  });
}

class Updates {
  constructor({updater, config, packaged, credentials, notify, changed = () => {}, log = () => {}, verifyAccess = verifyPrivateAccess}) {
    this.updater = updater;
    this.config = validateReleaseConfig(config);
    this.enabled = packaged && this.config.enabled;
    this.credentials = credentials;
    this.notify = notify;
    this.changed = changed;
    this.log = log;
    this.verifyAccess = verifyAccess;
    this.state = this.enabled ? 'idle' : 'disabled';
    this.version = null;
    this.manual = false;
    if (!this.enabled) return;
    updater.autoDownload = false;
    updater.autoInstallOnAppQuit = false;
    updater.allowDowngrade = false;
    updater.allowPrerelease = false;
    updater.disableDifferentialDownload = false;
    updater.disableWebInstaller = true;
    // Do not log provider URLs/headers: private-repository errors may carry keys.
    updater.logger = null;
    updater.on('checking-for-update', () => this.setState('checking'));
    updater.on('update-available', info => {
      this.version = info.version;
      this.setState('downloading');
      updater.downloadUpdate().catch(() => this.failure());
    });
    updater.on('update-not-available', () => {
      this.setState('idle');
      if (this.manual) this.notify('Установлена последняя версия Violet.');
      this.manual = false;
    });
    updater.on('download-progress', info => {
      this.progress = Math.max(0, Math.min(100, Math.floor(info.percent || 0)));
      this.changed();
    });
    updater.on('update-downloaded', info => {
      this.version = info.version;
      this.setState('downloaded');
      this.notify(`Версия ${info.version} загружена. Сохраните проект, затем выберите «Установить обновление и перезапустить» в меню приложения.`);
      this.manual = false;
    });
    updater.on('error', () => this.failure());
  }
  setState(state) { this.state = state; this.changed(); }
  failure() {
    const wasManual = this.manual;
    this.manual = false;
    this.setState('error');
    this.log('Не удалось проверить или скачать обновление (секреты ответа не записываются).');
    if (wasManual) this.notify('Не удалось получить обновление. Проверьте интернет, приглашение в закрытый репозиторий и персональный ключ доступа. Установленная версия продолжает работать.');
  }
  async check(manual = false) {
    if (!this.enabled) {
      if (manual) this.notify('Это тестовая сборка. Канал подписанных обновлений ещё не подключён.');
      return;
    }
    if (['checking', 'downloading'].includes(this.state)) return;
    if (this.state === 'downloaded') { if (manual) this.notify('Обновление уже загружено. Его можно установить из меню приложения.'); return; }
    this.manual = manual;
    this.setState('checking');
    try {
      if (this.config.provider === 'github-private') {
        const token = this.credentials.read();
        if (!token) {
          this.manual = false; this.setState('access-required');
          if (manual) this.notify('Откройте «Доступ к обновлениям» в меню Violet и укажите свой персональный ключ для закрытого репозитория.');
          return;
        }
        // Stable electron-updater otherwise prefers ambient developer tokens.
        delete process.env.GH_TOKEN; delete process.env.GITHUB_TOKEN;
        await this.verifyAccess(this.config, token);
        this.updater.setFeedURL({provider: 'github', owner: this.config.owner, repo: this.config.repo,
          private: true, token});
      } else {
        this.updater.setFeedURL({provider: 'generic', url: this.config.feedUrl, channel: 'latest'});
      }
      await this.updater.checkForUpdates();
    } catch { this.failure(); }
  }
  label() {
    return {disabled:'Обновления не подключены', checking:'Проверка обновлений…',
      downloading:`Загрузка обновления: ${this.progress || 0}%`, downloaded:`Готова версия ${this.version}`,
      'access-required':'Нужен доступ к обновлениям', error:'Обновления временно недоступны'}[this.state] || 'Проверить обновления…';
  }
}
module.exports = {Updates, verifyPrivateAccess};
