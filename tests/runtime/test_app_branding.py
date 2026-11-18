"""Preserve the supplied artwork and catch CTk's delayed icon replacement."""

from pathlib import Path
import ctypes
from ctypes import wintypes
import sys
import struct
import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageChops
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pixtagra_branding import APP_ICON, AppToplevel, apply_app_icon


def _icon_pixels(icon):
    class IconInfo(ctypes.Structure):
        _fields_ = [("is_icon", wintypes.BOOL), ("x", wintypes.DWORD),
                    ("y", wintypes.DWORD), ("mask", wintypes.HBITMAP),
                    ("color", wintypes.HBITMAP)]

    class Bitmap(ctypes.Structure):
        _fields_ = [("type", wintypes.LONG), ("width", wintypes.LONG),
                    ("height", wintypes.LONG), ("stride", wintypes.LONG),
                    ("planes", wintypes.WORD), ("depth", wintypes.WORD),
                    ("bits", ctypes.c_void_p)]

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(IconInfo)]
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetBitmapBits.argtypes = [wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    info = IconInfo()
    assert user32.GetIconInfo(icon, ctypes.byref(info))
    try:
        bitmap = Bitmap()
        assert gdi32.GetObjectW(info.color, ctypes.sizeof(bitmap), ctypes.byref(bitmap))
        length = bitmap.height * bitmap.stride
        pixels = ctypes.create_string_buffer(length)
        assert gdi32.GetBitmapBits(info.color, length, pixels) == length
        return bitmap.width, bitmap.height, pixels.raw
    finally:
        gdi32.DeleteObject(info.color)
        gdi32.DeleteObject(info.mask)


def _window_icon_pixels(window):
    # Inspect only the native handles owned by these test widgets, without
    # sending user input or inspecting unrelated desktop windows.
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetClassLongPtrW.restype = ctypes.c_size_t
    hwnd = user32.GetParent(window.winfo_id())
    assert hwnd
    icons = []
    for kind in (0, 1):  # WM_GETICON: ICON_SMALL and ICON_BIG
        icon = user32.SendMessageW(hwnd, 0x7F, kind, 0)
        if not icon:
            # Tk's default= icon is a window-class icon. Windows uses it when
            # WM_GETICON returns zero, including for native child dialogs.
            icon = user32.GetClassLongPtrW(hwnd, -34 if kind == 0 else -14)
        assert icon
        icons.append(_icon_pixels(icon))
    return icons


def test_ico_contains_all_requested_sizes_and_preserves_original_pixels():
    sizes = {(n, n) for n in (16, 24, 32, 48, 64, 128, 256)}
    data = APP_ICON.read_bytes()
    assert struct.unpack_from("<HHH", data) == (0, 1, 7)
    assert data[6:8] == b"\x00\x00"  # 256px first for Tk on Windows
    with Image.open(APP_ICON.with_suffix(".png")) as source, Image.open(APP_ICON) as ico:
        assert source.mode == "RGBA"
        assert source.size == (1024, 1024)
        assert source.getchannel("A").getextrema() == (0, 255)
        assert ico.ico.sizes() == sizes
        for size in sizes:
            expected = source.resize(size, Image.Resampling.LANCZOS)
            actual = ico.ico.getimage(size).convert("RGBA")
            assert ImageChops.difference(actual, expected).getbbox(alpha_only=False) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ICO window integration")
def test_windows_keep_official_icon_after_ctk_delayed_callbacks():
    root = ctk.CTk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    try:
        # A cold packaged launch can execute CTk's delayed callback inside
        # its constructor before App gets to apply the official branding.
        framework_icon = Path(ctk.__file__).resolve().parent / "assets" / "icons" / "CustomTkinter_icon_Windows.ico"
        root.iconbitmap(str(framework_icon))
        apply_app_icon(root)
        dialog = AppToplevel(root)
        dialog.withdraw()
        native_dialog = tk.Toplevel(root)
        native_dialog.withdraw()
        root.update_idletasks()
        expected = _window_icon_pixels(dialog)
        root.after(450, root.quit)
        root.mainloop()
        for window in (root, dialog, native_dialog):
            assert _window_icon_pixels(window) == expected
        assert not errors
    finally:
        root.destroy()
