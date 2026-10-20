'use strict';
const fs = require('node:fs');
const path = require('node:path');

class Credentials {
  constructor(userData, safeStorage) {
    this.filename = path.join(userData, 'update-access.bin');
    this.safeStorage = safeStorage;
  }
  available() { return this.safeStorage.isEncryptionAvailable(); }
  save(value) {
    const token = typeof value === 'string' ? value.trim() : '';
    if (!/^[A-Za-z0-9_]{20,512}$/.test(token)) throw new Error('Нужен персональный ключ GitHub, без пробелов и переносов.');
    if (!this.available()) throw new Error('Защищённое хранилище системы недоступно. Ключ не будет сохранён открытым текстом.');
    fs.mkdirSync(path.dirname(this.filename), {recursive: true});
    fs.writeFileSync(this.filename + '.tmp', this.safeStorage.encryptString(token), {mode: 0o600});
    fs.renameSync(this.filename + '.tmp', this.filename);
  }
  read() {
    if (!fs.existsSync(this.filename)) return null;
    if (!this.available()) throw new Error('Системное хранилище ключей недоступно.');
    return this.safeStorage.decryptString(fs.readFileSync(this.filename));
  }
  clear() { fs.rmSync(this.filename, {force: true}); }
}
module.exports = {Credentials};
