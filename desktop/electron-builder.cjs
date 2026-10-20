"use strict";
const fs = require("node:fs");
const path = require("node:path");
const release = process.env.VIOLET_RELEASE === "1";
const policyPath = path.join(__dirname, "stage", "release-config.json");
const policy = fs.existsSync(policyPath) ? JSON.parse(fs.readFileSync(policyPath, "utf8")) : { enabled: false };
if (release !== Boolean(policy.enabled)) {
  throw new Error("Run scripts/prepare_release.py for the selected preview/release mode first.");
}
if (release && (policy.provider !== "github-private" || !policy.owner || !policy.repo || !process.env.CSC_LINK && !process.env.CSC_NAME)) {
  throw new Error("Production requires the private GitHub release repository and a signing identity.");
}
const arch = process.arch;
if (release && (policy.architecture !== arch || policy.platform !== (process.platform === "darwin" ? "mac" : "win"))) {
  throw new Error("Release policy belongs to another native platform/architecture.");
}
if (release) {
  const notices = JSON.parse(fs.readFileSync(path.join(__dirname, "stage/notices/inventory.json"), "utf8"));
  const backend = JSON.parse(fs.readFileSync(path.join(__dirname, "stage/backend/bundle-inventory.json"), "utf8"));
  const nativeArch = { arm64: "arm64", aarch64: "arm64", x86_64: "x64", AMD64: "x64" }[backend.architecture];
  if (!notices.release_ready || backend.platform !== process.platform || nativeArch !== arch) {
    throw new Error("Production requires reviewed native licenses and a matching native backend.");
  }
}
module.exports = {
  appId: "com.violet.interior-rd",
  productName: "Violet",
  artifactName: "Violet-${version}-${os}-${arch}.${ext}",
  directories: { output: "dist", buildResources: "build" },
  // This is an app-only allowlist, never the parent workspace.
  files: ["main.cjs", "backend-process.cjs", "security.cjs", "updater.cjs", "preload.cjs",
    "access-preload.cjs", "credentials.cjs", "loading.html", "loading.css", "access.html",
    "access.js", "access.css", "package.json", "!**/*.test.*", "!**/.env*"],
  extraResources: [
    { from: "stage/backend", to: "backend", filter: ["**/*", "!**/__pycache__/**", "!**/*.pyc"] },
    { from: "stage/release-config.json", to: "release-config.json" },
    { from: "stage/notices", to: "notices" },
  ],
  asar: true,
  npmRebuild: false,
  forceCodeSigning: release,
  publish: release ? [{ provider: "github", owner: policy.owner, repo: policy.repo, private: true, releaseType: "release" }] : null,
  mac: {
    target: [{ target: "dmg", arch: [arch] }, { target: "zip", arch: [arch] }],
    category: "public.app-category.graphics-design",
    identity: release ? undefined : null,
    hardenedRuntime: true,
    entitlements: "build/entitlements.mac.plist",
    entitlementsInherit: "build/entitlements.mac.plist",
    notarize: release,
    binaries: ["Contents/Resources/backend/violet-backend"],
  },
  dmg: { sign: release },
  win: {
    target: [{ target: "nsis", arch: ["x64"] }],
    verifyUpdateCodeSignature: true,
    ...(release ? { publisherName: process.env.VIOLET_WINDOWS_PUBLISHER_NAME } : {}),
  },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowToChangeInstallationDirectory: true,
    deleteAppDataOnUninstall: false,
    createDesktopShortcut: true,
    differentialPackage: true,
  },
};
