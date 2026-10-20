'use strict';
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { localOrigin } = require('./security.cjs');

function launchArguments({packaged, resourcesPath, appPath, dataDir, platform = process.platform, env = process.env}) {
  if (packaged) {
    return {command: path.join(resourcesPath, 'backend', platform === 'win32' ? 'violet-backend.exe' : 'violet-backend'),
      args: ['--data-dir', dataDir, '--parent-pid', String(process.pid)]};
  }
  const root = path.resolve(appPath, '..');
  const python = env.VIOLET_PYTHON || path.join(root, '.venv', platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  return {command: python, args: ['-m', 'backend.desktop_entry', '--data-dir', dataDir,
    '--assets-root', root, '--parent-pid', String(process.pid)], cwd: root};
}

function backendEnvironment(source, token) {
  const result = {...source};
  for (const name of Object.keys(result)) {
    if (/^(GH_|GITHUB_|CSC_|WIN_CSC_|APPLE_|AWS_|AZURE_|NPM_TOKEN$|NODE_AUTH_TOKEN$|ELECTRON_RUN_AS_NODE$)/i.test(name))
      delete result[name];
  }
  return {...result, VIOLET_DESKTOP_TOKEN: token, PYTHONUNBUFFERED:'1', PYTHONUTF8:'1'};
}

class BackendProcess extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.token = randomBytes(32).toString('hex');
    this.child = null;
    this.origin = null;
    this.stopping = false;
    this.log = options.log || (() => {});
  }
  async start() {
    if (this.child) throw new Error('Движок уже запущен');
    this.stopping = false;
    this.origin = null;
    fs.mkdirSync(this.options.dataDir, {recursive: true});
    const launch = launchArguments(this.options);
    if (!fs.existsSync(launch.command)) throw new Error('Встроенный движок не найден. Установите полный комплект приложения.');
    return new Promise((resolve, reject) => {
      let buffer = '', ready = false, settled = false;
      const fail = error => { if (!settled) { settled = true; clearTimeout(timer); reject(error); } };
      const child = this.child = spawn(launch.command, launch.args, {
        cwd: launch.cwd || path.dirname(launch.command), windowsHide: true,
        env: backendEnvironment(process.env, this.token),
        stdio: ['ignore', 'pipe', 'pipe']
      });
      const timer = setTimeout(() => {
        fail(new Error('Движок не запустился за две минуты. Подробности сохранены в журнале.'));
        child.kill();
      }, 120000);
      child.once('error', fail);
      child.once('exit', (code, signal) => {
        this.child = null;
        this.origin = null;
        if (!ready) fail(new Error(`Движок завершился до запуска (${code ?? signal}). Откройте журнал приложения.`));
        else if (!this.stopping) this.emit('unexpected-exit', {code, signal});
      });
      child.stderr.on('data', bytes => this.log(bytes.toString('utf8').replaceAll(this.token, '[secret]')));
      child.stdout.on('data', bytes => {
        buffer += bytes.toString('utf8');
        if (buffer.length > 65536) { buffer = ''; fail(new Error('Некорректный ответ движка')); child.kill(); return; }
        let end;
        while ((end = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, end).trim(); buffer = buffer.slice(end + 1);
          if (!line) continue;
          let message;
          try { message = JSON.parse(line); } catch { this.log(line.replaceAll(this.token, '[secret]')); continue; }
          if (ready || message.type !== 'ready') continue;
          try {
            if (message.protocol !== 1 || message.pid !== child.pid) throw new Error('Несовместимая версия движка');
            this.origin = localOrigin(message.port);
            ready = true; settled = true; clearTimeout(timer); resolve(this.origin);
          } catch (error) { fail(error); child.kill(); }
        }
      });
    });
  }
  request(endpoint, method = 'GET', body) {
    if (!this.origin) return Promise.reject(new Error('Движок ещё не запущен'));
    return new Promise((resolve, reject) => {
      const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
      const request = http.request(new URL(endpoint, this.origin), {method,
        headers: {'X-Violet-Desktop-Token': this.token,
          ...(data ? {'Content-Type': 'application/json', 'Content-Length': data.length} : {})}
      }, response => {
        let size = 0; const chunks = [];
        response.once('error', reject);
        response.once('aborted', () => reject(new Error('Ответ движка прерван')));
        response.on('data', chunk => {
          size += chunk.length;
          if (size > 1024 * 1024) request.destroy(new Error('Слишком большой ответ состояния'));
          else chunks.push(chunk);
        });
        response.on('end', () => {
          if (response.statusCode !== 200) {
            const error = new Error(response.statusCode === 409
              ? 'Выполняется расчёт или сохранение. Обновление отложено; дождитесь завершения.'
              : `Движок: HTTP ${response.statusCode}`);
            error.status = response.statusCode; reject(error); return;
          }
          try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch (error) { reject(error); }
        });
      });
      request.setTimeout(15000, () => request.destroy(new Error('Движок не ответил')));
      request.once('error', reject);
      if (data) request.write(data);
      request.end();
    });
  }
  health() { return this.request('/api/v1/desktop/health'); }
  async stop({requireIdle = false} = {}) {
    if (this.stopPromise) return this.stopPromise;
    this.stopPromise = this._stop(requireIdle);
    try { return await this.stopPromise; }
    finally { this.stopPromise = null; }
  }
  async _stop(requireIdle) {
    this.stopping = true;
    const child = this.child;
    if (!child) return;
    // Install only after the database is closed and worker shutdown is complete.
    let exitListener;
    const exited = new Promise(resolve => { exitListener = resolve; child.once('exit', exitListener); });
    try { await this.request('/api/v1/desktop/shutdown', 'POST', {require_idle:requireIdle}); }
    catch (error) {
      if (requireIdle) {
        child.removeListener('exit', exitListener);
        this.stopping = false;
        throw error;
      }
      this.log(`Остановка: ${error.message}`); child.kill();
    }
    let timer;
    const done = await Promise.race([exited.then(() => true),
      new Promise(resolve => { timer = setTimeout(() => resolve(false), 30000); })]);
    clearTimeout(timer);
    if (!done) {
      child.removeListener('exit', exitListener);
      throw new Error('Движок ещё завершает расчёты. Обновление не установлено; попробуйте позже.');
    }
  }
}

module.exports = {BackendProcess, launchArguments, backendEnvironment};
