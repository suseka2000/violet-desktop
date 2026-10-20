'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {verifyPrivateAccess} = require('../updater.cjs');

function transport(status, body) {
  return {get(options, callback) {
    assert.equal(options.hostname,'api.github.com');
    assert.equal(options.path,'/repos/owner/repo');
    assert.equal(options.headers.Authorization,'Bearer test-only-token');
    const request = new EventEmitter();
    request.setTimeout = () => {};
    queueMicrotask(() => {
      const response = new EventEmitter(); response.statusCode=status;
      callback(response);
      response.emit('data',Buffer.from(JSON.stringify(body)));
      response.emit('end');
    });
    return request;
  }};
}

test('updates require confirmed private repository access with no redirect or public fallback', async () => {
  const config = {owner:'owner',repo:'repo'};
  await verifyPrivateAccess(config,'test-only-token',transport(200,{private:true,full_name:'owner/repo'}));
  for (const [status,body] of [[200,{private:false,full_name:'owner/repo'}],
    [301,{private:true,full_name:'owner/repo'}],[404,{}],[200,{private:true,full_name:'other/repo'}]]) {
    await assert.rejects(verifyPrivateAccess(config,'test-only-token',transport(status,body)),/not confirmed/);
  }
});
