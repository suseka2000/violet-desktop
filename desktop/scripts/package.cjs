"use strict";
// Cross-platform wrapper: a release command cannot accidentally emit a preview.
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const desktop = path.resolve(__dirname, "..");
const root = path.dirname(desktop);
const [mode, ...args] = process.argv.slice(2);
if (!["preview", "release"].includes(mode)) throw new Error("Specify preview or release");
const nativePython = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.VIOLET_BUILD_PYTHON || (fs.existsSync(nativePython) ? nativePython : (process.platform === "win32" ? "python" : "python3"));
const env = { ...process.env, VIOLET_RELEASE: mode === "release" ? "1" : "0" };
function run(command, arguments_) {
  const result = spawnSync(command, arguments_, { cwd: desktop, env, stdio: "inherit", shell: false });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
if (!fs.existsSync(path.join(desktop, "stage/backend", process.platform === "win32" ? "violet-backend.exe" : "violet-backend"))) {
  throw new Error("Build the native Python backend before packaging");
}
run(python, [path.join(__dirname, "prepare_release.py"), ...(mode === "release" ? ["--release"] : [])]);
run(process.execPath, [require.resolve("electron-builder/out/cli/cli.js"), "--config", "electron-builder.cjs", "--publish", "never", ...args]);
