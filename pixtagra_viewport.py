"""A logical scrollbar over a stationary canvas containing a small editor pool.

Tk's native canvas scroll first moves every embedded window out of the viewport.
Repositioning recycled windows afterwards causes another map/paint of every child.
Keep the native canvas at zero and place a bounded pool of rows as its children.
Offscreen rows stay mapped and are clipped by the parent, so crossing a row
boundary does not remap every native editor.
"""
import tkinter as tk
import sys


def enable_composited_painting(widget):
    """Present this viewport's native children together on Windows.

    Reusing editors prevents remapping but does not buffer HWND paints. Without
    WS_EX_COMPOSITED a move can expose the parent background before its text and
    image children repaint. Keep buffering local to the scrolling viewport.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_style = user32.GetWindowLongW
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = wintypes.LONG
    set_style = user32.SetWindowLongW
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    set_style.restype = wintypes.LONG
    hwnd = widget.winfo_id()
    style = get_style(hwnd, -20)  # GWL_EXSTYLE; styles are 32-bit on Win64 too.
    ctypes.set_last_error(0)
    previous = set_style(hwnd, -20, style | 0x02000000)
    if previous == 0 and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    if not get_style(hwnd, -20) & 0x02000000:
        raise RuntimeError("Viewport double buffering could not be enabled")
    return True


class ViewportCanvas(tk.Canvas):
    def __init__(self, *args, **kwargs):
        self._offset = 0.0
        self._extent = 1.0
        self._scroll_callback = None
        self._notify_job = None
        super().__init__(*args, **kwargs)
        # Tk moves/clips native child windows while reusing the editor pool.
        # WS_EX_COMPOSITED on this scrolling ancestor retains stale child
        # pixels after moves; let Tk invalidate and paint the viewport normally.
        self.composited_painting = False
        self.bind("<Configure>", lambda event: self._set_offset(self._offset), add="+")

    @property
    def scroll_offset(self):
        return self._offset

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {}, **kwargs)
        if "yscrollcommand" in options:
            self._scroll_callback = options.pop("yscrollcommand")
        if "scrollregion" in options:
            region = options.pop("scrollregion")
            self._extent = max(1.0, float(region[3]) - float(region[1]))
            self._set_offset(self._offset)
        if options:
            return super().configure(**options)

    config = configure

    def _set_offset(self, value):
        # Whole-pixel offsets: a scrollbar drag reports fractional fractions,
        # and a fractional y makes every placed row a new geometry, so each
        # drag event re-placed every visible editor and repainted its children.
        self._offset = float(max(0, round(min(float(value), max(0, self._extent - self.winfo_height())))))
        if self._notify_job is None:
            self._notify_job = self.after_idle(self._notify)

    def _notify(self):
        self._notify_job = None
        if self._scroll_callback is not None:
            self._scroll_callback(*self.yview())

    def yview(self, *args):
        if not args:
            return self._offset / self._extent, min(1.0, (self._offset + self.winfo_height()) / self._extent)
        if args[0] == "moveto":
            return self.yview_moveto(args[1])
        if args[0] == "scroll":
            return self.yview_scroll(args[1], args[2])
        raise ValueError(f"Unsupported scroll action: {args[0]}")

    def yview_moveto(self, fraction):
        self._set_offset(float(fraction) * self._extent)

    def yview_scroll(self, number, what):
        step = max(1, float(self.cget("yscrollincrement"))) if what == "units" else self.winfo_height() * 0.9
        self._set_offset(self._offset + int(number) * step)

    def canvasy(self, screeny, gridspacing=None):
        value = float(screeny) + self._offset
        return round(value / gridspacing) * gridspacing if gridspacing else value

    def destroy(self):
        if self._notify_job is not None:
            self.after_cancel(self._notify_job)
        super().destroy()
