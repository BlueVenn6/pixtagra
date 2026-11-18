"""Delivery boundaries: editing/saving is not a successful quality review."""
from types import SimpleNamespace

import pytest

from tests.runtime.test_stage2_isolation import app_module, _Var
from tests.runtime.test_keyword_quality_policy import generated, WORDS
from pixtagra_exports import record_errors
from pixtagra_quality import keyword_review_warnings


def record(module, monkeypatch, count=19):
    monkeypatch.setattr(module.tk, "BooleanVar", lambda **kw: _Var(True))
    app = SimpleNamespace(mode_var=_Var(module.PROMPT_MODES[0]), model_var=_Var("Qwen 3.7 Plus"),
                          data_cache={}, _update_stats=lambda: None, _schedule_stats_update=lambda: None)
    card = module.CardRecord("fixture.jpg", app)
    card.fill({**generated(count), "keyword_policy_version": 3})
    return app, card


def assert_current_minimum(card):
    assert card._cached_data["keyword_policy_version"] == 3
    assert any("at least 20" in issue for issue in card.quality_issues)
    assert any("20–35" in issue for issue in record_errors("shutterstock", card.path, card._cached_data))


def test_batch_addition_cannot_downgrade_generated_minimum(app_module, monkeypatch):
    app, card = record(app_module, monkeypatch, 18)
    app_module.App._apply_batch_keywords(app, [card], [WORDS[18]])
    assert len(card._cached_data["keywords"]) == 19
    assert_current_minimum(card)


def test_editor_change_cannot_downgrade_generated_minimum(app_module, monkeypatch):
    app, card = record(app_module, monkeypatch, 20)
    data = card._cached_data
    view = SimpleNamespace(record=card, app=app,
        title_e=SimpleNamespace(get=lambda: data["title"]), desc_e="description", kw_e="keywords")
    view._get_text_widget = lambda widget: data["description"] if widget == "description" else "; ".join(WORDS[:19])
    app_module.ImageCard.sync_to_record(view)
    assert_current_minimum(card)


def test_preserved_manual_keywords_keep_their_original_current_policy(app_module, monkeypatch):
    app, card = record(app_module, monkeypatch)
    card.manual_fields = {"keywords"}
    card.regen_scope = "unmodified"
    card.fill({**generated(30), "keyword_policy_version": 3})
    assert len(card._cached_data["keywords"]) == 19
    assert_current_minimum(card)


def test_missing_review_on_current_generation_is_not_verified():
    assert keyword_review_warnings({"keyword_policy_version": 3, "keywords": WORDS[:25]})
    assert keyword_review_warnings({"keyword_policy_version": 2, "keywords": WORDS[:12]}) == []
    assert keyword_review_warnings({"keywords": WORDS[:7]}) == []


def test_saving_review_record_does_not_turn_it_into_ready(app_module, monkeypatch):
    app, card = record(app_module, monkeypatch)
    card.mark_saved()
    assert card.saved and not card.var_sel.get()
    assert card.has_error
    assert app_module.App._card_needs_review(card)
    assert not app_module.App._card_ready(card)


def test_saved_valid_manual_record_remains_ready(app_module, monkeypatch):
    app, card = record(app_module, monkeypatch)
    data = generated(25)
    data.pop("keyword_policy_version")
    card.fill(data)
    card.mark_saved()
    assert not card.has_error
    assert app_module.App._card_ready(card)
    assert not app_module.App._card_needs_review(card)


def test_saved_review_state_survives_workspace_reopen(app_module, monkeypatch, tmp_path):
    from PIL import Image
    from pixtagra_session import SessionStore, image_identity
    path = tmp_path / "original.jpg"
    Image.new("RGB", (20, 20), "green").save(path)
    database = tmp_path / "workspace.sqlite3"
    store = SessionStore(database)
    store.save([{"path": str(path), "identity": image_identity(path), "saved": True,
                 "selected": False, "data": {**generated(19), "keyword_policy_version": 3}}], {})
    store.close()
    monkeypatch.setattr(app_module.tk, "BooleanVar", lambda **kw: _Var(False))
    app = SimpleNamespace(mode_var=_Var(app_module.PROMPT_MODES[0]), cards={}, card_order=[], data_cache={},
        _update_resume_button=lambda: None, _session_error=lambda exc: pytest.fail(str(exc)))
    app_module.App._initialize_recovery(app, database, app_module.CardRecord)
    try:
        restored = app.cards[str(path)]
        assert restored.saved
        assert_current_minimum(restored)
        assert app_module.App._card_needs_review(restored)
        assert not app_module.App._card_ready(restored)
    finally:
        app._session_store.close()


@pytest.mark.parametrize("stage", ["unavailable", "cancelled", "model_unqualified"])
def test_incomplete_review_is_a_review_result_not_transport_failure(app_module, monkeypatch, stage):
    app, card = record(app_module, monkeypatch, 25)
    card.fill({**generated(25), "keyword_policy_version": 3, "keyword_review": {"status": stage}})
    assert app_module.App._card_needs_review(card)
    assert not app_module.App._card_processing_failed(card)
    assert not app_module.App._card_ready(card)
