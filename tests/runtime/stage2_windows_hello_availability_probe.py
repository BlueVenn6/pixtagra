"""Probe UserConsentVerifier availability without requesting Windows Hello UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pixtagra_windows_hello import WindowsHelloConsentBackend


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pixtagra_hello_availability_") as root_text:
        root = Path(root_text)
        home = root / "home"
        config = root / "config"
        vault = home / ".pictureseo"
        home.mkdir()
        config.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        os.environ["PSS_CONFIG_DIR"] = str(config)
        os.environ["PSS_CONFIG_FILE"] = str(config / "config.json")

        hwnd_created = False
        availability_status = "interop_unavailable"
        interop_status = "interop_unavailable"
        error_type = None
        root_window = None
        try:
            import tkinter as tk

            root_window = tk.Tk()
            root_window.withdraw()
            root_window.update_idletasks()
            root_window.update()
            hwnd = int(root_window.winfo_id())
            hwnd_created = hwnd > 0
            result = WindowsHelloConsentBackend().check_availability(hwnd)
            availability_status = result.status
            interop_status = "verified" if result.interop_verified else result.status
            error_type = result.error_type
        except Exception as exc:
            error_type = type(exc).__name__
        finally:
            if root_window is not None:
                try:
                    root_window.destroy()
                except Exception:
                    pass

        print(json.dumps({
            "availability_status": availability_status,
            "error_type": error_type,
            "hello_prompt_requested": 0,
            "hwnd_created": hwnd_created,
            "interop_status": interop_status,
            "network_requests": 0,
            "platform": sys.platform,
            "real_credential_manager_access": 0,
            "real_vault_access": 0,
        }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
