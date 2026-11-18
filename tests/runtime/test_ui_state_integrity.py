"""Regression checks for stale batch presentation and controls that reject clicks."""
from types import SimpleNamespace
import threading
import queue

from tests.runtime.test_stage2_isolation import app_module, _bare_app, _Var


class Control:
    def __init__(self, **values):
        self.values = values

    def configure(self, **values):
        self.values.update(values)

    def cget(self, key):
        return self.values.get(key)

    def set(self, value):
        self.values["value"] = value


def batch_app(module):
    app = _bare_app(module)
    app.stats = dict(total=2, processed=2, success=2, need_review=0, failed=0)
    app.prog = Control(value=1)
    app.prog_lbl = Control(text="Done: 2 ok")
    app.btns = {key: Control() for key in ("⚡ Analyze", "🔄 Retry", "⏹ Cancel")}
    app._stop_event = threading.Event()
    app.mode_var = _Var(module.PROMPT_MODES[0])
    app.visual_review_var = _Var(False)
    app.error_lock = threading.Lock()
    app.api_errors = []
    app._stats_update_job = None
    app.stop_flag = False
    app._persist_session = lambda **kw: True
    app._release_active_credential_lease = lambda: None
    app._update_stats = lambda: None
    app._update_retry_button = lambda: None
    return app


def test_second_batch_connection_check_has_no_previous_progress(app_module):
    app = batch_app(app_module)
    app._run_network_check = lambda *args: None  # Hold preflight open.
    app._start_authorized_analysis([object()], "Local Ollama", "", "", None)
    assert app.prog.cget("value") == 0
    assert app.stats["processed"] == app.stats["success"] == 0
    assert app.prog_lbl.cget("text") == "Checking model connection..."


def test_result_callbacks_cannot_overwrite_finished_batch(app_module):
    app = batch_app(app_module)
    app.stats.update(processed=0, success=0)
    pending = []
    app.after = lambda delay, callback: pending.append(callback)
    app._schedule_stats_update = lambda: None
    app._mark_processing_failed = lambda card: None
    app._record_analysis_result(SimpleNamespace(removed=False), None, total=1)
    app._finish()
    finished = (app.prog.cget("value"), app.prog_lbl.cget("text"))
    for callback in pending:
        callback()
    assert (app.prog.cget("value"), app.prog_lbl.cget("text")) == finished
    assert app.prog_lbl.cget("text").startswith("Done:")


def test_failed_preflight_does_not_report_previous_success(app_module, monkeypatch):
    app = batch_app(app_module)
    app._run_network_check = lambda *args: None
    app._format_connection_feedback = lambda result, text: text
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *a: None)
    app._start_authorized_analysis([object()], "Local Ollama", "", "", None)
    app._on_analysis_preflight((False, "offline", {}), [], "Local Ollama", "", "", "", None)
    assert app.prog.cget("value") == 0
    assert "not started" in app.prog_lbl.cget("text").lower()
    assert "2 ok" not in app.prog_lbl.cget("text")


def test_outputs_are_disabled_during_generation_and_when_csv_has_no_rows(app_module):
    app = batch_app(app_module)
    card = SimpleNamespace(var_sel=_Var(True), status=Control(text="● Unprocessed"),
                           get_data=lambda **kw: ("", "", []))
    app.cards = {"fixture.jpg": card}
    app.data_cache = {}
    app._saveable_counts = lambda: (0, 0)
    app.is_saving = app.is_importing = app._network_check_busy = False
    app.is_processing = True
    keys = ("💾 Save", "💾 Save ready", "💾 Save all", "📁 Save→Folder", "📄 JSON", "📄 JSON ready", "📊 SS CSV")
    app.btns.update({key: Control(state="normal") for key in keys})
    app._update_save_export_buttons()
    assert all(app.btns[key].cget("state") == "disabled" for key in keys)
    app.is_processing = False
    app._update_save_export_buttons()
    assert app.btns["📊 SS CSV"].cget("state") == "disabled"


def test_undo_refreshes_summary_and_refuses_to_mutate_while_saving(app_module, monkeypatch):
    monkeypatch.setattr(app_module.tk, "BooleanVar", lambda **kw: _Var(True))
    refreshes = []
    app = SimpleNamespace(mode_var=_Var(app_module.PROMPT_MODES[0]), data_cache={},
                          _workspace_busy=lambda: app.is_saving, is_saving=True,
                          _schedule_stats_update=lambda: refreshes.append(True))
    card = app_module.CardRecord("fixture.jpg", app)
    card.history = [{"data": {"title": "Previous title"}}]
    assert not card.undo_last_generation()
    assert len(card.history) == 1
    app.is_saving = False
    assert card.undo_last_generation()
    assert refreshes


def test_save_poll_survives_slow_writer_without_worker_tk_calls(app_module):
    app = batch_app(app_module)
    app._save_result_queue = queue.Queue()
    app._save_result_job = "first-poll"
    app.is_saving = True
    scheduled = []
    app.after = lambda delay, callback: scheduled.append(callback) or "next-poll"
    completions = []
    def done(*result):
        completions.append(result)
        app.is_saving = False
    app._save_done = done
    app._drain_save_results()  # Writer is still running at the first poll.
    assert scheduled, "An empty queue must not end polling while a save is active"
    result = (1, [], None, [], [], [])
    app._save_result_queue.put(result)
    scheduled.pop(0)()
    assert completions == [result]
    assert not scheduled
