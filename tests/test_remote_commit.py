"""The remote-commit lookup must survive GitHub's API rate limit.

``--update`` and the hourly update indicator ask the GitHub API for the SHA
of upstream main. The API allows 60 unauthenticated requests an hour per
address; on a machine where several tools poll GitHub that runs out, the API
answers 403, and the updater used to stop with "Could not reach GitHub API"
although the network was fine. A failed API call now falls back to
``git ls-remote``, which GitHub serves without that limit.

The API call goes through ``_fetch_bounded`` (a hard wall-clock ceiling, see
"Render-path networking" in claude_status.py), so these tests fake its network
seam, ``_open_and_read``. Faking ``urllib.request.urlopen`` would miss it and
let the tests reach the real GitHub API.

Run: ``python tests/test_remote_commit.py``
"""
import io
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
LS_REMOTE = f"{SHA}\trefs/heads/main"



def _rate_limited(*a, **k):
    raise urllib.error.HTTPError("https://api.github.com/x", 403, "rate limit exceeded",
                                 {}, io.BytesIO(b""))


class RemoteCommitTest(unittest.TestCase):
    def test_api_answer_wins_and_git_never_runs(self):
        with mock.patch.object(cs, "_open_and_read", return_value=SHA.encode()), \
                mock.patch.object(cs, "_git") as git:
            self.assertEqual(cs.get_remote_commit(), SHA)
        git.assert_not_called()

    def test_rate_limited_api_falls_back_to_git_ls_remote(self):
        with mock.patch.object(cs, "_open_and_read", side_effect=_rate_limited), \
                mock.patch.object(cs, "_git", return_value=LS_REMOTE) as git:
            self.assertEqual(cs.get_remote_commit(), SHA)
        args, kwargs = git.call_args
        self.assertEqual(args, ("ls-remote", "origin", "refs/heads/main"))
        self.assertEqual(kwargs["env"].get("GIT_TERMINAL_PROMPT"), "0",
                         "the fallback must never wait on a credential prompt")

    def test_html_error_page_from_the_api_falls_back_too(self):
        with mock.patch.object(cs, "_open_and_read",
                               return_value=b"<html>rate limited</html>"), \
                mock.patch.object(cs, "_git", return_value=LS_REMOTE):
            self.assertEqual(cs.get_remote_commit(), SHA)

    def test_both_sources_failing_gives_none(self):
        with mock.patch.object(cs, "_open_and_read", side_effect=_rate_limited), \
                mock.patch.object(cs, "_git", return_value=None):
            self.assertIsNone(cs.get_remote_commit())

    def test_garbage_from_git_gives_none(self):
        for out in ("", "fatal: not a git repository", "deadbeef\trefs/heads/main"):
            with mock.patch.object(cs, "_open_and_read", side_effect=_rate_limited), \
                    mock.patch.object(cs, "_git", return_value=out):
                self.assertIsNone(cs.get_remote_commit(), out)

    def test_spent_deadline_runs_nothing(self):
        with mock.patch.object(cs, "_open_and_read",
                               side_effect=AssertionError("network used")) as net, \
                mock.patch.object(cs, "_git", side_effect=AssertionError("git used")) as git:
            self.assertIsNone(cs.get_remote_commit(deadline=time.time() - 1))
        net.assert_not_called()
        git.assert_not_called()

    def test_fallback_keeps_the_callers_deadline(self):
        """The status line's update check has a hard budget: the git fallback
        must be capped by it, not by its own fixed timeout."""
        seen = {}

        def fake_git(*args, timeout=2, deadline=None, env=None, **_kwargs):
            seen["deadline"] = deadline
            return LS_REMOTE

        deadline = time.time() + 2.0
        with mock.patch.object(cs, "_open_and_read", side_effect=_rate_limited), \
                mock.patch.object(cs, "_git", side_effect=fake_git):
            self.assertEqual(cs.get_remote_commit(deadline=deadline), SHA)
        self.assertEqual(seen["deadline"], deadline)

    def test_git_helper_passes_the_environment_through(self):
        calls = {}

        def fake_run(cmd, capture_output=None, text=None, timeout=None, cwd=None, env=None):
            calls["env"] = env
            return SimpleNamespace(returncode=0, stdout=LS_REMOTE, stderr="")

        with mock.patch.object(cs.subprocess, "run", side_effect=fake_run):
            self.assertEqual(cs._git("ls-remote", "origin", "refs/heads/main",
                                     env={"GIT_TERMINAL_PROMPT": "0"}), LS_REMOTE)
            self.assertEqual(calls["env"], {"GIT_TERMINAL_PROMPT": "0"})
            self.assertEqual(cs._git("rev-parse", "HEAD"), LS_REMOTE)
            self.assertIsNone(calls["env"], "no env given: the child inherits ours")


class UpdateCommandMessageTest(unittest.TestCase):
    def test_update_says_both_sources_failed(self):
        lines = []
        origin = SimpleNamespace(returncode=0, stdout=f"https://github.com/{cs.GITHUB_REPO}.git\n",
                                 stderr="")
        with mock.patch.object(cs, "utf8_print", lines.append), \
                mock.patch.object(cs.Path, "exists", return_value=True), \
                mock.patch.object(cs.subprocess, "run", return_value=origin), \
                mock.patch.object(cs, "get_local_commit", return_value=SHA), \
                mock.patch.object(cs, "get_remote_commit", return_value=None):
            cs.cmd_update()
        text = "\n".join(lines)
        self.assertIn("API and git both failed", text)
        self.assertNotIn("Update found", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
