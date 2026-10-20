"""Create non-secret signed updater policy; paid OS certificates are optional."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import re
import base64

ROOT = Path(__file__).resolve().parents[2]


def public_key_pem(value):
    if not isinstance(value, str):
        raise ValueError("A pinned Ed25519 public key is required")
    match = re.fullmatch(r"\s*-----BEGIN PUBLIC KEY-----\s+([A-Za-z0-9+/=\s]+)\s+-----END PUBLIC KEY-----\s*", value)
    if not match:
        raise ValueError("Expected an Ed25519 SPKI public key, never a private key")
    try:
        der = base64.b64decode("".join(match.group(1).split()), validate=True)
    except ValueError as error:
        raise ValueError("Invalid public-key encoding") from error
    if len(der) != 44 or der[:12] != bytes.fromhex("302a300506032b6570032100"):
        raise ValueError("The update key must be Ed25519")
    return "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der).decode() + "\n-----END PUBLIC KEY-----\n"


def release_configuration(env, system=None, architecture=None):
    system = system or platform.system()
    architecture = architecture or platform.machine()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(architecture)
    target = {"Darwin": "mac", "Windows": "win"}.get(system)
    if not target or not arch:
        raise ValueError("Unsupported native build target")
    owner = env.get("VIOLET_GITHUB_OWNER") or "suseka2000"
    repo = env.get("VIOLET_GITHUB_REPOSITORY") or "violet-desktop"
    provider = env.get("VIOLET_UPDATE_PROVIDER") or "github-public"
    if provider not in {"github-public", "github-private"}:
        raise ValueError("VIOLET_UPDATE_PROVIDER must be github-public or github-private")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner):
        raise ValueError("Production needs VIOLET_GITHUB_OWNER for the actual release repository")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo) or repo in {".", ".."}:
        raise ValueError("Production needs VIOLET_GITHUB_REPOSITORY for the actual release repository")
    pinned = ROOT / "desktop/build/update-public-key.json"
    pem = env.get("VIOLET_UPDATE_PUBLIC_KEY") or (json.loads(pinned.read_text(encoding="utf-8")).get("publicKeyPem") if pinned.is_file() else "")
    key = public_key_pem(pem)
    certified = env.get("VIOLET_OS_SIGNING") == "1"
    if certified:
        if not (env.get("CSC_LINK") or env.get("CSC_NAME")):
            raise ValueError("Optional OS signing requires a signing identity")
        if env.get("CSC_LINK") and not env.get("CSC_KEY_PASSWORD"):
            raise ValueError("CSC_KEY_PASSWORD is required for imported signing identities")
        if target == "mac":
            apple_id = all(env.get(name) for name in ("APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID"))
            api_key = all(env.get(name) for name in ("APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER"))
            if not (apple_id or api_key):
                raise ValueError("Optional certified macOS release needs notarization credentials")
        elif not env.get("VIOLET_WINDOWS_PUBLISHER_NAME"):
            raise ValueError("Windows publisher must match the Authenticode certificate")
    return {"enabled": True, "provider": provider, "owner": owner, "repo": repo, "channel": "stable",
            "platform": target, "architecture": arch, "updateProtocol": "violet-ed25519-v1",
            "publicKeyPem": key, "osSigned": certified}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    config = {"enabled": False, "provider": "github-public", "channel": "stable"}
    if args.release:
        try:
            config = release_configuration(os.environ)
        except ValueError as error:
            parser.error(str(error))
        report_path = ROOT / "desktop/stage/notices/inventory.json"
        if not report_path.is_file():
            parser.error("Generate the dependency notices inventory before packaging")
        if not json.loads(report_path.read_text(encoding="utf-8")).get("release_ready"):
            print("NOTICE REVIEW PENDING: this build does not certify third-party license completeness; review before redistribution.",
                  file=os.sys.stderr)
        inventory_path = ROOT / "desktop/stage/backend/bundle-inventory.json"
        if not inventory_path.is_file():
            parser.error("Build the native backend first")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if inventory.get("platform") != os.sys.platform or inventory.get("architecture") != platform.machine():
            parser.error("The backend bundle was built for another OS/architecture")
    destination = ROOT / "desktop/stage/release-config.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Updater policy:", f"{config['provider']} Ed25519 updates; artifacts must be signed separately"
          if args.release else "local preview; network updates disabled")


if __name__ == "__main__":
    main()
