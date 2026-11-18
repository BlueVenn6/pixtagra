"""A connection result must not hide a local generation prerequisite failure."""
from types import SimpleNamespace

from tests.runtime.test_stage2_isolation import app_module, _bare_app


def test_every_explicit_generate_attempt_reports_unavailable_workspace(app_module, monkeypatch):
    app = _bare_app(app_module)
    app.is_processing = app._network_check_busy = app.is_importing = app.is_saving = False
    app._session_store = None
    app._session_save_error = "workspace locked"
    app._session_error_shown = True  # Startup warning was already dismissed.
    progress, warnings, starts = [], [], []
    app.prog_lbl = SimpleNamespace(configure=lambda **kw: progress.append(kw["text"]))
    app._start_credential_operation = lambda *a, **kw: starts.append(a)
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *a: warnings.append(a))
    for _ in range(2):
        app.analyze([])
    assert len(warnings) == 2
    assert all("reopen this window" in message for title, message in warnings)
    assert len(progress) == 2 and all("Generation not started" in text for text in progress)
    assert starts == []
    # Background autosave must not turn this into an endless dialog loop.
    assert not app._persist_session()
    assert len(warnings) == 2


def test_connection_success_discloses_workspace_block(app_module, monkeypatch):
    app = _bare_app(app_module)
    app._session_save_error = "workspace locked"
    messages = []
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *a: messages.append(a[1]))
    result = {"ok": True, "status_code": 200, "ctx": {
        "route": "custom", "transport": "openai_responses", "model": "gpt-5.6-sol",
        "resolved_url": "http://127.0.0.1/responses"}}
    app._show_connection_result(result)
    assert "Connection OK" in messages[-1]
    assert "Generation is blocked" in messages[-1]
    app._session_save_error = ""
    app._show_connection_result(result)
    assert "Generation is blocked" not in messages[-1]
