"""The single application icon used by source and packaged Windows windows."""

import ctypes
from pathlib import Path
import sys

import customtkinter as ctk


APP_USER_MODEL_ID = "PixTagra.PixTagra"
APP_ICON = Path(__file__).resolve().parent / "assets" / "branding" / "PixTagra_App_Icon.ico"


def configure_windows_app_id():
    if sys.platform == "win32":
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        if result < 0:
            print(f"PixTagra taskbar identity could not be set: HRESULT {result}", file=sys.stderr)


def apply_app_icon(window):
    if sys.platform == "win32":
        # CTk may already have set a window-specific icon during a slow
        # constructor. default= alone only changes the inherited class icon.
        window.iconbitmap(str(APP_ICON))
        window.iconbitmap(default=str(APP_ICON))


class AppToplevel(ctk.CTkToplevel):
    """Keep CTk's delayed default-icon callback on the official application icon."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_app_icon(self)

    def iconbitmap(self, bitmap=None, default=None):
        if sys.platform == "win32" and (bitmap is not None or default is not None):
            bitmap = str(APP_ICON)
            if default is not None:
                default = str(APP_ICON)
        return super().iconbitmap(bitmap, default)

    def title(self, value=None):
        from pixtagra_localization import translate
        return super().title(translate(value) if value is not None else value)
