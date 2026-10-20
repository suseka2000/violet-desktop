"use strict";
const fs = require("node:fs");
const path = require("node:path");
const electronNotices = require("./build/electron_notices.cjs");
const release = process.env.VIOLET_RELEASE === "1";
const certified = process.env.VIOLET_OS_SIGNING === "1";
const policyPath = path.join(__dirname, "stage", "release-config.json");
const policy = fs.existsSync(policyPath) ? JSON.parse(fs.readFileSync(policyPath, "utf8")) : { enabled: false };
if (release !== Boolean(policy.enabled)) {
  throw new Error("Run scripts/prepare_release.py for the selected preview/release mode first.");
}
if (release && (policy.provider !== "github-private" || !policy.owner || !policy.repo ||
    policy.updateProtocol !== "violet-ed25519-v1" || !policy.publicKeyPem)) {
  throw new Error("Private release requires a pinned Ed25519 key and private GitHub repository.");
}
if (certified && (!release || !policy.osSigned || !process.env.CSC_LINK && !process.env.CSC_NAME)) {
  throw new Error("Optional OS signing requires a validated signing identity.");
}
const arch = process.arch;
if (release && (policy.architecture !== arch || policy.platform !== (process.platform === "darwin" ? "mac" : "win"))) {
  throw new Error("Release policy belongs to another native platform/architecture.");
}
if (release) {
  const notices = JSON.parse(fs.readFileSync(path.join(__dirname, "stage/notices/inventory.json"), "utf8"));
  const backend = JSON.parse(fs.readFileSync(path.join(__dirname, "stage/backend/bundle-inventory.json"), "utf8"));
  const nativeArch = { arm64: "arm64", aarch64: "arm64", x86_64: "x64", AMD64: "x64" }[backend.architecture];
  if (backend.platform !== process.platform || nativeArch !== arch) {
    throw new Error("Release requires a matching native backend.");
  }
  if (!notices.release_ready) console.warn("Dependency notice review remains incomplete; app authenticity signing does not resolve license obligations.");
}
module.exports = {
  appId: "com.violet.interior-rd",
  productName: "Violet",
  artifactName: "Violet-${version}-${os}-${arch}.${ext}",
  directories: { output: "dist", buildResources: "build" },
  // This is an app-only allowlist, never the parent workspace.
  files: ["main.cjs", "backend-process.cjs", "security.cjs", "updater.cjs", "preload.cjs",
    "access-preload.cjs", "credentials.cjs", "loading.html", "loading.css", "access.html",
    "access.js", "access.css", "private-release.cjs", "update-install.cjs", "update-zip.cjs",
    "update-helpers/mac-install.sh", "update-helpers/windows-install.ps1",
    "package.json", "!**/*.test.*", "!**/.env*"],
  extraResources: [
    { from: "stage/backend", to: "backend", filter: ["**/*", "!**/__pycache__/**", "!**/*.pyc"] },
    { from: "stage/release-config.json", to: "release-config.json" },
    { from: "stage/notices", to: "notices" },
  ],
  asar: true,
  asarUnpack: ["update-helpers/**"],
  afterExtract: context => electronNotices.preserve(context, path.join(__dirname, "stage/notices/electron")),
  afterPack: electronNotices.verify,
  npmRebuild: false,
  forceCodeSigning: certified,
  // Free private updates use our pinned-key manifests, not latest.yml trust.
  publish: null,
  mac: {
    target: [{ target: "dmg", arch: [arch] }, { target: "zip", arch: [arch] }],
    category: "public.app-category.graphics-design",
    identity: certified ? undefined : null,
    hardenedRuntime: true,
    entitlements: "build/entitlements.mac.plist",
    entitlementsInherit: "build/entitlements.mac.plist",
    notarize: certified,
    binaries: ["Contents/Resources/backend/violet-backend"],
  },
  dmg: { sign: certified },
  win: {
    target: [{ target: "nsis", arch: ["x64"] }],
    verifyUpdateCodeSignature: true,
    ...(certified ? { publisherName: process.env.VIOLET_WINDOWS_PUBLISHER_NAME } : {}),
  },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowElevation: false,
    allowToChangeInstallationDirectory: true,
    deleteAppDataOnUninstall: false,
    createDesktopShortcut: true,
    differentialPackage: true,
  },
};
