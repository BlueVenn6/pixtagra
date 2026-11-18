"""Pure popup geometry and Windows monitor work-area helpers."""
import ctypes
import os
from datetime import datetime

def _format_date_picker_value(year, month, day):
    """Return the exact display format accepted by the metadata writer."""
    return datetime(int(year), int(month), int(day)).strftime("%m/%d/%Y")


def _date_picker_geometry(
    anchor_x, anchor_y, anchor_width, anchor_height,
    popup_width, popup_height, screen_x, screen_y, screen_width, screen_height,
):
    """Place a date popup by its trigger without letting it escape the screen."""
    margin = 8
    left = int(anchor_x) + int(anchor_width) - int(popup_width)
    below = int(anchor_y) + int(anchor_height) + 6
    screen_right = int(screen_x) + int(screen_width)
    screen_bottom = int(screen_y) + int(screen_height)
    x = min(max(int(screen_x) + margin, left), screen_right - int(popup_width) - margin)
    y = below
    if y + int(popup_height) > screen_bottom - margin:
        y = int(anchor_y) - int(popup_height) - 6
    y = min(max(int(screen_y) + margin, y), screen_bottom - int(popup_height) - margin)
    return x, y


def _widget_monitor_work_area(widget):
    """Return the work area for the monitor containing a Tk widget."""
    fallback = (
        widget.winfo_vrootx(),
        widget.winfo_vrooty(),
        widget.winfo_vrootwidth(),
        widget.winfo_vrootheight(),
    )
    if os.name != "nt":
        return fallback

    try:
        from ctypes import wintypes

        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", Rect),
                ("rcWork", Rect),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = [wintypes.HWND, wintypes.DWORD]
        monitor_from_window.restype = wintypes.HANDLE
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        get_monitor_info.restype = wintypes.BOOL

        monitor = monitor_from_window(wintypes.HWND(widget.winfo_id()), 2)
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not monitor or not get_monitor_info(monitor, ctypes.byref(info)):
            return fallback
        work = info.rcWork
        return work.left, work.top, work.right - work.left, work.bottom - work.top
    except (AttributeError, OSError, TypeError, ValueError):
        return fallback


def _centered_window_geometry(
    requested_width,
    requested_height,
    screen_x,
    screen_y,
    screen_width,
    screen_height,
    margin=16,
):
    """Return a clamped, centered geometry inside a monitor work area."""
    available_width = max(1, int(screen_width) - int(margin) * 2)
    available_height = max(1, int(screen_height) - int(margin) * 2)
    width = min(max(1, int(requested_width)), available_width)
    height = min(max(1, int(requested_height)), available_height)
    x = int(screen_x) + (int(screen_width) - width) // 2
    y = int(screen_y) + (int(screen_height) - height) // 2
    return width, height, x, y


def _centered_ctk_window_geometry(widget, requested_width, requested_height, work_area, margin=16):
    """Center a CTk window using logical dimensions and physical screen coordinates."""
    screen_x, screen_y, screen_width, screen_height = work_area
    try:
        scaling = float(widget._get_window_scaling())
    except (AttributeError, TypeError, ValueError):
        scaling = 1.0
    if scaling <= 0:
        scaling = 1.0
    logical_width = max(1, int(round(screen_width / scaling)))
    logical_height = max(1, int(round(screen_height / scaling)))
    width, height, _x, _y = _centered_window_geometry(
        requested_width,
        requested_height,
        0,
        0,
        logical_width,
        logical_height,
        margin,
    )
    physical_width = int(round(width * scaling))
    physical_height = int(round(height * scaling))
    x = int(screen_x) + (int(screen_width) - physical_width) // 2
    y = int(screen_y) + (int(screen_height) - physical_height) // 2
    return width, height, x, y

