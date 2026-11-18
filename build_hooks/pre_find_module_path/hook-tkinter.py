"""Keep tkinter discoverable when PyInstaller cannot probe Tcl headlessly."""

from pathlib import Path
import sys


def pre_find_module_path(hook_api):
    python_lib = Path(sys.base_prefix) / "Lib"
    if (python_lib / "tkinter").is_dir():
        hook_api.search_dirs = [str(python_lib)]
