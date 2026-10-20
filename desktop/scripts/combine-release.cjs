"use strict";
// Offline only: assemble one private GitHub draft's artifacts, never upload.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");
const yaml = createRequire(require.resolve("electron-updater/package.json"))("js-yaml");

const CHANNELS = new Set(["latest.yml", "latest-mac.yml"]);
const ARTIFACT = /\.(?:exe|dmg|zip|blockmap)$/i;
function sha512(buffer) { return crypto.createHash("sha512").update(buffer).digest("base64"); }
function safeName(value) {
  if (typeof value !== "string" || !value || value !== path.basename(value) || value.includes("\\") || value.includes(":")) {
    throw new Error("Release manifests must reference local artifact basenames only");
  }
  return value;
}
function collect(inputs) {
  const artifacts = new Map(), channels = new Map(), versions = new Set();
  for (const directory of inputs) {
    if (fs.lstatSync(directory).isSymbolicLink()) throw new Error("Symlink release input is not allowed");
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (!entry.isFile() || !ARTIFACT.test(entry.name)) continue;
      const bytes = fs.readFileSync(path.join(directory, entry.name));
      const old = artifacts.get(entry.name);
      if (old && !old.equals(bytes)) throw new Error(`Conflicting artifact: ${entry.name}`);
      artifacts.set(entry.name, bytes);
    }
    for (const channel of CHANNELS) {
      const source = path.join(directory, channel);
      if (!fs.existsSync(source)) continue;
      if (fs.lstatSync(source).isSymbolicLink()) throw new Error("Symlink channel manifest is not allowed");
      const manifest = yaml.load(fs.readFileSync(source, "utf8"), { schema: yaml.JSON_SCHEMA });
      if (!manifest || typeof manifest.version !== "string" || !Array.isArray(manifest.files) || !manifest.files.length) {
        throw new Error(`Invalid update manifest: ${source}`);
      }
      versions.add(manifest.version);
      const list = channels.get(channel) || [];
      list.push(manifest);
      channels.set(channel, list);
    }
  }
  if (versions.size !== 1) throw new Error("All native artifacts must describe exactly one common version");
  const result = new Map();
  for (const [channel, manifests] of channels) {
    const files = new Map();
    for (const manifest of manifests) {
      for (const file of manifest.files) {
        const name = safeName(file.url);
        const content = artifacts.get(name);
        if (!content || sha512(content) !== file.sha512 || (file.size != null && content.length !== file.size)) {
          throw new Error(`Missing or changed update artifact: ${name}`);
        }
        const prior = files.get(name);
        if (prior && prior.sha512 !== file.sha512) throw new Error(`Conflicting metadata: ${name}`);
        files.set(name, file);
      }
    }
    const combined = [...files.values()].sort((a, b) => a.url.localeCompare(b.url));
    const first = combined[0];
    result.set(channel, yaml.dump({
      version: manifests[0].version,
      files: combined,
      path: first.url,
      sha512: first.sha512,
      releaseDate: manifests[0].releaseDate,
    }, { lineWidth: -1, noRefs: true }));
  }
  if (!result.has("latest.yml") || !result.has("latest-mac.yml")) {
    throw new Error("The private release needs Windows and macOS update metadata");
  }
  const mac = yaml.load(result.get("latest-mac.yml"), { schema: yaml.JSON_SCHEMA });
  if (!["arm64", "x64"].every(arch => mac.files.some(file => file.url.endsWith(`-${arch}.zip`)))) {
    throw new Error("macOS manifest must include both signed ARM64 and Intel ZIPs");
  }
  return { artifacts, channels: result, version: [...versions][0] };
}
function combine(inputs, destination) {
  const result = collect(inputs);
  // A new directory avoids stale binaries or metadata from older releases.
  fs.mkdirSync(destination);
  for (const [name, data] of result.artifacts) fs.writeFileSync(path.join(destination, name), data, { flag: "wx" });
  for (const [name, data] of result.channels) fs.writeFileSync(path.join(destination, name), data, { flag: "wx" });
  const sums = [...result.artifacts].map(([name, data]) => `${crypto.createHash("sha256").update(data).digest("hex")}  ${name}`).sort();
  fs.writeFileSync(path.join(destination, "SHA256SUMS.txt"), sums.join("\n") + "\n", { flag: "wx" });
  return { version: result.version, artifacts: result.artifacts.size, metadata: [...result.channels.keys()] };
}
module.exports = { collect, combine };
if (require.main === module) {
  const [destination, ...inputs] = process.argv.slice(2);
  if (!destination || inputs.length < 2) {
    console.error("Usage: node scripts/combine-release.cjs NEW_OUTPUT_DIR MAC_ARM_DIR MAC_INTEL_DIR WINDOWS_DIR");
    process.exitCode = 2;
  } else {
    console.log(JSON.stringify(combine(inputs.map(p => path.resolve(p)), path.resolve(destination)), null, 2));
    console.log("Prepared locally only. Review signatures; upload binaries first, manifests last, then publish the private draft.");
  }
}
