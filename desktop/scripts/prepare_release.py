"""Create non-secret updater policy. Production refuses incomplete credentials."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import re

ROOT = Path(__file__).resolve().parents[2]


def release_configuration(env, system=None, architecture=None):
    system = system or platform.system()
    architecture = architecture or platform.machine()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(architecture)
    target = {"Darwin": "mac", "Windows": "win"}.get(system)
    if not target or not arch:
        raise ValueError("Unsupported native build target")
    owner = env.get("VIOLET_GITHUB_OWNER", "")
    repo = env.get("VIOLET_GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner):
        raise ValueError("Production needs VIOLET_GITHUB_OWNER for the actual private release repository")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo) or repo in {".", ".."}:
        raise ValueError("Production needs VIOLET_GITHUB_REPOSITORY for the actual private release repository")
    if not (env.get("CSC_LINK") or env.get("CSC_NAME")):
        raise ValueError("Production needs a signing identity (CSC_LINK or CSC_NAME)")
    if env.get("CSC_LINK") and not env.get("CSC_KEY_PASSWORD"):
        raise ValueError("CSC_KEY_PASSWORD is required for imported signing identities")
    if target == "mac":
        apple_id = all(env.get(key) for key in ("APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID"))
        api_key = all(env.get(key) for key in ("APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER"))
        if not (apple_id or api_key):
            raise ValueError("Production macOS needs Apple notarization credentials")
    elif not env.get("VIOLET_WINDOWS_PUBLISHER_NAME"):
        raise ValueError("Windows publisher must match the Authenticode certificate")
    return {"enabled": True, "provider": "github-private", "owner": owner, "repo": repo, "channel": "stable",
            "platform": target, "architecture": arch}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    config = {"enabled": False, "provider": "github-private", "channel": "stable"}
    if args.release:
        try:
            config = release_configuration(os.environ)
        except ValueError as error:
            parser.error(str(error))
        report_path = ROOT / "desktop/stage/notices/inventory.json"
        if not report_path.is_file() or not json.loads(report_path.read_text()).get("release_ready"):
            parser.error("Native dependency redistribution notices must be reviewed before a production release")
        inventory_path = ROOT / "desktop/stage/backend/bundle-inventory.json"
        if not inventory_path.is_file():
            parser.error("Build the native backend first")
        inventory = json.loads(inventory_path.read_text())
        if inventory.get("platform") != os.sys.platform or inventory.get("architecture") != platform.machine():
            parser.error("The backend bundle was built for another OS/architecture")
    destination = ROOT / "desktop/stage/release-config.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Updater policy:", "signed production release" if args.release else "local preview; network updates disabled")


if __name__ == "__main__":
    main()
