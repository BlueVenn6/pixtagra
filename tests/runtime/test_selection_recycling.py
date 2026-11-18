"""Real Tk selection behavior while native card editors are recycled."""
from PIL import Image
from tests.runtime.test_stage2_isolation import app_module


def test_recycling_equal_selection_avoids_repaint_and_preserves_record_ownership(app_module, monkeypatch, tmp_path, request):
    from tests.runtime.gui_process import in_fresh_gui_process
    if not in_fresh_gui_process(request):
        return
    monkeypatch.setenv('PIXTAGRA_PACKAGED_TEST_ISOLATION', '1')
    source = tmp_path / 'first.jpg'
    second = tmp_path / 'second.jpg'
    Image.new('RGB', (64, 64), 'red').save(source)
    Image.new('RGB', (64, 64), 'blue').save(second)
    monkeypatch.setattr(app_module.filedialog, 'askopenfilenames', lambda **kw: [str(source)])
    app = app_module.App()
    try:
        app.load_images()
        app.update()
        first = app.cards[str(source)]
        view = first.view
        next_record = app_module.CardRecord(str(second), app)
        drawn = []
        original = view.sel_box._draw
        def draw(*args, **kwargs):
            drawn.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(view.sel_box, '_draw', draw)
        view.bind_record(next_record)
        assert drawn == []  # Both records selected: no visual value changed.
        first.var_sel.set(False)
        assert view.sel_box.get() == 1
        next_record.var_sel.set(False)
        assert view.sel_box.get() == 0
        view.sel_box.toggle()  # Same widget callback as a real user click.
        assert next_record.var_sel.get() and not first.var_sel.get()
        view.unbind_record()
        assert not next_record.var_sel.trace_info()
        next_record.var_sel.set(False)  # No callback into a detached editor.
    finally:
        app.destroy()


def test_recycled_editors_cover_both_partial_viewport_edges(app_module, monkeypatch, tmp_path, request):
    """A smaller pool must still render every row intersecting the viewport."""
    from tests.runtime.gui_process import in_fresh_gui_process
    if not in_fresh_gui_process(request):
        return
    monkeypatch.setenv('PIXTAGRA_PACKAGED_TEST_ISOLATION', '1')
    paths = []
    for index in range(10):
        path = tmp_path / f'{index}.jpg'
        Image.new('RGB', (32, 32), (index * 20, 0, 0)).save(path)
        paths.append(str(path))
    monkeypatch.setattr(app_module.filedialog, 'askopenfilenames', lambda **kw: paths)
    app = app_module.App()
    try:
        app.load_images()
        import time
        deadline = time.monotonic() + 10
        while app.is_importing and time.monotonic() < deadline:
            app.update()
        assert len(app.cards) == 10
        for size in ('1100x650', '1100x850'):
            app.geometry(size)
            app.update()
            pitch = app._card_height + app._card_gap
            total = len(paths) * pitch + 20
            for row in (0, 2, 5, 9):
                for within_row in (.05, .5, .95):
                    app.canvas.yview_moveto((row + within_row) * pitch / total)
                    app.update()
                    offset = app.canvas.scroll_offset
                    viewport = app.canvas.winfo_height()
                    visible = {view.record.path: view for view in app.card_views if view.record}
                    for index, path in enumerate(paths):
                        top = index * pitch + 10 - offset
                        if top < viewport and top + app._card_height > 0:
                            assert path in visible, (size, row, within_row, index)
                            assert visible[path].winfo_ismapped(), (size, row, within_row, index,
                                app.state(), app.workspace.winfo_ismapped(), top, viewport,
                                visible[path].winfo_y(), visible[path].winfo_height())
                            assert abs(visible[path].winfo_y() - top) <= 1
    finally:
        app.destroy()
