"""Per-image state, manual edits and undo history, independent of application setup."""
from datetime import datetime
import tkinter as tk
from PIL import Image, ImageOps
from pixtagra_session import image_identity
from pixtagra_language import metadata_language, metadata_options, split_keywords
from pixtagra_keywords import keyword_limits
from pixtagra_quality import keyword_review_warnings
from pixtagra_keywords import (PROMPT_MODES, KEYWORD_TARGET_COUNT,
    sanitize_keywords_for_stock, rank_keywords_for_stock, normalize_compliance_flags,
    validate_stock_metadata, metadata_compliance_warnings)


class _CardStatusProxy:
    def __init__(self, record):
        self.record = record

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.record.status_text = kwargs["text"]
        if "text_color" in kwargs:
            self.record.status_color = kwargs["text_color"]
        self.record.refresh_view(force_fields=False)

    def cget(self, key):
        if key == "text":
            return self.record.status_text
        if key == "text_color":
            return self.record.status_color
        raise KeyError(key)


class CardRecord:
    def __init__(self, path, app):
        self.app = app
        self.path = path
        self.source_identity = image_identity(path)
        self.var_sel = tk.BooleanVar(master=app, value=True)
        self.has_error = False
        self.processing_failed = False
        self.manual_modified = False
        self.manual_fields = set()
        self.regen_scope = "all"
        self.history = []
        self.last_generated_at = ""
        self.last_generated_model = ""
        self.saved = False
        self.removed = False
        self.thumb_requested = False
        self.thumb_image = None
        self.view = None
        self.status_text = "● Unprocessed"
        self.status_color = "#94A3B8"
        self.status = _CardStatusProxy(self)
        self._cached_data = {"title": "", "description": "", "keywords": []}
        self.quality_issues = []
        self.compliance_warnings = []

    def winfo_exists(self):
        return not self.removed

    def destroy(self):
        self.removed = True
        if self.view is not None:
            self.view.unbind_record()

    def refresh_view(self, force_fields=False):
        if self.view is not None and self.view.winfo_exists():
            self.view.refresh_from_record(force_fields=force_fields)

    def sync_from_view(self):
        if self.view is not None and self.view.record is self and self.view.winfo_exists():
            self.view.sync_to_record()

    def _make_square_thumb(self, src_img, size=384):
        img = ImageOps.exif_transpose(src_img).convert("RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        result = Image.new("RGB", (size, size), "#F3F4F6")
        result.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
        img.close()
        return result

    def load_thumb(self):
        if self.thumb_requested or self.removed:
            return
        self.thumb_requested = True
        thumb_generation = self.app._thumb_generation

        def _load():
            img = None
            try:
                with Image.open(self.path) as src:
                    src.draft("RGB", (384, 384))
                    img = self._make_square_thumb(src)
            except Exception:
                pass
            # Tk objects must only be created and touched on the main thread.
            # The app drains this queue from its regular event loop.
            self.app._thumb_result_queue.put((self, img, thumb_generation))

        self.app._thumb_pool.submit(_load)

    def fill(self, data):
        previous_data = dict(self._cached_data)
        # A first-pass preview is not a completed generation. The preview path
        # records the true pre-batch state once and sets _partial_draft, so the
        # completed fill below must not record the draft as a second undo step.
        # Any non-empty previous content stays undoable, including text without
        # a title, so the original condition continues to apply.
        draft = getattr(self, "_partial_applied", False) or getattr(self, "_partial_draft", False)
        if not getattr(self, "_partial_applied", False):
            # Consumed once: later regenerations push history normally.
            self._partial_draft = False
        if not draft and (previous_data.get("title") or previous_data.get("description")
                          or previous_data.get("keywords")):
            self.history.append({"data": previous_data, "time": self.last_generated_at,
                                 "model": self.last_generated_model, "manual_fields": set(self.manual_fields)})
            self.history = self.history[-5:]
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        keywords = data.get("keywords", [])
        if isinstance(keywords, str):
            keywords = split_keywords(keywords)
        keywords, _rejected, _shortfall = sanitize_keywords_for_stock(
            keywords,
            target_count=keyword_limits(data)[1],
            language=data.get("keyword_language") or metadata_language(data),
            keyword_policy_version=data.get("keyword_policy_version"),
        )
        mode = self.app.mode_var.get() if hasattr(self.app, "mode_var") else PROMPT_MODES[0]
        keywords = rank_keywords_for_stock(keywords, mode)
        compliance = normalize_compliance_flags(data.get("compliance", {}))
        generated_data = {
            "title": title,
            "description": description,
            "keywords": keywords,
            "compliance": compliance,
        }
        for field in ("metadata_language", "keyword_language", "keyword_policy_version"):
            if field in data:
                generated_data[field] = data[field]
        if "keyword_review" in data:
            generated_data["keyword_review"] = data["keyword_review"]

        self.processing_failed = False
        preserve_fields = set()
        if self.regen_scope == "keywords":
            preserve_fields.update({"title", "description"})
        elif self.regen_scope in {"unmodified", "resume"}:
            preserve_fields.update(self.manual_fields)
        if preserve_fields:
            for field in preserve_fields:
                if field in previous_data:
                    generated_data[field] = previous_data[field]
        if preserve_fields & {"title", "description"}:
            generated_data["metadata_language"] = metadata_language(previous_data)
        generated_data["keyword_language"] = (previous_data.get("keyword_language") or metadata_language(previous_data)) if "keywords" in preserve_fields else (data.get("keyword_language") or metadata_language(data))
        if "keywords" in preserve_fields:
            if previous_data.get("keyword_policy_version") == 3:
                generated_data["keyword_policy_version"] = 3
            else:
                generated_data.pop("keyword_policy_version", None)
        if "keywords" in preserve_fields and self.regen_scope != "resume":
            generated_data.pop("keyword_review", None)
            if "keyword_review" in previous_data:
                generated_data["keyword_review"] = previous_data["keyword_review"]
        self._cached_data = generated_data
        # Export the merged record, including protected manual fields, rather
        # than the raw model response cached by the analysis callback.
        self.app.data_cache[self.path] = dict(generated_data)
        self.last_generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_generated_model = self.app.model_var.get() if hasattr(self.app, "model_var") else ""
        title = str(generated_data.get("title", "")).strip()
        description = str(generated_data.get("description", "")).strip()
        keywords = list(generated_data.get("keywords", []))
        quality_issues = validate_stock_metadata(title, description, keywords, mode, **metadata_options(generated_data)) + keyword_review_warnings(generated_data)
        self.quality_issues = quality_issues
        self.compliance_warnings = metadata_compliance_warnings(
            title, description, keywords, generated_data.get("compliance", {})
        )
        content_issues = [
            issue for issue in quality_issues
            if issue.startswith("Title") or issue.startswith("Description")
        ]
        self.has_error = bool(quality_issues)
        self.manual_fields.intersection_update(preserve_fields)
        self.manual_modified = bool(self.manual_fields)
        self.regen_scope = "all"

        if content_issues:
            self.status_text = "● Needs review"
            self.status_color = "#F59E0B"
        elif quality_issues or self.compliance_warnings:
            self.status_text = "● Needs review"
            self.status_color = "#F97316"
        else:
            self.status_text = "● Ready"
            self.status_color = "#10B981"
        self.refresh_view(force_fields=True)

    def mark_saved(self):
        self.saved = True
        needs_review = bool(self.quality_issues or self.compliance_warnings)
        self.status_text = "● Saved — needs review" if needs_review else "● Saved"
        self.status_color = "#F97316" if needs_review else "#10B981"
        self.var_sel.set(False)
        self.has_error = bool(self.quality_issues)
        self.manual_modified = False
        self.manual_fields.clear()
        self.refresh_view(force_fields=False)

    def get_data(self, sync=True):
        if sync:
            self.sync_from_view()
        keywords = self._cached_data.get("keywords", [])
        return (
            str(self._cached_data.get("title", "")).strip(),
            str(self._cached_data.get("description", "")).strip(),
            keywords if isinstance(keywords, list) else [],
        )

    def get_compliance(self):
        return normalize_compliance_flags(self._cached_data.get("compliance", {}))

    def undo_last_generation(self):
        if not self.history or self.saved or getattr(self.app, "_workspace_busy", lambda: False)():
            return False
        previous = self.history.pop()
        self._cached_data = dict(previous.get("data", {}))
        self.app.data_cache[self.path] = dict(self._cached_data)
        self.manual_fields = set(previous.get("manual_fields", ()))
        self.manual_modified = bool(self.manual_fields)
        self.last_generated_at = previous.get("time", "")
        self.last_generated_model = previous.get("model", "")
        self.refresh_quality_state()
        self.refresh_view(force_fields=True)
        refresh = getattr(self.app, "_schedule_stats_update", None)
        if refresh is not None:
            refresh()
        return True

    def refresh_quality_state(self):
        title, description, keywords = self.get_data(sync=False)
        mode = self.app.mode_var.get() if hasattr(self.app, "mode_var") else PROMPT_MODES[0]
        self.quality_issues = validate_stock_metadata(title, description, keywords, mode, **metadata_options(self._cached_data)) + keyword_review_warnings(self._cached_data)
        self.compliance_warnings = metadata_compliance_warnings(
            title, description, keywords, self.get_compliance()
        )
        self.has_error = bool(self.quality_issues)
        content_issues = [
            issue for issue in self.quality_issues
            if issue.startswith("Title") or issue.startswith("Description")
        ]
        if content_issues:
            self.status_text, self.status_color = "● Needs review", "#F59E0B"
        elif self.quality_issues or self.compliance_warnings:
            self.status_text, self.status_color = "● Needs review", "#F97316"
        else:
            self.status_text, self.status_color = "● Ready", "#10B981"
        self.refresh_view(force_fields=False)

    def regen(self, scope="all"):
        if self.app._workspace_busy() or self.removed or self.saved:
            return
        self.saved = False
        self.has_error = False
        self.processing_failed = False
        self.regen_scope = scope
        self.status_text = "● Unprocessed"
        self.status_color = "#94A3B8"
        self.var_sel.set(True)
        self.refresh_view(force_fields=False)
        self.app.analyze([self])

    def remove(self):
        self.app._remove_card(self)

