# Controlled one-folder Windows RC specification. It carries no project data directories.
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules


PROJECT_ROOT = Path(SPEC).resolve().parent
GENERATED_DIR = Path(os.environ["PIXTAGRA_GENERATED_DIR"]).resolve()
VERSION_FILE = os.environ["PIXTAGRA_VERSION_FILE"]
APP_ICON = PROJECT_ROOT / "assets" / "branding" / "PixTagra_App_Icon.ico"
if not APP_ICON.is_file():
    raise RuntimeError("Missing official PixTagra application icon")

datas = []
binaries = []
hiddenimports = [
    "customtkinter",
    "darkdetect",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "PIL",
    "PIL.Image",
    "piexif",
    "piexif.helper",
    "cryptography.hazmat.backends.openssl",
    "cryptography.hazmat.primitives.ciphers.aead",
    "pixtagra_credentials",
    "pixtagra_credential_migration",
    "pixtagra_credential_policy",
    "pixtagra_live_credentials",
    "pixtagra_windows_hello",
    "pixtagra_packaged_selftest",
    "pixtagra_workspace_probe",
    "pixtagra_release",
    "pixtagra_build_info",
]

package_datas, package_binaries, package_hiddenimports = collect_all("customtkinter")
datas += package_datas
binaries += package_binaries
hiddenimports += package_hiddenimports

# The controlled build host has tkinter installed, but PyInstaller's Tcl/Tk
# probe can be unavailable in headless CI sessions. Keep the GUI runtime
# explicit so the packaged credential probes and the real GUI use the same
# dependencies.
PYTHON_ROOT = Path(sys.base_prefix)
TKINTER_ROOT = PYTHON_ROOT / "Lib" / "tkinter"
TCL_ROOT = PYTHON_ROOT / "tcl"
DLL_ROOT = PYTHON_ROOT / "DLLs"
required_runtime_paths = (
    TKINTER_ROOT / "__init__.py",
    TCL_ROOT / "tcl8.6" / "init.tcl",
    TCL_ROOT / "tk8.6" / "tk.tcl",
    DLL_ROOT / "_tkinter.pyd",
    DLL_ROOT / "tcl86t.dll",
    DLL_ROOT / "tk86t.dll",
)
missing_runtime_paths = [path for path in required_runtime_paths if not path.exists()]
if missing_runtime_paths:
    raise RuntimeError("Missing tkinter runtime files: " + ", ".join(map(str, missing_runtime_paths)))

hiddenimports += ["_tkinter"]
hiddenimports += collect_submodules("tkinter")
datas += [
    (str(APP_ICON), "assets/branding"),
    (str(TCL_ROOT / "tcl8.6"), "_tcl_data"),
    (str(TCL_ROOT / "tk8.6"), "_tk_data"),
    (str(PROJECT_ROOT / "docs" / "legal" / "EULA.md"), "legal"),
    (str(PROJECT_ROOT / "docs" / "legal" / "PRIVACY.md"), "legal"),
    (str(PROJECT_ROOT / "docs" / "legal" / "REFUND.md"), "legal"),
    (str(PROJECT_ROOT / "docs" / "legal" / "CODE_SIGNING_POLICY.md"), "legal"),
]
binaries += [
    (str(DLL_ROOT / "_tkinter.pyd"), "."),
    (str(DLL_ROOT / "tcl86t.dll"), "."),
    (str(DLL_ROOT / "tk86t.dll"), "."),
]

for package_name in ("PIL", "piexif", "cryptography"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    [str(PROJECT_ROOT / "pixtagra.py")],
    pathex=[str(GENERATED_DIR), str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[str(PROJECT_ROOT / "build_hooks")],
    excludes=["pytest", "unittest", "pydoc"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PixTagra",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=VERSION_FILE,
    icon=str(APP_ICON),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="PixTagra",
)
