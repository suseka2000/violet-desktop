# PyInstaller executes this as Python. The staged manifest is the only data input.
import json
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_delvewheel_libs_directory

ROOT = Path(SPECPATH).parent.parent
manifest = json.loads((ROOT / "desktop/stage/bundle-inputs.json").read_text(encoding="utf-8"))
datas = [(row["source"], str(Path(row["path"]).parent)) for row in manifest["files"]]
binaries, hiddenimports = [], collect_submodules("backend")
for package in ("OCP", "pypdfium2", "pypdfium2_raw", "shapely", "certifi"):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
# Windows OCP wheels keep 70 DLLs beside OCP/, not inside it. Collecting only
# OCP's package directory produces an app that starts but cannot build STEP.
datas, binaries = collect_delvewheel_libs_directory(
    "OCP", libdir_name="cadquery_ocp_novtk.libs", datas=datas, binaries=binaries)

a = Analysis(
    [str(ROOT / "backend/desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "matplotlib", "IPython", "notebook"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="violet-backend",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=os.environ.get("VIOLET_PYTHON_CODESIGN_IDENTITY"),
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="backend")
