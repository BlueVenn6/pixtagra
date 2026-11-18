"""Width-aware action rows for the desktop workspace."""

import customtkinter as ctk


def flow_positions(widths, available, gap=6):
    """Keep controls whole and wrap before the next control would be clipped."""
    row = column = used = 0
    positions = []
    for width in widths:
        if column and used + gap + width > available:
            row, column, used = row + 1, 0, 0
        positions.append((row, column))
        used += (gap if column else 0) + width
        column += 1
    return positions


class ActionRow(ctk.CTkFrame):
    """Wrap only on width/content changes, never during canvas scrolling."""

    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=0, **kwargs)
        self._items = []
        self._layout_job = None
        self._positions = None
        self.grid_propagate(False)
        self.bind("<Configure>", self.request_layout, add="+")

    def add(self, widget):
        self._items.append((widget, widget.cget("width")))
        self.request_layout()
        return widget

    def request_layout(self, _event=None):
        if self._layout_job is None:
            self._layout_job = self.after_idle(self._layout)

    def _layout(self):
        self._layout_job = None
        scale = self._get_widget_scaling()
        widths = []
        for widget, preferred in self._items:
            label = getattr(widget, "_text_label", None) or getattr(widget, "_label", None)
            text_width = label.winfo_reqwidth() / scale if label is not None else 0
            width = max(preferred, text_width + 26)
            if round(widget.cget("width")) != round(width):
                widget.configure(width=width)
            widths.append(width)
        positions = flow_positions(widths, max(1, self.winfo_width() / scale - 24))
        row_heights = {}
        for (widget, _), (row, _column) in zip(self._items, positions):
            row_heights[row] = max(row_heights.get(row, 0), widget.cget("height") + 10)
        height = sum(row_heights.values()) or 1
        layout = (positions, widths, list(row_heights.values()))
        if layout != self._positions:
            x = 12
            for (widget, _), width, (row, column) in zip(self._items, widths, positions):
                if column == 0:
                    x = 12
                y = sum(row_heights[r] for r in range(row)) + 5
                widget.place(x=x, y=y)
                x += width + 6
            self._positions = layout
        if round(self.cget("height")) != round(height):
            self.configure(height=height)

    def destroy(self):
        if self._layout_job is not None:
            self.after_cancel(self._layout_job)
            self._layout_job = None
        super().destroy()
