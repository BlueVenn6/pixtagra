from pixtagra_viewport import ViewportCanvas


def viewport():
    canvas = object.__new__(ViewportCanvas)
    canvas._offset = 0
    canvas._extent = 10000
    canvas._notify_job = None
    canvas.winfo_height = lambda: 500
    canvas.cget = lambda key: 16
    canvas.after_idle = lambda callback: "pending"
    return canvas


def test_large_jump_keeps_logical_range_and_clamps_to_last_full_page():
    canvas = viewport()
    canvas.yview("moveto", "1")
    assert canvas.yview() == (0.95, 1.0)
    assert canvas.canvasy(100) == 9600
    canvas.yview("scroll", "-1", "pages")
    assert canvas.scroll_offset == 9050
    canvas.yview_moveto(-1)
    assert canvas.yview() == (0, 0.05)


def test_clear_or_resize_clamps_existing_scroll_offset():
    canvas = viewport()
    canvas.yview_moveto(0.9)
    canvas.configure(scrollregion=(0, 0, 1200, 800))
    assert canvas.scroll_offset == 300
    canvas.configure(scrollregion=(0, 0, 1200, 0))
    assert canvas.yview() == (0, 1)


def test_wheel_bursts_coalesce_without_losing_distance():
    canvas = viewport()
    calls = []
    canvas.after_idle = lambda callback: calls.append(callback) or "pending"
    for _ in range(20):
        canvas.yview_scroll(3, "units")
    assert canvas.scroll_offset == 960
    assert len(calls) == 1
