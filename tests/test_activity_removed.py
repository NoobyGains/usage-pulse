"""The ``activity`` widget (``⚡ Active``) was removed in 3.6.2.

It lit up when more than three tool calls landed within ten seconds, but the
count was only recomputed when the next tool call fired. The bar never aged
it, so after any burst the flag stayed on for as long as the hook state was
fresh (five minutes), and in a working session it was on nearly all the time.
The heartbeat spinner already shows live tool activity, so the widget is gone
rather than fixed.

Pinned here: it no longer renders, an older config that switched it on loads
cleanly with the setting dropped, and ``--show activity`` is refused like any
unknown widget.

Run: ``python tests/test_activity_removed.py``
"""
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@contextmanager
def sandbox():
    """State and config under a temp dir."""
    with tempfile.TemporaryDirectory() as td:
        env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td, "XDG_CACHE_HOME": td,
               "XDG_CONFIG_HOME": td, "HOME": td, "USERPROFILE": td}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cs, "get_config_path", return_value=Path(td) / "config.json"), \
                mock.patch.object(cs, "get_state_dir", return_value=Path(td)):
            yield Path(td)


class ActivityWidgetRemovedTest(unittest.TestCase):
    def test_not_a_known_widget(self):
        self.assertNotIn("activity", cs.DEFAULT_SHOW)
        self.assertNotIn("activity", cs.WIDGET_PRIORITY)

    def test_a_burst_of_tool_calls_no_longer_shows_active(self):
        show = {k: False for k in cs.DEFAULT_SHOW}
        show["activity"] = True  # what an older config may still say
        usage = {"five_hour": {"utilization": 10, "resets_at": None},
                 "seven_day": {"utilization": 20, "resets_at": None}}
        with sandbox(), \
                mock.patch.object(cs, "_read_hook_state",
                                  return_value={"rapid_calls": 9, "tool_count": 9}), \
                mock.patch.object(cs, "_is_hook_state_fresh", return_value=True):
            line = ANSI.sub("", cs.build_status_line(usage, "", {"show": show}, {}))
        self.assertNotIn("Active", line)

    def test_an_older_config_loads_with_the_setting_dropped(self):
        with sandbox() as td:
            (td / "config.json").write_text(
                json.dumps({"show": {"activity": True, "heartbeat": True}}), encoding="utf-8")
            show = cs.load_config()["show"]
        self.assertNotIn("activity", show)
        self.assertTrue(show["heartbeat"], "the other widgets are left as they were")

    def test_show_activity_is_refused(self):
        out = []
        with sandbox(), mock.patch.object(cs, "utf8_print", out.append):
            cs.cmd_show("activity")
            show = cs.load_config()["show"]
        self.assertTrue(out and out[0].startswith("Unknown part: activity"), out)
        self.assertNotIn("activity", show)


if __name__ == "__main__":
    unittest.main()
