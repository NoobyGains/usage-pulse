"""Test helper: pretend the suite is run from a terminal window of a given width.

claude_status fits the status line to the terminal it finds (stdout, stderr,
stdin, /dev/tty, COLUMNS, then a /proc walk) and caches the answer for the
life of the process. A test that drives ``main()`` with a fake stdout (no
``fileno``) falls through to stderr, so on a contributor's machine the line is
cut to the width of their own window, and the widgets the test asserts on go
missing. CI and Claude Code have no terminal, which is why that never showed.

``host_terminal_width`` reproduces that contributor environment on any machine,
so a harness that forgets to pin the width fails the same way everywhere.
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_status as cs  # noqa: E402


@contextmanager
def host_terminal_width(columns):
    """Inside the block, width detection reports a terminal *columns* wide.

    The per-process width cache starts empty (as in a fresh run) and the
    detection cascade is replaced by the answer a real window would give;
    both are restored on exit. A harness that pins ``cs._detect_terminal_width``
    itself, as the main()-driven tests now do, is unaffected by this."""
    with mock.patch.object(cs, "_cached_terminal_width", None), \
            mock.patch.object(cs, "_detect_terminal_width_uncached", return_value=columns):
        yield
