"""Create a new local, exact-allowlist source checkout. Never runs Git or uploads.

Usage: python desktop/scripts/stage_sources.py /absolute/new-directory
The destination must not exist and must be outside this checkout. After source
files are added, review and explicitly update build/source-files.json; this tool
deliberately never learns new files from a directory sweep.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop/build"))
from backend import library_io
from bundle_policy import PRIVATE_EXTENSIONS, RESOURCE_FILES, application_assets, library_assets

MANIFEST = "desktop/build/source-files.json"
LIBRARY_MANIFEST = "desktop/build/approved-step-library.json"
REPORT = "source-stage-manifest.json"
DENIED_PARTS = {
    "data", "output", "work", "tmp", ".git", ".venv", "venv", "node_modules",
    ".pnpm-store", "__pycache__", "stage", "dist", "uploads", "sources",
    ".aws", ".ssh", ".codex", ".agents",
}
ROOT_FILES = {
    "pyproject.toml", "requirements.txt", "requirements-cad.txt",
    "requirements-desktop-build.txt", "THIRD_PARTY_NOTICES.md",
}
TEST_FILES = {
    "tests/test_agent.py", "tests/test_agent_api.py",
    "tests/__init__.py", "tests/test_desktop_packaging.py",
    "tests/test_desktop_library_io.py", "tests/test_desktop_runtime.py",
    "tests/test_desktop_source_staging.py", "tests/verify_desktop_frozen.py",
    "tests/verify_desktop_windows_install.py",
    "tests/test_finish_geometry.py",
    "tests/test_finish_surfaces.py",
}
FIXED_FILES = ROOT_FILES | TEST_FILES | set(RESOURCE_FILES) | {
    "backend/documents/fonts/NotoSans-Regular.ttf",
    "backend/documents/fonts/NotoSans-Bold.ttf", "backend/documents/fonts/OFL.txt",
    "desktop/package.json", "desktop/pnpm-lock.yaml", "desktop/pnpm-workspace.yaml",
    "desktop/build/violet-backend.spec", "desktop/build/bundle_policy.py",
    "desktop/build/electron_notices.cjs",
    "desktop/build/entitlements.mac.plist", "desktop/build/native-license-review.json",
    "desktop/build/update-public-key.json",
    "desktop/build/licenses/native-evidence-macos-arm64.json",
    "desktop/build/licenses/supplemental-upstream-evidence.json",
    "desktop/update-helpers/mac-install.sh", "desktop/update-helpers/windows-install.ps1",
    MANIFEST, LIBRARY_MANIFEST, ".github/workflows/desktop-build.yml",
    "docs/desktop-release.md",
}
TEXT_EXTENSIONS = {
    ".py", ".js", ".cjs", ".css", ".html", ".svg", ".json", ".yml", ".yaml",
    ".md", ".txt", ".toml", ".spec", ".plist", ".step", ".stp", ".sh", ".ps1",
}
# High-confidence credential formats, not environment-variable names or test
# placeholders. Values are never printed in an error or copied into the report.
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN " + rb"(?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{25,}\b"),
)


def checked_relative(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or any(c in name for c in "\\\0\r\n"):
        raise ValueError("Invalid source-manifest path")
    relative = PurePosixPath(name)
    if (relative.is_absolute() or relative.as_posix() != name
            or any(part in {"..", "."} or ":" in part for part in relative.parts)):
        raise ValueError("Non-canonical source-manifest path")
    if any(part.lower() in DENIED_PARTS or part.lower().startswith(".env") for part in relative.parts):
        raise ValueError(f"Private/generated path is forbidden: {name}")
    if relative.suffix.lower() in PRIVATE_EXTENSIONS:
        raise ValueError(f"Private file format is forbidden: {name}")
    return relative


def source_path_allowed(relative: PurePosixPath) -> bool:
    name, parts = relative.as_posix(), relative.parts
    if name in FIXED_FILES:
        return True
    if parts[0] == "backend":
        return relative.suffix == ".py"
    if parts[0] == "frontend":
        return relative.suffix in {".js", ".css", ".html", ".svg", ".png", ".ico"}
    if parts[0] != "desktop":
        return False
    if len(parts) == 2:
        return relative.suffix in {".cjs", ".js", ".html", ".css"}
    if len(parts) == 3 and parts[1] == "scripts":
        return relative.suffix in {".py", ".cjs"}
    if len(parts) == 3 and parts[1] == "tests":
        return relative.name.endswith(".test.cjs")
    return len(parts) == 4 and parts[1:3] == ("build", "licenses") and relative.suffix == ".txt"


@contextmanager
def opened_source(root: Path, relative: str):
    """Read through no-follow handles, including Windows reparse protection."""
    path = checked_relative(relative)
    handles = [library_io.root_directory(root)]
    try:
        for part in path.parts[:-1]:
            handles.append(library_io.child_directory(handles[-1], part))
        fd = library_io.open_file(handles[-1], path.name)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"Only non-linked regular source files are allowed: {relative}")
            yield stream
    finally:
        for handle in reversed(handles):
            library_io.close_directory(handle)


def read_checked(root: Path, relative: str) -> bytes:
    with opened_source(root, relative) as stream:
        before = os.fstat(stream.fileno())
        content = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_size != len(content) or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns):
        raise ValueError(f"Source changed while staging: {relative}")
    if Path(relative).suffix.lower() in TEXT_EXTENSIONS:
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise ValueError(f"Possible credential in {relative}; value withheld. Review before staging.")
    return content


def source_names(root: Path) -> list[str]:
    manifest = json.loads(read_checked(root, MANIFEST))
    if manifest.get("schema_version") != "violet-source-allowlist/1":
        raise ValueError("Unsupported source allowlist schema")
    names = manifest.get("files")
    if not isinstance(names, list) or not names:
        raise ValueError("Source allowlist needs an explicit nonempty file list")
    seen = set()
    for name in names:
        relative = checked_relative(name)
        if not source_path_allowed(relative):
            raise ValueError(f"File is outside permitted source categories: {name}")
        key = unicodedata.normalize("NFC", name).casefold()
        if key in seen:
            raise ValueError(f"Duplicate or cross-platform-colliding source path: {name}")
        seen.add(key)
    # A newly added application module must trigger review, not a broken build.
    required = {name for _, name in application_assets(root)}
    required.update(FIXED_FILES)
    missing = required.difference(names)
    if missing:
        raise ValueError("Review new source files and update source-files.json: " + ", ".join(sorted(missing)))
    return sorted(names)


def build_plan(root: Path) -> list[dict]:
    root = root.resolve(strict=True)
    names = source_names(root)
    # Reuse the release policy's exact 88-file, hash-pinned supplier approval.
    approved = library_assets(root, root / LIBRARY_MANIFEST)
    library_hashes = {
        item["path"]: item["sha256"]
        for item in json.loads(read_checked(root, LIBRARY_MANIFEST))["files"]
    }
    records = []
    all_names = names + [name for _, name in approved]
    seen = set()
    for name in sorted(all_names):
        key = unicodedata.normalize("NFC", name).casefold()
        if key in seen:
            raise ValueError(f"Duplicate/colliding release input: {name}")
        seen.add(key)
        content = read_checked(root, name)
        sha = hashlib.sha256(content).hexdigest()
        if name in library_hashes and sha != library_hashes[name]:
            raise ValueError(f"Approved supplier source changed: {name}")
        records.append({"path": name, "bytes": len(content), "sha256": sha,
                        "kind": "approved_supplier" if name in library_hashes else "source"})
    return records


def _git_literal(name: str) -> str:
    # Patterns are anchored paths; quote wildcards and spaces literally.
    return "".join("\\" + char if char in "\\*?[]!# " else char for char in name)


def gitignore_for(names: list[str]) -> str:
    """Deny every unreviewed addition, including files inside permitted folders."""
    children: dict[str, set[tuple[str, bool]]] = {"": set()}
    for name in [*names, ".gitignore", REPORT]:
        parts = PurePosixPath(name).parts
        for index, part in enumerate(parts):
            parent = "/".join(parts[:index])
            directory = index < len(parts) - 1
            children.setdefault(parent, set()).add((part, directory))
    lines = [
        "# Generated exact allowlist. Re-run stage_sources.py after reviewing additions.",
        "# New files are ignored by default; never force-add customer data or secrets.",
    ]
    for directory in sorted(children, key=lambda value: (value.count("/"), value)):
        prefix = "/" + directory + "/" if directory else "/"
        lines.append(_git_literal(prefix) + "*")
        for name, is_directory in sorted(children[directory]):
            lines.append("!" + _git_literal(prefix + name) + ("/" if is_directory else ""))
    return "\n".join(lines) + "\n"


def stage(root: Path, destination: Path) -> dict:
    root = root.resolve(strict=True)
    destination = destination.absolute()
    if not destination.parent.is_dir():
        raise ValueError("Destination parent must already exist")
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("Destination must be a new directory outside the source checkout")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing staging directory")
    records = build_plan(root)  # Validate all sources before creating anything.
    destination.mkdir(mode=0o700)
    for row in records:
        content = read_checked(root, row["path"])
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError(f"Source changed after validation: {row['path']}; staging is incomplete")
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content)
    (destination / ".gitignore").write_text(gitignore_for([row["path"] for row in records]), encoding="utf-8")
    report = {
        "schema_version": "violet-staged-sources/1",
        "private_distribution_only": json.loads(read_checked(root, LIBRARY_MANIFEST)).get("redistribution_scope") != "public",
        "git_initialized": False,
        "uploaded": False,
        "file_count": len(records),
        "total_bytes": sum(row["bytes"] for row in records),
        "supplier_files": sum(row["kind"] == "approved_supplier" for row in records),
        "files": records,
        "note": "Contains source and explicitly approved supplier assets only. "
                "No personal projects, original drawings, build outputs or credentials. "
                "Secret-pattern scanning is supplemental, not proof of complete manual review.",
    }
    (destination / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        report = stage(ROOT, args.destination)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Source staging refused: {error}\n")
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
