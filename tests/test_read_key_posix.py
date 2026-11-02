"""``_read_key`` on a real POSIX pseudo terminal (issue #56).

A terminal delivers an arrow key as one write of ``ESC [ B``. ``_read_key``
used to read the first byte through the buffered ``sys.stdin``, which pulled
the whole sequence into Python's buffer; the ``select`` that should have seen
the rest of it then found the descriptor empty and the arrow came back as
``'esc'``, cancelling the setup wizard. Each case here runs ``_read_key`` in a
child on a fresh pty and writes the bytes the way a terminal does.

Run: ``python tests/test_read_key_posix.py``
"""
import ast
import os
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CASES = [
    (b"\x1b[A", "up"),
    (b"\x1b[B", "down"),
    (b"\x1b[C", "right"),
    (b"\x1b[D", "left"),
    (b"\x1bOB", "down"),          # application-cursor mode
    (b"\x1b", "esc"),
    (b"\r", "enter"),
    (b"\x03", "ctrl-c"),
    (b"\x7f", "backspace"),
    (b"q", "q"),
    ("é".encode("utf-8"), "é"),   # multi-byte UTF-8 in a text field
    ("€".encode("utf-8"), "€"),
]


def _read_key_on_pty(data):
    import pty
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            sys.path.insert(0, str(REPO_ROOT))
            import claude_status
            os.write(1, b"\nREADY\n")
            key = claude_status._read_key()
            os.write(1, ("\nRESULT:" + repr(key) + "\n").encode("utf-8"))
        finally:
            os._exit(0)
    out = b""
    try:
        while b"READY" not in out:
            out += os.read(fd, 4096)
        time.sleep(0.2)  # let _read_key switch the terminal to raw mode
        os.write(fd, data)
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            out += chunk
    except OSError:  # EIO once the child exits
        pass
    finally:
        os.waitpid(pid, 0)
        os.close(fd)
    for line in out.decode("utf-8", errors="replace").splitlines():
        if line.startswith("RESULT:"):
            return ast.literal_eval(line[len("RESULT:"):])
    raise AssertionError(f"no result from _read_key; output: {out!r}")


@unittest.skipIf(sys.platform == "win32", "POSIX pty only")
class ReadKeyPosixTest(unittest.TestCase):
    def test_keys_map_to_tokens(self):
        for data, expected in CASES:
            with self.subTest(data=data):
                self.assertEqual(_read_key_on_pty(data), expected)


if __name__ == "__main__":
    unittest.main()
