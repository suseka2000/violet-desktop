"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const names = ["LICENSE", "LICENSES.chromium.html"];

async function preserve(context, destination) {
  // electron-builder renames Windows LICENSE during cleanupAfterUnpack,
  // before afterExtract runs. macOS keeps LICENSE until branding. Preserve
  // these exact extracted notices under the same names in every app.
  fs.mkdirSync(destination, {recursive:true});
  const files = [];
  for (const filename of names) {
    const extractedName = filename === "LICENSE" && context.electronPlatformName === "win32"
      ? "LICENSE.electron.txt" : filename;
    const source = path.join(context.appOutDir, extractedName), stat = fs.lstatSync(source);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error("Missing regular Electron license: " + filename);
    const bytes = fs.readFileSync(source);
    fs.writeFileSync(path.join(destination, filename), bytes);
    files.push({filename, sha256:crypto.createHash("sha256").update(bytes).digest("hex"), size:bytes.length});
  }
  fs.writeFileSync(path.join(destination, "inventory.json"), JSON.stringify({
    electronVersion:context.packager.info.framework.version, files,
    source:"Actual electron-builder verified native Electron distribution"
  },null,2));
}

async function verify(context) {
  const destination = path.join(context.packager.getResourcesDir(context.appOutDir), "notices/electron");
  const report = JSON.parse(fs.readFileSync(path.join(destination, "inventory.json"), "utf8"));
  for (const filename of names) {
    const source = path.join(destination, filename), stat = fs.lstatSync(source);
    if (!stat.isFile() || stat.isSymbolicLink()) throw Error("Electron notice missing from packaged app: " + filename);
    const digest = crypto.createHash("sha256").update(fs.readFileSync(source)).digest("hex");
    if (!report.files.some(file=>file.filename === filename && file.sha256 === digest && file.size === stat.size)) {
      throw Error("Electron notice changed while packaging: " + filename);
    }
  }
}
module.exports = {preserve, verify};
