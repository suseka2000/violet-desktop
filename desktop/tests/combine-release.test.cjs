"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { collect, combine } = require("../scripts/combine-release.cjs");
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "violet-release-test-"));
  const dirs = ["mac-arm64", "mac-x64", "win-x64"].map(name => {
    const directory = path.join(root, name); fs.mkdirSync(directory);
    const artifact = `Violet-0.4.0-${name}.${name.startsWith("mac") ? "zip" : "exe"}`;
    const bytes = Buffer.from(`binary ${name}`);
    fs.writeFileSync(path.join(directory, artifact), bytes);
    const channel = name.startsWith("mac") ? "latest-mac.yml" : "latest.yml";
    const manifest = { version: "0.4.0", files: [{ url: artifact, size: bytes.length,
      sha512: crypto.createHash("sha512").update(bytes).digest("base64") }] };
    fs.writeFileSync(path.join(directory, channel), JSON.stringify(manifest));
    return directory;
  });
  return { root, dirs };
}
test("merges both mac architectures and preserves checksum validation", t => {
  const { root, dirs } = fixture(); t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const result = collect(dirs);
  assert.equal(result.channels.get("latest-mac.yml").match(/url:/g).length, 2);
  const destination = path.join(root, "combined");
  assert.equal(combine(dirs, destination).artifacts, 3);
  assert.throws(() => combine(dirs, destination), /EEXIST/);
});
test("missing architecture or changed binary cannot produce a release", t => {
  const { root, dirs } = fixture(); t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.throws(() => collect([dirs[0], dirs[2]]), /both signed/);
  fs.appendFileSync(path.join(dirs[0], "Violet-0.4.0-mac-arm64.zip"), "tampered");
  assert.throws(() => collect(dirs), /changed update/);
});
