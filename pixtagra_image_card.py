"""Reusable image-card editor view; record state is owned by the application.

This module depends on the keyword policy but does not import the App, network
transports, credential storage or image metadata writers.
"""

from pathlib import Path
import tkinter as tk
import customtkinter as ctk
from customtkinter.windows.widgets.scaling import CTkScalingBaseClass
from tkinter import messagebox
from tkinter import font as tkfont

from pixtagra_keywords import KEYWORD_MINIMUM_COUNT, KEYWORD_MAXIMUM_COUNT, PROMPT_MODES, rank_keywords_for_stock
from pixtagra_quality import keyword_review_detail
from pixtagra_branding import AppToplevel
from pixtagra_language import metadata_language, split_keywords
from pixtagra_keywords import keyword_limits
from pixtagra_localization import ctk, tk, messagebox


class ImageCard(tk.Frame, CTkScalingBaseClass):
    def __init__(self, parent, app):
        # A native border avoids the full-card backing canvas that CTkFrame
        # redraws whenever this large embedded window moves. Keep its width
        # fixed: saved/unsaved styling must not relayout every child editor.
        tk.Frame.__init__(self, parent, background="#FFFFFF", bd=0,
                          highlightbackground="#E5E7EB", highlightthickness=1)
        CTkScalingBaseClass.__init__(self, scaling_type="widget")
        self.configure(highlightthickness=max(1, round(self._apply_widget_scaling(1))))
        self.configure(height=260)
        self.app = app
        self.record = None
        self.path = ""
        self.thumb_image = None
        self._native_labels = []
        self._editor_layout = None

        # Moving nested native windows repaints each layer on Windows. Keep
        # the live editors and controls in one container, with no border or
        # layout frames underneath each text field.
        self.main = tk.Frame(self, background="#FFFFFF", bd=0)
        self.main.pack(fill="both", expand=True)
        self.var_sel = tk.BooleanVar(master=app, value=True)
        self._selection_view_var = self.var_sel
        self._selection_trace = None
        self.sel_box = ctk.CTkCheckBox(self.main, text="", width=22, height=22,
                                      checkbox_width=22, checkbox_height=22, variable=self.var_sel,
                                      command=self._on_selection_clicked)
        self.img_lbl = ctk.CTkLabel(self.main, text="No preview", width=128, height=128,
                                    fg_color="#F3F4F6", corner_radius=8, font=("Segoe UI", 12))
        self.status = self._label(self.main, "● Unprocessed", 11, "#94A3B8", bold=True)
        self.manual_lbl = self._label(self.main, "", 9, "#B45309", bold=True)
        self.file_lbl = self._label(self.main, "", 9, "#64748B")
        self._field_headings = [
            self._label(self.main, text, 12, "#334155", bold=True)
            for text in ("Title (8-12 words)", "Description (30-50 words)", "Keywords (20–35 suggested)")
        ]
        self.title_e = tk.Entry(self.main, relief="flat", bd=0, font=("Segoe UI", 13),
                                background="#F9FAFB", foreground="#111827", insertbackground="#111827",
                                highlightbackground="#597D87", highlightthickness=1)
        self.desc_e = self._create_text_editor(self.main, 4)
        self.kw_e = self._create_text_editor(self.main, 3)
        self.kw_count_lbl = self._label(self.main, "0 tags", 11, "#DC2626", bold=True)
        self.kw_expand_btn = self._button("Expand editor", "#64748B", "#475569", 10, self._open_keyword_editor)
        self.kw_scroll = tk.Scrollbar(self.main, orient="vertical", command=self.kw_e.yview)
        self.kw_e.configure(yscrollcommand=self.kw_scroll.set)
        self.regen_btn = self._button("↻ Regenerate", "#0390AA", "#027A8F", 10, self._on_regen)
        self.undo_btn = self._button("Undo generation", "#64748B", "#475569", 9, self._on_undo_generation)
        self.remove_btn = self._button("× Remove", "#C17930", "#A16207", 10, self._on_remove)
        for widget in (self.title_e, self.desc_e, self.kw_e):
            widget.bind("<FocusOut>", self._sync_now)
            widget.bind("<KeyRelease>", self._sync_now)
            self.app._bind_edit_context_menu(widget)
        self.kw_e.bind("<MouseWheel>", self._on_keyword_mousewheel)
        self.kw_scroll.bind("<MouseWheel>", self._on_keyword_mousewheel)
        self._bind_scroll_targets(
            self, self.main, self.img_lbl, self.status, self.manual_lbl, self.file_lbl,
            self.regen_btn, self.undo_btn, self.remove_btn, self.sel_box, *self._field_headings,
        )
        self._scale_native_editors()
        self.main.bind("<Configure>", self._layout_editors, add="+")


    def _label(self, parent, text, size, color, bold=False):
        label = tk.Label(parent, text=text, foreground=color, background="#FFFFFF", bd=0, padx=0)
        self._native_labels.append((label, size, bold))
        return label

    def _button(self, text, background, activebackground, size, command):
        # Keep the existing rounded controls; their backing canvas must receive
        # the same logical size as the physical placement below.
        return ctk.CTkButton(
            self.main, text=text, command=command, corner_radius=8,
            fg_color=background, hover_color=activebackground,
            text_color="white", text_color_disabled="#CBD5E1",
            font=("Segoe UI", size, "bold"), border_width=0,
        )

    def _bind_scroll_targets(self, *widgets):
        for widget in widgets:
            widget.bind("<Enter>", self.app._bind_canvas_mousewheel, add="+")
            widget.bind("<Leave>", self.app._unbind_canvas_mousewheel, add="+")
            widget.bind("<MouseWheel>", self.app._on_mousewheel, add="+")

    def _create_text_editor(self, parent, lines):
        return tk.Text(
            parent,
            height=lines,
            wrap="word",
            relief="flat",
            bd=0,
            padx=4,
            pady=2,
            undo=False,
            font=("Segoe UI", 12),
            background="#F9FAFB",
            foreground="#111827",
            insertbackground="#111827",
            highlightthickness=0,
        )

    def _scale_native_editors(self):
        for label, size, bold in getattr(self, "_native_labels", ()):
            font = tkfont.Font(family="Segoe UI", size=-max(1, round(self._apply_widget_scaling(size))),
                               weight="bold" if bold else "normal")
            label.configure(font=font)
            label._scaled_font = font
        # Native Tk point fonts do not follow CTk's per-monitor pixel scaling.
        for name, size in (("title_e", 14), ("desc_e", 13), ("kw_e", 13)):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(font=("Segoe UI", -max(1, round(self._apply_widget_scaling(size)))),
                                 highlightthickness=max(1, round(self._apply_widget_scaling(1))))
        if hasattr(self, "kw_scroll"):
            pixels = lambda value: round(self._apply_widget_scaling(value))
            self.main.pack_configure(padx=pixels(14), pady=pixels(8))
            self.main.configure(height=pixels(260))
            self.title_e.configure(bd=pixels(5))
            self.desc_e.configure(padx=pixels(10), pady=pixels(5))
            self.kw_e.configure(padx=pixels(10), pady=pixels(5))
            self._editor_layout = None
            self._layout_editors()

    def _layout_editors(self, _event=None):
        scale = self._get_widget_scaling()
        width = self.main.winfo_width() / scale
        if width < 400 or self._editor_layout == (width, scale):
            return
        self._editor_layout = (width, scale)

        def position(widget, x, y, width, height):
            # This container owns every coordinate in physical pixels. CTk
            # still owns control drawing and font scaling; do not scale twice.
            if isinstance(widget, ctk.CTkButton):
                widget.configure(width=width, height=height)
            tk.Place.place_configure(widget, x=round(x * scale), y=round(y * scale),
                                     width=round(width * scale), height=round(height * scale))

        position(self.sel_box, 82, 4, 24, 24)
        position(self.img_lbl, 31, 34, 128, 128)
        for widget, y in ((self.status, 172), (self.manual_lbl, 207), (self.file_lbl, 238)):
            position(widget, 0, y, 190, 28)
        right = width - 104
        left = 208
        editor_width = right - 18 - left
        for widget, y in zip(self._field_headings, (0, 73, 166)):
            position(widget, left, y, editor_width, 28)
            widget.configure(anchor="w")
        # Leave room for the count and Expand action even in a narrow window.
        self._field_headings[2].configure(
            text="20–35 tags" if editor_width < 440 else "Keywords (20–35 suggested)")
        position(self.title_e, left, 32, editor_width, 34)
        position(self.desc_e, left, 105, editor_width, 58)
        position(self.kw_e, left, 198, editor_width - 14, 50)
        position(self.kw_scroll, left + editor_width - 14, 198, 14, 50)
        position(self.kw_count_lbl, left + editor_width - 180, 166, 64, 28)
        position(self.kw_expand_btn, left + editor_width - 112, 168, 112, 24)
        for widget, y, height in ((self.regen_btn, 36, 38), (self.undo_btn, 84, 30), (self.remove_btn, 124, 38)):
            position(widget, right, y, 96, height)


    def _set_scaling(self, new_widget_scaling, new_window_scaling):
        CTkScalingBaseClass._set_scaling(self, new_widget_scaling, new_window_scaling)
        self.configure(highlightthickness=max(1, round(self._apply_widget_scaling(1))))
        self._scale_native_editors()
        if hasattr(self, "app"):
            self.app._card_height_ready = False

    def destroy(self):
        self._detach_selection_trace()
        CTkScalingBaseClass.destroy(self)
        tk.Frame.destroy(self)

    @staticmethod
    def _parse_keyword_text(value):
        return split_keywords(value)

    def _refresh_keyword_count(self):
        count = len(self._parse_keyword_text(self._get_text_widget(self.kw_e)))
        minimum, maximum = keyword_limits(self.record._cached_data if self.record is not None else {})
        if getattr(self, "_displayed_keyword_count", None) == (count, minimum, maximum):
            return
        self._displayed_keyword_count = (count, minimum, maximum)
        color = "#059669" if minimum <= count <= maximum else "#DC2626"
        self.kw_count_lbl.configure(text=f"{count} tags", foreground=color)

    def _on_keyword_mousewheel(self, event):
        delta = -1 if getattr(event, "delta", 0) > 0 else 1
        self.kw_e.yview_scroll(delta, "units")
        return "break"

    def _set_text_widget(self, widget, value):
        if self._get_text_widget(widget) == value:
            return
        state = str(widget.cget("state"))
        if state == "disabled":
            widget.configure(state="normal")
        widget.delete("1.0", "end")
        if value:
            widget.insert("1.0", value)
        if state == "disabled":
            widget.configure(state="disabled")

    def _get_text_widget(self, widget):
        return widget.get("1.0", "end").strip()

    def _set_editor_state(self, enabled):
        if getattr(self, "_editor_enabled", None) == enabled:
            return
        self._editor_enabled = enabled
        self.title_e.configure(state="normal")
        self.title_e.configure(
            state="normal" if enabled else "disabled",
            background="#F9FAFB" if enabled else "#E5E7EB",
            disabledbackground="#E5E7EB",
            disabledforeground="#111827",
            highlightbackground="#597D87" if enabled else "#CBD5E1",
        )
        for widget in (self.desc_e, self.kw_e):
            widget.configure(
                background="#F9FAFB" if enabled else "#E5E7EB",
                highlightbackground="#597D87" if enabled else "#CBD5E1",
                foreground="#111827",
                insertbackground="#111827" if enabled else "#6B7280",
                state="normal" if enabled else "disabled",
            )
        # Tk's classic Scrollbar has no ``state`` option (unlike ttk's
        # scrollbar). Keep it usable while the editor is enabled/disabled;
        # the Text widget itself controls whether editing is allowed.
        self.kw_expand_btn.configure(state="normal" if enabled else "disabled")

    def _sync_now(self, _event=None):
        self.sync_to_record()
        self._refresh_keyword_count()

    def _detach_selection_trace(self):
        binding = self.__dict__.get("_selection_trace")
        if binding is not None:
            variable, trace = binding
            variable.trace_remove("write", trace)
            self._selection_trace = None

    def _sync_selection_from_record(self, *_args):
        if self.record is not None:
            selected = self.record.var_sel.get()
            if self._selection_view_var.get() != selected:
                self._selection_view_var.set(selected)

    def _on_selection_clicked(self):
        if self.record is not None:
            self.record.var_sel.set(self._selection_view_var.get())
            self.app._schedule_stats_update()

    def bind_record(self, record):
        if self.record is record:
            return
        self.sync_to_record()
        if self.record is not None and self.record.view is self:
            self.record.view = None
        self._detach_selection_trace()
        self.record = record
        self.path = record.path if record else ""
        self.var_sel = record.var_sel if record else self._selection_view_var
        if record is not None:
            # CTk redraws even when configure(variable=...) binds an equal
            # Boolean value. Keep the editor's variable and mirror its owner,
            # updating the checkbox only when the visible value changes.
            trace = record.var_sel.trace_add("write", self._sync_selection_from_record)
            self._selection_trace = (record.var_sel, trace)
            self._sync_selection_from_record()
            record.view = self
        self.refresh_from_record(force_fields=True)

    def unbind_record(self):
        self.sync_to_record()
        self._detach_selection_trace()
        if self.record is not None and self.record.view is self:
            self.record.view = None
        self.record = None
        self.path = ""
        # Release the recycled row's image handle. The pooled widget outlives
        # its record, so keeping the CTkImage here would hold bytes for an
        # entry the thumbnail cache has already evicted.
        if getattr(self, "thumb_image", None) is not None:
            try:
                self.img_lbl._label.configure(image="")
                self.img_lbl.configure(image=None, text="No preview")
            except Exception:
                pass
            self.thumb_image = None
        if getattr(self, "_viewport_mapped", False):
            self.place_forget()
            self._viewport_mapped = False
            self._viewport_geometry = None

    def refresh_from_record(self, force_fields=False):
        if self.record is None:
            return
        data = self.record._cached_data
        language = metadata_language(data) if any(data.get(field) for field in ("title", "description")) else self.app.language_var.get()
        chinese = language in ("zh", "简体中文")
        headings = ("Title (8-30 Chinese characters)", "Description (30-120 Chinese characters)") if chinese else ("Title (8-12 words)", "Description (30-50 words)")
        if getattr(self, "_caption_headings", None) != headings:
            for widget, text in zip(self._field_headings, headings):
                widget.configure(text=text)
            self._caption_headings = headings
        fname = Path(self.record.path).name
        short_name = fname if len(fname) <= 22 else fname[:15] + "..." + fname[-4:]
        presentation = (short_name, self.record.status_text, self.record.status_color,
                        self.record.manual_modified, self.record.saved, bool(self.record.history), self.app._workspace_busy())
        previous = getattr(self, "_last_presentation", (None,) * 7)
        if presentation[0] != previous[0]:
            self.file_lbl.configure(text=short_name)
        if presentation[1:3] != previous[1:3]:
            self.status.configure(text=self.record.status_text, foreground=self.record.status_color)
        if presentation[3] != previous[3]:
            self.manual_lbl.configure(text="● Manual edits pending" if self.record.manual_modified else "")
        if presentation[4] != previous[4]:
            self.configure(highlightbackground="#10B981" if self.record.saved else "#E5E7EB")
        if presentation[4:] != previous[4:]:
            editable = not self.record.saved and not presentation[6]
            for button, state in (
                (self.regen_btn, "normal" if editable else "disabled"),
                (self.undo_btn, "normal" if self.record.history and editable else "disabled"),
                (self.remove_btn, "disabled" if presentation[6] else "normal"),
            ):
                if button.cget("state") != state:
                    button.configure(state=state)
        self._last_presentation = presentation
        self._set_editor_state(not self.record.saved and not presentation[6])

        if self.thumb_image is not self.record.thumb_image:
            next_image = self.record.thumb_image
            if next_image is None:
                # CTkLabel 5.2.2 accepts image=None but leaves its native label
                # pointing at the old Tcl image. Clear that handle before the
                # last Python reference is released during row recycling.
                self.img_lbl._label.configure(image="")
            self.img_lbl.configure(image=next_image, text="" if next_image else "No preview")
            self.thumb_image = next_image

        if force_fields:
            data = self.record._cached_data
            title_state = str(self.title_e.cget("state"))
            self.title_e.configure(state="normal")
            self.title_e.delete(0, "end")
            self.title_e.insert(0, str(data.get("title", "")).strip())
            self.title_e.configure(state=title_state)
            self._set_text_widget(self.desc_e, str(data.get("description", "")).strip())
            kw = data.get("keywords", [])
            self._set_text_widget(self.kw_e, "; ".join(kw if isinstance(kw, list) else []))
        self._refresh_keyword_count()

    def sync_to_record(self):
        if self.record is None or self.record.saved or self.record.removed:
            return
        previous = self.record._cached_data
        title = self.title_e.get().strip()
        description = self._get_text_widget(self.desc_e)
        kw_raw = self._get_text_widget(self.kw_e)
        keywords = split_keywords(kw_raw)
        compliance = self.record._cached_data.get("compliance", {})
        current = {"title": title, "description": description, "keywords": keywords}
        if all(value == previous.get(field) for field, value in current.items()):
            return
        for field, value in current.items():
            if value != previous.get(field):
                self.record.manual_fields.add(field)
        self.record.manual_modified = bool(self.record.manual_fields)
        self.record._cached_data = {
            **previous,
            "title": title,
            "description": description,
            "keywords": keywords,
            "compliance": compliance,
        }
        if title != previous.get("title") or description != previous.get("description"):
            self.record._cached_data["metadata_language"] = metadata_language({"title": title, "description": description})
        if keywords != previous.get("keywords"):
            self.record._cached_data["keyword_language"] = metadata_language({"keywords": keywords})
            if previous.get("keyword_policy_version") != 3:
                self.record._cached_data.pop("keyword_policy_version", None)
        self.app.data_cache[self.record.path] = dict(self.record._cached_data)
        self.record.refresh_quality_state()
        self.app._schedule_stats_update()

    def _open_keyword_editor(self):
        if self.record is None or self.app._workspace_busy() or self.record.saved:
            return
        working = self._parse_keyword_text(self._get_text_widget(self.kw_e))
        editor_font = ("Segoe UI", -max(1, round(self._apply_widget_scaling(13))))
        action_font = ("Segoe UI", -max(1, round(self._apply_widget_scaling(13))))
        dialog = AppToplevel(self.app)
        dialog.title("Keyword editor")
        dialog.transient(self.app)
        dialog.geometry("640x520")
        dialog.minsize(520, 360)
        body = tk.Frame(dialog, padx=16, pady=14, background="#F8FAFC")
        body.pack(fill="both", expand=True)
        heading = tk.Frame(body, background="#F8FAFC")
        heading.pack(fill="x")
        tk.Label(heading, text="Keywords", font=("Segoe UI", -max(1, round(self._apply_widget_scaling(15))), "bold"), anchor="w", background="#F8FAFC").pack(side="left")
        def show_review():
            data = {**self.record._cached_data, "keywords": working}
            messagebox.showinfo("Visual keyword check", keyword_review_detail(data), parent=dialog)
        tk.Button(heading, font=action_font, text="View keyword check", command=show_review).pack(side="right")
        count_lbl = tk.Label(body, text="", font=action_font, anchor="w", background="#F8FAFC")
        count_lbl.pack(fill="x", pady=(2, 8))
        list_frame = tk.Frame(body, background="#F8FAFC")
        list_frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            activestyle="none",
            font=editor_font,
            background="#FFFFFF",
            foreground="#111827",
            selectbackground="#0F9DB3",
            selectforeground="#FFFFFF",
        )
        list_scroll = tk.Scrollbar(list_frame, orient="vertical", command=listbox.yview,
                                   width=max(1, round(self._apply_widget_scaling(14))))
        listbox.configure(yscrollcommand=list_scroll.set)
        listbox.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")

        def redraw():
            listbox.delete(0, "end")
            for keyword in working:
                listbox.insert("end", keyword)
            count_lbl.configure(text=f"{len(working)} tags · 20–35 suggested; relevance first")

        def remove_selected():
            for index in reversed(listbox.curselection()):
                del working[index]
            redraw()

        def deduplicate():
            seen = set()
            unique = []
            for keyword in working:
                normalized = keyword.lower()
                if normalized not in seen:
                    seen.add(normalized)
                    unique.append(keyword)
            working[:] = unique
            redraw()

        def sort_keywords():
            mode = self.app.mode_var.get() if hasattr(self.app, "mode_var") else PROMPT_MODES[0]
            working[:] = rank_keywords_for_stock(working, mode)
            redraw()

        def copy_keywords():
            try:
                self.app.clipboard_clear()
                self.app.clipboard_append("; ".join(working))
            except tk.TclError:
                pass

        add_row = tk.Frame(body, background="#F8FAFC")
        add_row.pack(fill="x", pady=(10, 0))
        add_entry = tk.Entry(add_row, font=editor_font)
        add_entry.pack(side="left", fill="x", expand=True)

        def add_keyword():
            keyword = add_entry.get().strip()
            if keyword:
                working.append(keyword)
                add_entry.delete(0, "end")
                redraw()

        tk.Button(add_row, font=action_font, text="Add", command=add_keyword).pack(side="left", padx=(8, 0))
        actions = tk.Frame(body, background="#F8FAFC")
        actions.pack(fill="x", pady=(12, 0))
        tk.Button(actions, font=action_font, text="Remove selected", command=remove_selected).pack(side="left", padx=(0, 6))
        tk.Button(actions, font=action_font, text="Deduplicate", command=deduplicate).pack(side="left", padx=6)
        tk.Button(actions, font=action_font, text="Use cases last", command=sort_keywords).pack(side="left", padx=6)
        tk.Button(actions, font=action_font, text="Copy all", command=copy_keywords).pack(side="left", padx=6)
        footer = tk.Frame(body, background="#F8FAFC")
        footer.pack(fill="x", pady=(12, 0))

        def apply_changes():
            self._set_text_widget(self.kw_e, "; ".join(working))
            self.sync_to_record()
            self._refresh_keyword_count()
            dialog.destroy()

        tk.Button(footer, font=action_font, text="Cancel", command=dialog.destroy).pack(side="right")
        tk.Button(footer, font=action_font, text="Apply", command=apply_changes).pack(side="right", padx=(0, 6))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        redraw()
        dialog.grab_set()
        self.app.wait_window(dialog)

    def _on_regen(self):
        if self.record is not None and not self.app._workspace_busy():
            self.sync_to_record()
            if self.record.manual_modified:
                self._confirm_regen_scope()
            else:
                self.record.regen()

    def _confirm_regen_scope(self):
        dialog = tk.Toplevel(self.app)
        dialog.title("Manual edits detected")
        dialog.transient(self.app)
        dialog.resizable(False, False)
        body = tk.Frame(dialog, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="This record has unsaved manual edits.", font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text="Choose what Regenerate should replace:", anchor="w").pack(fill="x", pady=(6, 12))
        actions = tk.Frame(body)
        actions.pack(fill="x")

        def choose(scope):
            dialog.destroy()
            self.record.regen(scope)

        tk.Button(actions, text="Only keywords", command=lambda: choose("keywords")).pack(fill="x", pady=2)
        tk.Button(actions, text="Regenerate all", command=lambda: choose("all")).pack(fill="x", pady=2)
        tk.Button(actions, text="Keep manual fields", command=lambda: choose("unmodified")).pack(fill="x", pady=2)
        tk.Button(actions, text="Cancel", command=dialog.destroy).pack(fill="x", pady=(8, 2))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.app._scale_native_dialog(dialog)
        dialog.grab_set()
        self.app.wait_window(dialog)

    def _on_undo_generation(self):
        if self.record is not None:
            self.record.undo_last_generation()

    def _on_remove(self):
        if self.app._workspace_busy():
            return
        if self.record is not None:
            self.sync_to_record()
            self.record.remove()
