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
  constructor({config, packaged, credentials, notify, changed = () => {}, log = () => {},
    currentVersion, cacheDir, platform = process.platform, arch = process.arch,
    clientFactory = options => new (require('./private-release.cjs').PrivateReleaseClient)(options),
    installer = null}) {
    this.config = validateReleaseConfig(config);
    this.enabled = packaged && this.config.enabled;
    this.credentials = credentials;
    this.notify = notify;
    this.changed = changed;
    this.log = log;
    this.clientFactory = clientFactory;
    this.installer = installer;
    this.currentVersion = currentVersion;
    this.cacheDir = cacheDir;
    this.platform = platform;
    this.arch = arch;
    this.state = this.enabled ? 'idle' : 'disabled';
    this.version = null;
    this.manual = false;
    this.artifact = null;
    this.info = null;
  }
  setState(state) { this.state = state; this.changed(); }
  failure() {
    const wasManual = this.manual;
    this.manual = false;
    this.setState('error');
    this.log('Не удалось проверить или скачать обновление (секреты ответа не записываются).');
    if (wasManual) this.notify(this.config.provider === 'github-private'
      ? 'Не удалось получить обновление. Проверьте интернет, приглашение в закрытый репозиторий и персональный ключ доступа. Установленная версия продолжает работать.'
      : 'Не удалось получить обновление. Проверьте интернет и повторите позже: GitHub также ограничивает частоту запросов. Установленная версия продолжает работать.');
  }
  async check(manual = false) {
    if (!this.enabled) {
      if (manual) this.notify('Для этой сборки канал обновлений ещё не настроен.');
      return;
    }
    if (['checking', 'downloading'].includes(this.state)) return;
    if (this.state === 'downloaded') { if (manual) this.notify('Обновление уже загружено. Его можно установить из меню приложения.'); return; }
    this.manual = manual;
    this.setState('checking');
    try {
      const privateChannel = this.config.provider === 'github-private';
      const token = privateChannel ? this.credentials?.read() : undefined;
      if (privateChannel && !token) {
        this.manual = false; this.setState('access-required');
        if (manual) this.notify('Откройте «Доступ к обновлениям» в меню Violet и укажите свой персональный ключ для закрытого репозитория.');
        return;
      }
      // Only private channels read a per-recipient key. Public channels need no
      // credentials at all; neither channel consults ambient build credentials.
      const client = this.clientFactory({config:this.config,token,platform:this.platform,
        arch:this.arch,currentVersion:this.currentVersion,cacheDir:this.cacheDir,
        progress:info => {
          this.progress = Math.max(0, Math.min(100, Math.floor(info.percent || 0)));
          this.changed();
        }});
      const info = await client.check();
      if (!info) {
        this.setState('idle');
        if (this.manual) this.notify('Установлена последняя версия Violet.');
        this.manual = false;
        return;
      }
      this.version = info.version;
      this.progress = 0;
      this.setState('downloading');
      const artifact = await client.download(info);
      this.info = info;
      this.artifact = artifact;
      this.setState('downloaded');
      this.notify(`Версия ${info.version} загружена и проверена. Сохраните проект, затем выберите «Установить обновление и перезапустить» в меню приложения.`);
      this.manual = false;
    } catch { this.failure(); }
  }
  async prepareInstall(options) {
    if (this.state !== 'downloaded' || !this.info || !this.artifact)
      throw new Error('Сначала дождитесь загрузки обновления');
    const installer = this.installer || require('./update-install.cjs');
    return installer.prepareUpdate({...options,file:this.artifact,info:this.info,
      platform:this.platform,arch:this.arch});
  }
  async launchInstall(plan) {
    const installer = this.installer || require('./update-install.cjs');
    return installer.launchUpdate(plan);
  }
  label() {
    return {disabled:'Обновления не подключены', checking:'Проверка обновлений…',
      downloading:`Загрузка обновления: ${this.progress || 0}%`, downloaded:`Готова версия ${this.version}`,
      'access-required':'Нужен доступ к обновлениям', error:'Обновления временно недоступны'}[this.state] || 'Проверить обновления…';
  }
}
module.exports = {Updates, verifyPrivateAccess};
