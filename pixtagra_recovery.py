"""Workspace recovery integration. All UI state is read on the Tk thread."""
import copy
from tkinter import messagebox
from pixtagra_localization import messagebox

from pixtagra_session import SessionStore, image_identity
from pixtagra_models import migrate_model_selection


class WorkspaceRecoveryMixin:
    def _initialize_recovery(self, path, record_type):
        self._session_store = None
        self._session_job = None
        self._session_ack = set()
        self._session_error_shown = False
        self._close_requested = False
        try:
            self._session_store = SessionStore(path)
            snapshot, pending = self._session_store.load()
            context = snapshot.get("context", {})
            for name in ("mode", "shoot_location", "shoot_date"):
                if name in context:
                    getattr(self, name + "_var").set(context[name])
            if context.get("model"):
                self.model_var.set(migrate_model_selection(context["model"]))
                self._on_model_change()
            changed = []
            for saved in snapshot.get("records", []):
                card = record_type(saved["path"], self)
                for name in ("saved", "processing_failed", "last_generated_at", "last_generated_model", "regen_scope"):
                    if name in saved:
                        setattr(card, name, saved[name])
                card.var_sel.set(saved.get("selected", True))
                card._cached_data = saved.get("data", {})
                card.manual_fields = set(saved.get("manual_fields", []))
                card.manual_modified = bool(card.manual_fields)
                card.history = saved.get("history", [])
                card.source_identity = saved.get("identity")
                self.cards[card.path] = card
                self.card_order.append(card.path)
                checkpoint = pending.get(card.path)
                if image_identity(card.path) != card.source_identity or card.source_identity is None:
                    changed.append(card)
                elif checkpoint and checkpoint.get("identity") == card.source_identity:
                    data = checkpoint["data"]
                    if checkpoint.get("stage") != "done":
                        data["keyword_review"] = {"status": "pending", "stage": checkpoint["stage"]}
                    card.fill(data)
                    card.last_generated_model = checkpoint.get("model", "")
                    self._session_ack.add(card.path)
                if card._cached_data.get("title"):
                    self.data_cache[card.path] = copy.deepcopy(card._cached_data)
                    card.refresh_quality_state()
                if card.saved:
                    needs_review = bool(card.quality_issues or card.compliance_warnings)
                    card.status_text = "● Saved — needs review" if needs_review else "● Saved"
                    card.status_color = "#F97316" if needs_review else "#10B981"
                elif card.processing_failed:
                    card.status_text, card.status_color = "● Failed", "#EF4444"
            for card in changed:
                card.processing_failed = True
                card.has_error = True
                card.status_text, card.status_color = "● Source changed / missing", "#EF4444"
                card.var_sel.set(False)
                card._cached_data["keyword_review"] = {"status": "source_changed"}
                if card.path in self.data_cache:
                    self.data_cache[card.path] = copy.deepcopy(card._cached_data)
            if changed:
                self.after(100, lambda: messagebox.showwarning(
                    "Recovery needs attention",
                    f"{len(changed)} image(s) moved, changed or are missing. Their text is retained, "
                    "but they are deselected. Reimport the correct files before generating or saving."
                ))
            self._update_resume_button()
        except Exception as exc:
            if self._session_store is not None:
                self._session_store.close()
                self._session_store = None
            self._session_error(exc)

    def _session_error(self, exc, *, interactive=False):
        self._session_save_error = str(exc)
        if interactive or not self.__dict__.get("_session_error_shown", False):
            self._session_error_shown = True
            messagebox.showwarning("Workspace recovery unavailable",
                "Progress could not be saved or restored. Generation cannot start safely.\n\n"
                "Close other PixTagra windows, then close and reopen this window. "
                "You may need to import these images again. If the problem continues, "
                "check that the workspace folder is writable.\n\n" + str(exc))

    def _schedule_session_save(self):
        if not self.__dict__.get("_session_store") or self.__dict__.get("_saving_session"):
            return
        # One timer, not an endlessly postponed debounce during continuous editing.
        if self.__dict__.get("_session_job") is None:
            self._session_job = self.after(750, self._persist_session)

    def _persist_session(self, *, interactive=False):
        store = self.__dict__.get("_session_store")
        if store is None:
            error = self.__dict__.get("_session_save_error")
            if error and interactive:
                self._session_error(error, interactive=True)
            return not error
        job = self.__dict__.get("_session_job")
        if job is not None:
            self.after_cancel(job)
        self._session_job = None
        self._saving_session = True
        try:
            records = []
            for path in self.card_order:
                card = self.cards[path]
                card.sync_from_view()
                records.append({
                    "path": path, "identity": card.source_identity,
                    "selected": card.var_sel.get(), "saved": card.saved,
                    "processing_failed": card.processing_failed, "manual_fields": list(card.manual_fields),
                    "last_generated_at": card.last_generated_at, "last_generated_model": card.last_generated_model,
                    "regen_scope": card.regen_scope, "data": card._cached_data, "history": card.history,
                })
            context = {name: getattr(self, name + "_var").get() for name in (
                "mode", "model", "shoot_location", "shoot_date"
            )}
            store.save(records, context)
            store.acknowledge(self._session_ack)
            self._session_ack.clear()
            self._session_save_error = ""
            self._session_error_shown = False
            return True
        except Exception as exc:
            self._session_error(exc, interactive=interactive)
            return False
        finally:
            self._saving_session = False

    def _checkpoint_result(self, card, data, stage, model):
        store = self.__dict__.get("_session_store")
        if store is not None:
            try:
                store.checkpoint(card.path, data, stage, model, card.source_identity)
            except Exception as exc:
                self.stop_flag = True
                self._stop_event.set()
                self._analysis_result_queue.put(("session_error", None, str(exc), 0, None))

    def _request_close(self):
        self._close_requested = True
        if self.is_processing:
            self._stop()
        if self.is_processing or self.is_saving:
            self.prog_lbl.configure(text="Closing after active requests / writes finish; progress is being retained...")
            self.after(100, self._request_close)
            return
        if self._persist_session():
            self.destroy()
        else:
            self._close_requested = False
            if messagebox.askyesno("Progress is not saved", "Recovery storage is unavailable. Close without saving current progress?"):
                self.destroy()
