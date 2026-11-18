from pixtagra_layout import flow_positions


def test_controls_wrap_without_shrinking_and_keep_original_order():
    widths = [124, 96, 96, 112, 176, 96, 210]
    positions = flow_positions(widths, 850)
    assert positions == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (1, 0)]
    for row in {row for row, _ in positions}:
        row_widths = [width for width, position in zip(widths, positions) if position[0] == row]
        assert sum(row_widths) + 6 * (len(row_widths) - 1) <= 850


def test_narrow_layout_can_recover_to_one_row_after_resize():
    widths = [160, 130, 110, 150, 85, 120, 130]
    assert max(row for row, _ in flow_positions(widths, 700)) == 1
    assert max(row for row, _ in flow_positions(widths, 1200)) == 0
