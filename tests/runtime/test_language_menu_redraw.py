"""Real menu state changes must not rebuild unchanged translated choices."""
from tests.runtime.test_stage2_isolation import app_module


def test_unchanged_state_preserves_dropdown_items_and_language_switch_refreshes(app_module, monkeypatch, request):
    from tests.runtime.gui_process import in_fresh_gui_process
    if not in_fresh_gui_process(request):
        return
    monkeypatch.setenv('PIXTAGRA_PACKAGED_TEST_ISOLATION', '1')
    app = app_module.App()
    try:
        app.update()
        menu = app._workspace_mode_menu
        calls = []
        original = menu._dropdown_menu.configure
        def configure(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)
        monkeypatch.setattr(menu._dropdown_menu, 'configure', configure)
        for state in ('normal', 'disabled', 'disabled', 'normal') * 5:
            menu.configure(state=state)
        assert not calls, 'Unchanged choices were rebuilt on every state update'
        app._on_language_change('简体中文')
        assert len(calls) == 1
        assert calls[0]['values'][0] == '📷 通用摄影'
        assert menu.get() == app_module.PROMPT_MODES[0]
        menu._dropdown_callback('🎨 商业背景')
        assert menu.get() == app_module.PROMPT_MODES[1]
        assert menu._text_label.cget('text') == '🎨 商业背景'
        assert len(calls) == 1
        menu.configure(values=list(app_module.PROMPT_MODES))
        assert '通用摄影' in menu._dropdown_menu.entrycget(0, 'label')
        assert menu.get() == app_module.PROMPT_MODES[1]
        calls.clear()
        app._on_language_change('English')
        assert len(calls) == 1
        assert menu.get() == app_module.PROMPT_MODES[1]
        assert menu._text_label.cget('text') == app_module.PROMPT_MODES[1]
    finally:
        app.destroy()


def test_saved_state_changes_only_redraw_actions_whose_availability_changes(app_module, monkeypatch, tmp_path, request):
    from tests.runtime.gui_process import in_fresh_gui_process
    if not in_fresh_gui_process(request):
        return
    from PIL import Image
    monkeypatch.setenv('PIXTAGRA_PACKAGED_TEST_ISOLATION', '1')
    app = app_module.App()
    try:
        source = tmp_path / 'actions.png'
        Image.new('RGB', (64, 64), 'white').save(source)
        monkeypatch.setattr(app_module.filedialog, 'askopenfilenames', lambda **kw: [str(source)])
        app.load_images()
        app.update()
        record = app.cards[str(source)]
        view = record.view
        draws = []
        for control in (view.regen_btn, view.undo_btn, view.remove_btn, view.kw_expand_btn):
            original = control._draw
            def draw(*args, _original=original, _control=control, **kwargs):
                draws.append(_control)
                return _original(*args, **kwargs)
            monkeypatch.setattr(control, '_draw', draw)
        record.saved = True
        record.refresh_view()
        assert view.regen_btn.cget('state') == view.kw_expand_btn.cget('state') == 'disabled'
        assert view.remove_btn.cget('state') == 'normal'
        assert view.undo_btn not in draws and view.remove_btn not in draws
        draws.clear()
        record.saved = False
        record.refresh_view()
        assert view.regen_btn.cget('state') == view.kw_expand_btn.cget('state') == 'normal'
        assert view.undo_btn not in draws and view.remove_btn not in draws
        app._network_check_busy = True
        record.refresh_view()
        assert all(button.cget('state') == 'disabled' for button in (view.regen_btn, view.undo_btn, view.remove_btn, view.kw_expand_btn))
    finally:
        app.destroy()


def test_workspace_state_refresh_only_redraws_changed_controls(app_module, monkeypatch, request):
    from tests.runtime.gui_process import in_fresh_gui_process
    if not in_fresh_gui_process(request):
        return
    monkeypatch.setenv('PIXTAGRA_PACKAGED_TEST_ISOLATION', '1')
    app = app_module.App()
    try:
        app.update()
        app._update_workspace_controls()
        controls = (app._workspace_model_menu, app._workspace_mode_menu,
                    app._visual_review_checkbox, *app._language_menus)
        draws = []
        for control in controls:
            original = control._draw
            def draw(*args, _original=original, _control=control, **kwargs):
                draws.append(_control)
                return _original(*args, **kwargs)
            monkeypatch.setattr(control, '_draw', draw)
        for _ in range(5):
            app._update_workspace_controls()
        assert not draws, 'An unchanged workspace state still forces native redraws'
        app._network_check_busy = True
        app._update_workspace_controls()
        assert all(control.cget('state') == 'disabled' for control in controls)
        assert draws
        draws.clear()
        app._update_workspace_controls()
        assert not draws
        app._network_check_busy = False
        app._update_workspace_controls()
        assert all(control.cget('state') == 'normal' for control in controls)
    finally:
        app.destroy()
