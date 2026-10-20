"""Build a native onedir Python runtime; no user project is included."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop/build"))
from bundle_policy import application_assets, inventory, library_assets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-manifest", type=Path, default=ROOT / "desktop/build/approved-step-library.json")
    parser.add_argument("--without-library", action="store_true", help="Create an empty-library diagnostic build")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if platform.system() not in {"Darwin", "Windows"}:
        parser.error("Desktop artifacts must be built natively on macOS or Windows")
    stage = ROOT / "desktop/stage"
    stage.mkdir(parents=True, exist_ok=True)
    library_manifest = None if args.without_library else args.library_manifest
    files = application_assets(ROOT) + library_assets(ROOT, library_manifest)
    manifest = {
        "schema_version": "violet-desktop-inputs/1",
        "platform": sys.platform, "architecture": platform.machine(),
        "python": platform.python_version(),
        "private_library_included": library_manifest is not None,
        "files": [{**row, "source": str(source)} for (source, _), row in zip(files, inventory(files))],
    }
    (stage / "bundle-inputs.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Explicit bundle inputs: {len(files)} files; private library: {manifest['private_library_included']}", flush=True)
    if args.plan_only:
        return
    if importlib.metadata.version("PyInstaller") != "6.22.3":
        parser.error("Install requirements-desktop-build.txt in the build environment")
    subprocess.run([sys.executable, str(ROOT / "desktop/scripts/collect_notices.py")], check=True, cwd=ROOT)
    for path in sorted((stage / "notices").rglob("*")):
        if path.is_file():
            manifest["files"].append({"source": str(path), "path": "notices/" + path.relative_to(stage / "notices").as_posix()})
    (stage / "bundle-inputs.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    build_env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(stage / "pyinstaller-cache")}
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(stage), "--workpath", str(stage / "pyinstaller-work"),
                    str(ROOT / "desktop/build/violet-backend.spec")], check=True, cwd=ROOT, env=build_env)
    # Do not leak absolute build-machine paths into the installer.
    summary = {key: value for key, value in manifest.items() if key != "files"}
    summary["files"] = inventory(files)
    (stage / "backend" / "bundle-inventory.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Native backend ready:", stage / "backend")


if __name__ == "__main__":
    main()
