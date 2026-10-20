"use strict";
// Offline only. The private key never belongs in the app, repository or CI.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const root = path.resolve(__dirname, "../..");
const defaultPublic = path.join(root, "desktop/build/update-public-key.json");

function regular(filename) {
  const stat = fs.lstatSync(filename);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) throw Error("Expected one regular non-linked file");
  return stat;
}
function fingerprint(key) {
  return crypto.createHash("sha256").update(crypto.createPublicKey(key).export({type:"spki",format:"der"})).digest("hex");
}
function keygen(directory) {
  const resolved = path.resolve(directory);
  const realParent = fs.realpathSync(path.dirname(resolved));
  const safe = path.join(realParent, path.basename(resolved));
  if (safe === root || safe.startsWith(root + path.sep)) throw Error("Private keys must be outside the source repository");
  // Refuse replacement of an existing key or directory, even when empty.
  fs.mkdirSync(safe, {mode:0o700});
  const keys = crypto.generateKeyPairSync("ed25519");
  fs.writeFileSync(path.join(safe, "release-private.pem"), keys.privateKey.export({type:"pkcs8",format:"pem"}), {flag:"wx",mode:0o600});
  fs.writeFileSync(path.join(safe, "release-public.pem"), keys.publicKey.export({type:"spki",format:"pem"}), {flag:"wx",mode:0o644});
  return {directory:safe, publicFingerprint:fingerprint(keys.privateKey)};
}
function signRelease({artifact, platform, arch, version, key, output, publicKey = defaultPublic}) {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version) || version.length > 64) throw Error("Use a stable MAJOR.MINOR.PATCH version");
  if (!["darwin","win32"].includes(platform) || !["arm64","x64"].includes(arch) ||
      platform === "win32" && arch !== "x64") throw Error("Unsupported release target");
  const filename = `Violet-${version}-${platform === "darwin" ? "mac" : "win"}-${arch}.${platform === "darwin" ? "zip" : "exe"}`;
  if (path.basename(artifact) !== filename) throw Error("Artifact filename must exactly match version and target");
  const stat = regular(artifact), keyStat = regular(key);
  if (!Number.isSafeInteger(stat.size) || stat.size <= 0) throw Error("Empty or oversized artifact");
  if (process.platform !== "win32" && (keyStat.mode & 0o077)) throw Error("Private key must have owner-only permissions (0600)");
  const privateKey = crypto.createPrivateKey(fs.readFileSync(key));
  const keyText = fs.readFileSync(publicKey, "utf8");
  const pinned = crypto.createPublicKey(keyText.trimStart().startsWith("{") ? JSON.parse(keyText).publicKeyPem : keyText);
  if (privateKey.asymmetricKeyType !== "ed25519" || pinned.asymmetricKeyType !== "ed25519" ||
      fingerprint(privateKey) !== crypto.createHash("sha256").update(pinned.export({type:"spki",format:"der"})).digest("hex")) {
    throw Error("Signing key does not match the public key pinned into the application");
  }
  const handle = fs.openSync(artifact, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  let digest;
  try {
    const current = fs.fstatSync(handle);
    if (current.ino !== stat.ino || current.dev !== stat.dev || current.size !== stat.size) throw Error("Artifact changed");
    const hash = crypto.createHash("sha256"), buffer = Buffer.allocUnsafe(1024 * 1024);
    for (;;) { const count = fs.readSync(handle, buffer, 0, buffer.length, null); if (!count) break; hash.update(buffer.subarray(0,count)); }
    const after = fs.fstatSync(handle);
    if (after.size !== current.size || after.mtimeMs !== current.mtimeMs) throw Error("Artifact changed during signing");
    digest = hash.digest("hex");
  } finally { fs.closeSync(handle); }
  const manifest = {schema:"violet-update/1", version, platform, arch, filename, size:stat.size, sha256:digest};
  const bytes = Buffer.from(JSON.stringify(manifest, null, 2) + "\n");
  const signature = crypto.sign(null, bytes, privateKey);
  if (!crypto.verify(null, bytes, pinned, signature)) throw Error("Self-verification failed");
  const destination = path.resolve(output);
  if (!fs.existsSync(destination)) fs.mkdirSync(destination, {recursive:true});
  if (fs.lstatSync(destination).isSymbolicLink()) throw Error("Output cannot be a symlink");
  const name = `violet-update-${platform}-${arch}.json`;
  // Never silently replace a previously signed release manifest.
  if (fs.existsSync(path.join(destination,name)) || fs.existsSync(path.join(destination,name + ".sig"))) throw Error("Manifest already exists");
  fs.writeFileSync(path.join(destination,name), bytes, {flag:"wx",mode:0o644});
  fs.writeFileSync(path.join(destination,name + ".sig"), signature.toString("base64") + "\n", {flag:"wx",mode:0o644});
  return {manifest:path.join(destination,name), signature:path.join(destination,name + ".sig"), artifact:path.resolve(artifact),
    version, releaseTag:`v${version}`, publicFingerprint:fingerprint(privateKey)};
}
function main(argv) {
  if (argv[0] === "keygen" && argv.length === 2) return keygen(argv[1]);
  if (argv[0] !== "sign" || argv.length % 2 !== 1) throw Error("Usage: sign-release.cjs keygen OUTSIDE_REPO_DIRECTORY | sign --artifact FILE --platform darwin|win32 --arch arm64|x64 --version X.Y.Z --key PRIVATE_PEM --output DIRECTORY [--public-key PUBLIC_PEM]");
  const options = {};
  for (let i=1;i<argv.length;i+=2) {
    const option = {"--artifact":"artifact","--platform":"platform","--arch":"arch","--version":"version","--key":"key","--output":"output","--public-key":"publicKey"}[argv[i]];
    if (!option || options[option]) throw Error("Unknown or repeated option");
    options[option] = argv[i+1];
  }
  if (["artifact","platform","arch","version","key","output"].some(name=>!options[name])) throw Error("Missing signing argument");
  return signRelease(options);
}
if (require.main === module) {
  try { console.log(JSON.stringify(main(process.argv.slice(2)),null,2)); }
  catch (error) { console.error(error.message); process.exitCode=1; }
}
module.exports = {keygen, signRelease, fingerprint, main};
