"""Explicit distributable inputs. Never sweep the checkout into an installer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

RESOURCE_FILES = (
    "resources/vendor/manual-facts.json",
    "resources/vendor/profile-facts.json",
    "resources/vendor/public-installation-facts.json",
    "resources/vendor/mount-context-symbols.json",
)
FORBIDDEN_PARTS = {
    "data", "output", "tmp", ".git", ".venv", "node_modules", "__pycache__",
    ".aws", ".codex", "uploads", "sources",
}
PRIVATE_EXTENSIONS = {".sqlite", ".sqlite3", ".db", ".dwg", ".dxf", ".sldprt", ".sldasm", ".a3d", ".m3d", ".bak", ".log", ".pem", ".p12", ".pfx", ".key"}


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _plain_file(path: Path, root: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Not a regular distributable file: {path}")
    relative = path.relative_to(root)
    if any((root / Path(*relative.parts[:n])).is_symlink() for n in range(1, len(relative.parts) + 1)):
        raise ValueError(f"Symbolic link in distributable path: {relative}")
    if path.stat().st_nlink != 1:
        raise ValueError(f"Hard-linked distributable file: {relative}")


def application_assets(root: Path) -> list[tuple[Path, str]]:
    root = root.resolve()
    files: list[tuple[Path, str]] = []
    for directory, suffixes in (("frontend", {".js", ".css", ".html", ".svg", ".png", ".ico"}), ("backend", {".py"})):
        for path in sorted((root / directory).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in FORBIDDEN_PARTS for part in relative.parts) or path.name.endswith(".test.js"):
                continue
            if path.suffix.lower() not in suffixes:
                continue
            _plain_file(path, root)
            files.append((path, relative.as_posix()))
    for relative in (*RESOURCE_FILES, "backend/documents/fonts/NotoSans-Regular.ttf",
                     "backend/documents/fonts/NotoSans-Bold.ttf", "backend/documents/fonts/OFL.txt",
                     "THIRD_PARTY_NOTICES.md"):
        path = root / relative
        _plain_file(path, root)
        files.append((path, relative))
    return files


def library_assets(root: Path, manifest_path: Path | None) -> list[tuple[Path, str]]:
    """Only an explicitly reviewed exact-file manifest enables private assets."""
    if manifest_path is None:
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "violet-private-library/1" or manifest.get("redistribution_confirmed") is not True:
        raise ValueError("Library manifest needs explicit redistribution confirmation")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("Library files must be an explicit list; globs are not supported")
    root = root.resolve()
    result, seen = [], set()
    for item in entries:
        name = item["path"]
        path = PurePosixPath(name)
        if path.is_absolute() or "\\" in name or ".." in path.parts or not path.parts:
            raise ValueError("Unsafe library path")
        if path.parts[0] != "for_interior-rd-generator" or any(part in FORBIDDEN_PARTS for part in path.parts):
            raise ValueError("Library entry is outside supplier material folder")
        if path.suffix.lower() not in {".stp", ".step", ".pdf", ".jpg", ".jpeg"}:
            raise ValueError("Only explicitly selected STEP profiles and manual pages may be bundled")
        source = root / name
        _plain_file(source, root)
        actual = digest(source)
        if actual != item.get("sha256"):
            raise ValueError(f"Library source changed after review: {name}")
        if name in seen:
            raise ValueError("Duplicate library path")
        seen.add(name)
        result.append((source, path.as_posix()))
    return result


def inventory(files: list[tuple[Path, str]]) -> list[dict]:
    return [{"path": relative, "bytes": source.stat().st_size, "sha256": digest(source)}
            for source, relative in files]
