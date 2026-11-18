"""Run real Tk checks in fresh interpreters, retaining their full assertions.

Repeated isolated production imports and destroyed Tk roots in the same pytest
process exhibit Tcl initialization failures on Windows. A subprocess also keeps
CustomTkinter's process-global root/scaling state out of unrelated tests.
"""
import os
from pathlib import Path
import subprocess
import sys


def in_fresh_gui_process(request):
    node = request.node.nodeid
    if os.environ.get('PIXTAGRA_GUI_TEST_NODE') == node:
        return True
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q', node, '-p', 'no:cacheprovider'],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'PIXTAGRA_GUI_TEST_NODE': node},
        capture_output=True, timeout=90,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).decode('utf-8', errors='replace')
    return False
