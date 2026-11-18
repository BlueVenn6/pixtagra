"""Protect scroll optimizations without dropping queued user work or quality checks."""
from collections import OrderedDict
from types import SimpleNamespace

from tests.runtime.test_stage2_isolation import app_module, _bare_app
from tests.runtime.test_export_delivery import metadata


def test_selection_refresh_reuses_validation_but_all_metadata_changes_invalidate(app_module, monkeypatch):
    app = _bare_app(app_module)
    data = metadata()
    data['keywords'] = app_module.rank_keywords_for_stock(data['keywords'])
    card = SimpleNamespace(
        processing_failed=False, status=SimpleNamespace(cget=lambda key: '● Ready'),
        _cached_data=data,
        get_data=lambda **kwargs: (data['title'], data['description'], data['keywords']),
        get_compliance=lambda: data['compliance'],
    )
    app.cards = {'photo.jpg': card}
    actual = app_module.classify_stock_metadata
    calls = []
    def classify(*args):
        calls.append(args)
        return actual(*args)
    monkeypatch.setattr(app_module, 'classify_stock_metadata', classify)
    assert app._saveable_counts() == (1, 0)
    for _ in range(20):
        assert app._saveable_counts() == (1, 0)
    assert len(calls) == 1
    data['keywords'].append('stock photo')  # In-place edit, not a new list.
    assert app._saveable_counts() == (0, 1)
    assert len(calls) == 2
    data['keywords'].pop()  # Undo must restore the previous classification.
    assert app._saveable_counts() == (1, 0)
    data['title'] = 'Too short'
    assert app._saveable_counts() == (0, 0)
    data['title'] = metadata()['title']
    data['description'] = 'Too short'
    assert app._saveable_counts() == (0, 0)
    data['description'] = metadata()['description']
    assert app._saveable_counts() == (1, 0)
    count = len(calls)
    data['compliance']['visible_logo'] = True
    assert app._saveable_counts() == (0, 1)
    data['compliance'].clear()
    data['keyword_review'] = {'status': 'unavailable'}
    assert app._saveable_counts() == (0, 1)
    assert len(calls) == count  # Dynamic review/compliance checks stay live.
    app.mode_var.set(app_module.PROMPT_MODES[1])
    app._saveable_counts()
    assert len(calls) == count + 1


def thumbnail_app(module, bulk):
    app = _bare_app(module)
    app.showing_ws = True
    app._thumb_cache = OrderedDict()
    app._thumb_bulk_requested = bulk
    app.cards = {str(i): SimpleNamespace(path=str(i), removed=False, thumb_requested=False, thumb_image=None)
                 for i in range(12)}
    app.card_order = list(app.cards)
    app._thumb_queue = [app.cards[str(i)] for i in range(5)]
    app._thumb_queue_paths = {c.path for c in app._thumb_queue}
    app._thumb_flush_job = 'existing flush'
    app._thumb_result_job = 'existing drain'
    app._visible_index_window = lambda: (8, 11)
    return app


def test_scrollbar_jump_discards_only_obsolete_speculative_thumbnail_work(app_module):
    app = thumbnail_app(app_module, False)
    app._queue_visible_thumb_load()
    assert [c.path for c in app._thumb_queue] == ['8', '9', '10']
    assert app._thumb_queue_paths == {'8', '9', '10'}
    app._queue_visible_thumb_load()
    assert len(app._thumb_queue) == 3


def test_explicit_thumbs_action_survives_scroll_and_prioritizes_current_images(app_module):
    app = thumbnail_app(app_module, True)
    app._queue_visible_thumb_load()
    assert [c.path for c in app._thumb_queue] == ['8', '9', '10', '0', '1', '2', '3', '4']
    app._queue_visible_thumb_load()
    assert len(app._thumb_queue) == 8


def test_thumbnail_paint_yields_to_input_without_losing_completed_images(app_module, monkeypatch):
    import queue
    app = thumbnail_app(app_module, False)
    app._thumb_queue.clear()
    app._thumb_result_queue = queue.Queue()
    app._thumb_generation = 1
    clock = [0.0]
    painted, scheduled = [], []
    monkeypatch.setattr(app_module.time, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(app_module.ctk, 'CTkImage', lambda **kw: object())
    app.after = lambda delay, callback: scheduled.append((delay, callback)) or 'pending'
    app._trim_thumb_cache = lambda: None
    for record in app.cards.values():
        def paint(force_fields=False, record=record):
            painted.append(record.path)
            clock[0] += .020  # A slow native paint must yield before the next image.
        record.refresh_view = paint
        record.thumb_requested = True
        app._thumb_result_queue.put((record, object(), 1))
    app._drain_thumb_results()
    assert painted == ['0']
    assert app._thumb_result_queue.qsize() == 11
    # A bounded yield leaves time for native paints after the callback returns.
    assert 0 < scheduled[-1][0] <= 50
    while not app._thumb_result_queue.empty():
        app._drain_thumb_results()
    assert painted == list(app.cards)
    assert list(app._thumb_cache) == list(app.cards)
    assert all(not record.thumb_requested for record in app.cards.values())


def test_deferred_native_paints_do_not_chain_multiple_visible_images(app_module, monkeypatch):
    import queue
    app = thumbnail_app(app_module, False)
    app._thumb_queue.clear()
    app._thumb_result_queue = queue.Queue()
    app._thumb_generation = 1
    monkeypatch.setattr(app_module.time, 'perf_counter', lambda: 0)
    monkeypatch.setattr(app_module.ctk, 'CTkImage', lambda **kw: object())
    scheduled, painted = [], []
    app.after = lambda delay, fn: scheduled.append((delay, fn)) or 'pending'
    app._trim_thumb_cache = lambda: None
    for record in app.cards.values():
        record.view = object()
        record.refresh_view = lambda force_fields=False, record=record: painted.append(record.path)
        app._thumb_result_queue.put((record, object(), 1))
    app._drain_thumb_results()
    assert painted == ['0']
    assert app._thumb_result_queue.qsize() == 11
    assert 0 < scheduled[-1][0] <= 50


def test_large_jump_defers_images_but_resumes_without_dropping_results(app_module, monkeypatch):
    import queue
    app = thumbnail_app(app_module, False)
    app._thumb_queue.clear()
    app._thumb_result_queue = queue.Queue()
    app._thumb_generation = 1
    app._thumbnail_jump_until = .150
    clock = [0.0]
    monkeypatch.setattr(app_module.time, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(app_module.ctk, 'CTkImage', lambda **kw: object())
    scheduled, painted = [], []
    app.after = lambda delay, fn: scheduled.append((delay, fn)) or 'pending'
    app._trim_thumb_cache = lambda: None
    record = app.cards['0']
    record.refresh_view = lambda **kw: painted.append(record.path)
    app._thumb_result_queue.put((record, object(), 1))
    app._drain_thumb_results()
    assert not painted and app._thumb_result_queue.qsize() == 1
    assert scheduled[-1][0] == 150
    clock[0] = .151
    app._drain_thumb_results()
    assert painted == ['0'] and app._thumb_result_queue.empty()


def test_large_jump_wait_is_measured_after_editor_paint_turn(app_module, monkeypatch):
    import queue
    app = thumbnail_app(app_module, False)
    app._thumb_queue.clear()
    app._thumb_result_queue = queue.Queue()
    app._thumb_generation = 1
    app._thumbnail_viewport_offset = 0
    app._thumbnail_jump_job = None
    app._card_height_ready = True
    app.canvas = SimpleNamespace(scroll_offset=1000, winfo_height=lambda: 400)
    app._canvas_scrollbar = SimpleNamespace(set=lambda *args: None)
    clock = [0.0]
    monkeypatch.setattr(app_module.time, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(app_module.ctk, 'CTkImage', lambda **kw: object())
    idle, scheduled, painted = [], [], []
    app.after_idle = lambda callback: idle.append(callback) or 'paint-completion'
    app.after = lambda delay, fn: scheduled.append((delay, fn)) or 'image-drain'
    def rebind(**kwargs):
        clock[0] = .080  # Native editor work can outlast the old 75ms wait.
    app._schedule_visible_update = rebind
    app._trim_thumb_cache = lambda: None
    record = app.cards['0']
    record.refresh_view = lambda **kw: painted.append(record.path)
    app._thumb_result_queue.put((record, object(), 1))
    app._on_canvas_yview(0.2, 0.4)
    for callback in idle:
        callback()
    # Other native paint work can still run after an idle callback. A timer
    # completion must separate the entire paint turn from image application.
    clock[0] = .200
    app._drain_thumb_results()
    assert not painted and app._thumb_result_queue.qsize() == 1
    for _, callback in scheduled[:]:
        if callback == app._after_viewport_jump:
            callback()
    clock[0] = .351
    app._drain_thumb_results()
    assert painted == ['0'] and app._thumb_result_queue.empty()
