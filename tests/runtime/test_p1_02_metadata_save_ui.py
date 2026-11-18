"""Focused contracts for metadata guidance and the date-picker entry point."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from tests.runtime.test_stage2_isolation import app_module


def test_date_picker_formats_a_metadata_date_without_manual_input(app_module):
    assert app_module._format_date_picker_value(2026, 8, 3) == "08/03/2026"
    with pytest.raises(ValueError):
        app_module._format_date_picker_value(2026, 2, 30)


def test_welcome_metadata_controls_have_labels_hints_and_calendar_entry(app_module):
    setup_source = inspect.getsource(app_module.App._setup_welcome)

    assert "Creator / copyright holder" in setup_source
    assert "Shoot location (optional)" in setup_source
    assert "Date created (optional)" in setup_source
    assert "Pick a date" in setup_source
    assert "self._open_shoot_date_picker" in setup_source
    assert hasattr(app_module.App, "_open_shoot_date_picker")


def test_date_picker_accepts_arbitrary_years_without_a_bounded_year_dropdown(app_module):
    source = inspect.getsource(app_module.App._open_shoot_date_picker)

    assert "year_input = ctk.CTkEntry" in source
    assert "textvariable=year_var" in source
    assert 'year_input.bind("<Return>"' in source
    assert 'year_input.bind("<FocusOut>"' in source
    assert "1 <= year <= 9999" in source
    assert "year_values" not in source
    choose_day_source = source.split("def choose_day", 1)[1].split("header =", 1)[0]
    assert "set_year()" in choose_day_source
    assert "_reverse_window_scaling(popup_width)" in source
    assert "_reverse_window_scaling(popup_height)" in source


def test_batch_keyword_helpers_parse_and_merge_without_replacing_existing_terms(app_module):
    assert app_module._parse_batch_keyword_input("Chinese pattern; blue, floral\nblue") == [
        "Chinese pattern",
        "blue",
        "floral",
        "blue",
    ]
    assert app_module._merge_keyword_lists(
        ["Blue", "existing-term"],
        ["blue", "Chinese pattern", "CHINESE PATTERN"],
    ) == ["Blue", "existing-term", "Chinese pattern"]


def test_workspace_exposes_selected_image_batch_keyword_action(app_module):
    source = inspect.getsource(app_module.App._setup_workspace)
    app_source = inspect.getsource(app_module.App._open_batch_keyword_dialog)
    apply_source = inspect.getsource(app_module.App._apply_batch_keywords)
    assert '("batch_keywords", "Add keywords", self._open_batch_keyword_dialog)' in source
    assert 'text=f"Add keywords ({selected})"' in inspect.getsource(app_module.App._update_stats)
    assert "Add keywords to selected images" in app_source
    assert "English keywords" in app_source
    assert "Add to selected" in app_source
    assert "_parse_batch_keyword_input" in app_source
    assert "manual_fields.add(\"keywords\")" in apply_source
    assert "refresh_quality_state" in apply_source


def test_batch_keyword_action_updates_each_target_and_export_cache(app_module):
    class BatchCard:
        def __init__(self, path, keywords):
            self.path = path
            self.removed = False
            self.saved = False
            self.manual_fields = set()
            self.manual_modified = False
            self._cached_data = {
                "title": "Existing title",
                "description": "Existing description",
                "keywords": list(keywords),
            }
            self.quality_refreshes = 0
            self.view_refreshes = 0

        def sync_from_view(self):
            return None

        def get_data(self, sync=False):
            return (
                self._cached_data["title"],
                self._cached_data["description"],
                self._cached_data["keywords"],
            )

        def refresh_quality_state(self):
            self.quality_refreshes += 1

        def refresh_view(self, force_fields=False):
            self.view_refreshes += int(force_fields)

    first = BatchCard("first.jpg", ["Blue", "floral motif"])
    second = BatchCard("second.jpg", ["symmetrical design"])
    app = object.__new__(app_module.App)
    app.data_cache = {first.path: dict(first._cached_data), second.path: dict(second._cached_data)}
    app._update_stats = lambda: None

    changed, skipped, added_total = app._apply_batch_keywords(
        [first, second], ["blue", "Chinese pattern", "traditional ornament"]
    )

    assert (changed, skipped, added_total) == (2, 0, 5)
    assert first._cached_data["keywords"] == [
        "Blue",
        "floral motif",
        "Chinese pattern",
        "traditional ornament",
    ]
    assert second._cached_data["keywords"] == [
        "symmetrical design",
        "blue",
        "Chinese pattern",
        "traditional ornament",
    ]
    assert first.manual_fields == {"keywords"}
    assert second.manual_modified is True
    assert app.data_cache[first.path]["keywords"] == first._cached_data["keywords"]
    assert first.quality_refreshes == 1
    assert first.view_refreshes == 1


def _status(text):
    return SimpleNamespace(cget=lambda key: text if key == "text" else "")


def _card(module, path, status, *, selected=False, failed=False):
    return SimpleNamespace(
        path=path,
        status=_status(status),
        var_sel=SimpleNamespace(get=lambda: selected),
        processing_failed=failed,
    )


def test_retry_candidates_are_scoped_and_labeled_for_review_failed_or_selected(app_module):
    app = object.__new__(app_module.App)
    review = _card(app_module, "review.jpg", "● Need Review")
    failed = _card(app_module, "failed.jpg", "● Error", failed=True)
    app.cards = {review.path: review, failed.path: failed}

    targets, label = app._retry_candidates()
    assert targets == [failed]
    assert label == "Retry failed (1)"

    failed.processing_failed = False
    failed.status = _status("● Ready")
    targets, label = app._retry_candidates()
    assert targets == [review]
    assert label == "Retry review (1)"

    review.var_sel = SimpleNamespace(get=lambda: True)
    failed.status = _status("● Need Review")
    targets, label = app._retry_candidates()
    assert targets == [review]
    assert label == "Retry selected (1)"


def test_quality_recovery_dialog_exposes_direct_actions(app_module):
    source = inspect.getsource(app_module.App._show_quality_recovery_dialog)
    assert "Retry generation (" in source
    assert "Open record" in source
    assert "Edit manually" in source
    assert "Save ready (" in source
    assert "Cancel" in source
    assert "normalized_failures" in source
    assert "Save with review" in source
    assert "issue_scroll" in source


def test_keyword_editor_exposes_count_scroll_and_batch_edit_actions(app_module):
    source = inspect.getsource(app_module.ImageCard)
    assert "self.kw_count_lbl" in source
    # The user replaced the fixed 35-tag target with a suggested range.
    # Live English/Chinese heading behavior is covered by test_language_workflow.
    assert "20–35 suggested" in source
    assert "0/{KEYWORD_TARGET_COUNT}" not in source
    assert "self.kw_scroll" in source
    assert "Expand editor" in source
    assert "Deduplicate" in source
    assert "Remove selected" in source
    assert "Use cases last" in source
    assert "Copy all" in source
    assert "classify_stock_metadata" in inspect.getsource(app_module.App._start_save)


def test_keyword_editor_uses_classic_scrollbar_without_ttk_state_option(app_module):
    source = inspect.getsource(app_module.ImageCard._set_editor_state)
    assert "self.kw_scroll.configure(state" not in source


def test_save_and_retry_controls_expose_counts_and_cost_confirmation(app_module):
    workspace_source = inspect.getsource(app_module.App._setup_workspace)
    retry_source = inspect.getsource(app_module.App._confirm_batch_retry)
    save_source = inspect.getsource(app_module.App._update_save_export_buttons)
    assert "Save selected" in workspace_source
    assert "Save ready" in workspace_source
    assert "Save all" in workspace_source
    assert "JSON ready" in workspace_source
    assert "Save with review" in inspect.getsource(app_module.App._show_quality_recovery_dialog)
    assert "Estimated requests" in retry_source
    assert "additional API usage or charges" in retry_source
    assert "Export (blocked:" in save_source


def test_explicit_save_actions_keep_user_in_control_of_quality_warnings(app_module):
    save_source = inspect.getsource(app_module.App._save)
    save_all_source = inspect.getsource(app_module.App._save_all)
    folder_source = inspect.getsource(app_module.App._save_to_folder)
    start_source = inspect.getsource(app_module.App._start_save)
    worker_source = inspect.getsource(app_module.App._save_worker)

    assert "force=True" in save_source
    assert "force=True" in save_all_source
    assert "force=True" in folder_source
    assert "def _start_save(self, targets, output_dir=None, force=False)" in start_source
    assert "if quality_failures and not force" in start_source
    assert "if not t:" not in worker_source
    assert "output_title = t or Path(str(image_path)).stem or \"seo_image\"" in worker_source
    assert "_save_result_queue" in worker_source


def test_workspace_scroll_uses_bounded_steps_and_deferred_virtualized_updates(app_module):
    scroll_source = inspect.getsource(app_module.App._on_mousewheel)
    card_source = inspect.getsource(app_module.ImageCard)
    assert "scroll_units = min(8, max(3, notches * 3))" in scroll_source
    assert "self._schedule_visible_update()" in scroll_source
    assert "immediate=True" not in scroll_source
    assert "yscrollincrement=" in inspect.getsource(app_module.App._setup_workspace)
    # Native editor visibility is verified by the real GUI workspace probe.
    # Requiring canvas window items here forced offscreen rows to unmap,
    # causing the row-boundary redraw regression on Windows.
    schedule_source = inspect.getsource(app_module.App._schedule_visible_update)
    assert "elif self._visible_update_job is None" in schedule_source
    assert "after(16, self._run_visible_update)" in schedule_source
    assert 'font=("Segoe UI", 12)' in card_source
    assert 'font=("Segoe UI", 13)' in card_source
    assert "height=260" in card_source


def test_welcome_content_uses_flow_layout_without_relative_overlap(app_module):
    source = inspect.getsource(app_module.App._setup_welcome)
    assert "content = ctk.CTkFrame(center" in source
    assert "content.pack(expand=True, fill=\"x\"" in source
    assert "bf.pack(fill=\"x\")" in source
    assert "inf.pack(fill=\"x\", pady=(18, 0))" in source
    assert "bf.place(relx=" not in source
    assert "inf.place(relx=" not in source


def test_regenerate_protects_manual_edits_with_explicit_scope_choices(app_module):
    card_source = inspect.getsource(app_module.CardRecord)
    image_source = inspect.getsource(app_module.ImageCard)
    assert "manual_modified" in card_source
    assert "manual_fields" in card_source
    assert "Only keywords" in image_source
    assert "Regenerate all" in image_source
    assert "Keep manual fields" in image_source
    assert "Manual edits pending" in image_source


def test_batch_status_filters_and_field_level_quality_details_are_exposed(app_module):
    workspace_source = inspect.getsource(app_module.App._setup_workspace)
    status_source = inspect.getsource(app_module.App._batch_counts)
    detail_source = inspect.getsource(app_module.App._quality_detail)
    # These actions select matching records; the explicit labels distinguish
    # selection from generation and review. Preserve every callback as well.
    for label, callback in (
        ("Select ready", "self._sel_ready"), ("Select review", "self._sel_review"),
        ("Select failed", "self._sel_failed"), ("Select unprocessed", "self._sel_unprocessed"),
    ):
        assert f'"{label}"' in workspace_source
        assert callback in workspace_source
    assert "selected" in status_source
    assert "Keywords:" in detail_source
    assert "Description:" in detail_source
    assert "Title:" in detail_source
    assert "Compliance:" in detail_source


def test_batch_summary_closes_the_save_loop_and_history_keeps_generation_context(app_module):
    stats_source = inspect.getsource(app_module.App._update_stats)
    card_source = inspect.getsource(app_module.CardRecord)
    assert "Automated checks passed. Ready to save; review AI content before submission." in stats_source
    assert "ready to save" in stats_source
    assert "last_generated_at" in card_source
    assert "last_generated_model" in card_source
    assert "undo_last_generation" in card_source
