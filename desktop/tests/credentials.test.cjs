'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {Credentials} = require('../credentials.cjs');

test('only encrypted bytes are persisted and unavailable encryption fails closed', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(),'violet-credentials-test-'));
  t.after(() => fs.rmSync(dir,{recursive:true,force:true}));
  const value = 'github_pat_test_value_not_a_real_key';
  let saved;
  const storage = {isEncryptionAvailable:() => true,
    encryptString: input => { saved=input; return Buffer.from('opaque-system-encryption'); },
    decryptString: input => { assert.equal(input.toString(),'opaque-system-encryption'); return saved; }};
  const credentials = new Credentials(dir,storage);
  assert.equal(credentials.read(),null);
  credentials.save(value);
  assert.equal(credentials.read(),value);
  assert.doesNotMatch(fs.readFileSync(credentials.filename,'utf8'),/github_pat/);
  if (process.platform !== 'win32') assert.equal(fs.statSync(credentials.filename).mode & 0o777,0o600);
  assert.throws(() => credentials.save('not a token'));
  credentials.clear();
  storage.isEncryptionAvailable = () => false;
  assert.throws(() => credentials.save(value),/хранилище/);
  assert.equal(fs.existsSync(credentials.filename),false);
});
