"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const {keygen,signRelease} = require("../scripts/sign-release.cjs");

test("offline signing pins key, version, target and bytes without exposing private key", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(),"violet-sign-"));
  try {
    const keys = path.join(temp,"keys");
    keygen(keys);
    const artifact = path.join(temp,"Violet-0.4.0-mac-arm64.zip");
    fs.writeFileSync(artifact,"test artifact");
    const options = {artifact,platform:"darwin",arch:"arm64",version:"0.4.0",
      key:path.join(keys,"release-private.pem"),publicKey:path.join(keys,"release-public.pem"),output:path.join(temp,"signed")};
    const result = signRelease(options), bytes = fs.readFileSync(result.manifest);
    const manifest = JSON.parse(bytes);
    assert.deepEqual(Object.keys(manifest),["schema","version","platform","arch","filename","size","sha256"]);
    assert.equal(manifest.sha256,crypto.createHash("sha256").update("test artifact").digest("hex"));
    assert(crypto.verify(null,bytes,fs.readFileSync(options.publicKey),Buffer.from(fs.readFileSync(result.signature,"utf8").trim(),"base64")));
    assert(!JSON.stringify(result).includes("PRIVATE KEY"));
    assert.throws(()=>signRelease(options),/already exists/);
    assert.throws(()=>keygen(keys),/EEXIST/);
    assert.throws(()=>signRelease({...options,version:"0.4.0-beta"}),/stable/);
    assert.throws(()=>signRelease({...options,arch:"x64"}),/filename/);
  } finally { fs.rmSync(temp,{recursive:true,force:true}); }
});
test("wrong pinned key is rejected before creating manifest", () => {
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),"violet-sign-key-"));
  try {
    const a=path.join(temp,"a"),b=path.join(temp,"b"); keygen(a); keygen(b);
    const artifact=path.join(temp,"Violet-1.2.3-win-x64.exe"); fs.writeFileSync(artifact,"binary");
    assert.throws(()=>signRelease({artifact,platform:"win32",arch:"x64",version:"1.2.3",
      key:path.join(a,"release-private.pem"),publicKey:path.join(b,"release-public.pem"),output:path.join(temp,"out")}),/pinned/);
    assert(!fs.existsSync(path.join(temp,"out")));
  } finally { fs.rmSync(temp,{recursive:true,force:true}); }
});
