"""Collect dependency license texts, with explicit unresolved-native-license report."""
from __future__ import annotations
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys
import sysconfig
import platform
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
DIRECT = ["pydantic", "ezdxf", "reportlab", "pypdf", "Pillow", "pypdfium2", "shapely", "cadquery-ocp-novtk"]


def dependency_closure(names):
    found = {}
    queue = list(names)
    while queue:
        dist = metadata.distribution(queue.pop())
        name = dist.metadata["Name"]
        key = re.sub(r"[-_.]+", "-", name.lower())
        if key in found:
            continue
        found[key] = dist
        for raw in dist.requires or []:
            dependency = Requirement(raw)
            if dependency.marker is None or dependency.marker.evaluate({"extra": ""}):
                queue.append(dependency.name)
    return found


def main():
    target = ROOT / "desktop/stage/notices"
    target.mkdir(parents=True, exist_ok=True)
    dependencies, unresolved = [], []
    dependencies_to_collect = dependency_closure(DIRECT)
    # The frozen executable contains PyInstaller's exception-licensed bootloader.
    dependencies_to_collect["pyinstaller"] = metadata.distribution("PyInstaller")
    for name, dist in sorted(dependencies_to_collect.items()):
        paths = []
        for item in dist.files or []:
            filename = str(item).replace("\\", "/")
            if not any(part.lower().startswith(("license", "copying", "notice", "copyright", "build_licenses")) for part in Path(filename).parts):
                continue
            source = Path(dist.locate_file(item))
            if not source.is_file() or source.suffix.lower() in {".py", ".pyc"}:
                continue
            relative = Path(name) / filename
            if ".." in relative.parts:
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            paths.append(relative.as_posix())
        if name in {"cadquery-ocp-novtk", "cadquery-ocp-proxy"}:
            supplied = ROOT / "desktop/build/licenses/OCP-7.9.3.0-Apache-2.0.txt"
            if supplied.is_file() and dist.version == "7.9.3.0":
                paths.append("native/" + supplied.name)
        if not paths:
            unresolved.append(name)
        dependencies.append({
            "name": dist.metadata["Name"], "version": dist.version,
            "license_expression": dist.metadata.get("License-Expression"),
            "license_files": paths,
            "home_page": dist.metadata.get("Home-page"),
        })
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", target / "THIRD_PARTY_NOTICES.md")
    shutil.copyfile(ROOT / "backend/documents/fonts/OFL.txt", target / "Noto-Sans-OFL.txt")
    python_candidates = [Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
                         Path(sys.base_prefix) / "LICENSE.txt",
                         Path(sys.base_prefix) / "LICENSE"]
    python_license = next((path for path in python_candidates if path.is_file()), None)
    if python_license:
        shutil.copyfile(python_license, target / "Python-LICENSE.txt")
    else:
        unresolved.append("CPython")
    # Full native-wheel obligations must be reviewed per OS/architecture. A URL
    # or guessed license name is not a substitute for bundled license texts.
    native_review = ROOT / "desktop/build/native-license-review.json"
    review = json.loads(native_review.read_text()) if native_review.is_file() else {"redistribution_reviewed": False}
    for path in (ROOT / "desktop/build/licenses").rglob("*") if (ROOT / "desktop/build/licenses").exists() else []:
        if path.is_file():
            destination = target / "native" / path.relative_to(ROOT / "desktop/build/licenses")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    target_id = f"{sys.platform}-{platform.machine()}"
    report = {
        "schema_version": "violet-notices/1", "dependencies": dependencies,
        "missing_distribution_license_texts": unresolved,
        "native_review": review,
        "target": target_id,
        "release_ready": not unresolved and review.get("redistribution_reviewed") is True and target_id in review.get("reviewed_targets", []),
        "note": "Electron/Chromium licenses are also supplied by electron-builder in the installed app. This report does not authorize customer-file redistribution.",
    }
    (target / "inventory.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Collected notices for {len(dependencies)} Python distributions; release ready: {report['release_ready']}")
    if "--require-release-ready" in sys.argv and not report["release_ready"]:
        raise SystemExit("Release blocked: native/transitive notices need review; see desktop/stage/notices/inventory.json")


if __name__ == "__main__":
    main()
