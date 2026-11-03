"""Guard the README's factual claims against drift from the source.

The README is the project's shop window, and a claim that quietly drifted away
from the code is worse than no claim at all. Everything machine-checkable is
asserted here: theme/style/animation counts, that every documented --show/--hide
flag is a real widget, that every ``--flag`` the README names exists, that the
"Full flag list" block is the real ``--help`` output, that the changelog's top
version is the script's version, that the example status line has no removed
segments, that no removed feature is still advertised, and that every local
asset exists.

Run: ``python tests/test_readme_claims.py``

The README is the project's shop window; a claim that drifted from the code is
worse than no claim. This checks the ones that are machine-verifiable.
"""
import re
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import claude_status as cs  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

readme = (REPO / "README.md").read_text(encoding="utf-8")
changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
pulse_md = (REPO / "commands" / "pulse.md").read_text(encoding="utf-8")
issues = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        issues.append(f"{name}: {detail}")


print("== version / floor ==")
# Derived from the changelog's own heading rather than hardcoded here — a
# hardcoded version pin in this file is the same drift bug this suite
# exists to catch (the manifests are covered by test_version_lockstep.py).
# "## [Unreleased]" is skipped: it is not a version.
_top = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
check(
    "CHANGELOG top version heading matches VERSION",
    bool(_top) and _top.group(1) == cs.VERSION,
    f"CHANGELOG {_top.group(1) if _top else '(heading missing)'}"
    f" vs VERSION {cs.VERSION}",
)
check("README links CHANGELOG.md", "(CHANGELOG.md)" in readme)
# "3.6+" may legitimately appear in a historical note about 3.1.0; what must
# not appear is 3.6 stated as the *current* requirement.
stale_floor = re.search(r"(?:Python|python)[- ]3\.6\+(?!.*advertised)", readme)     and "advertised 3.6+" not in readme
check("README states Python 3.8+ as the requirement", "3.8+" in readme and not stale_floor)

print("\n== themes ==")
themes = set(cs.THEMES) if hasattr(cs, "THEMES") else set(cs.THEME_COLOURS)
claimed = re.search(r"\*\*(\d+) themes\*\*", readme)
check("theme count claim matches code",
      claimed and int(claimed.group(1)) == len(themes),
      f"README says {claimed.group(1) if claimed else '?'}, code has {len(themes)}: {sorted(themes)}")
for t in sorted(themes):
    check(f"theme '{t}' documented", t in readme)

print("\n== bar styles ==")
styles = set(cs.BAR_STYLES)
claimed_bs = re.search(r"\*\*(\d+) bar styles\*\*", readme)
check("bar style count matches",
      claimed_bs and int(claimed_bs.group(1)) == len(styles),
      f"README says {claimed_bs.group(1) if claimed_bs else '?'}, code has {len(styles)}: {sorted(styles)}")
for s in sorted(styles):
    check(f"bar style '{s}' documented", s in readme)

print("\n== animation modes ==")
modes = set(cs.ANIMATE_MODES)
if modes:
    claimed_am = re.search(r"\*\*(\d+) animation modes\*\*", readme)
    check("animation mode count matches",
          claimed_am and int(claimed_am.group(1)) == len(modes),
          f"README says {claimed_am.group(1) if claimed_am else '?'}, code has {len(modes)}: {sorted(modes)}")
    for m in sorted(modes):
        check(f"animation '{m}' documented", m in readme)

print("\n== show/hide flags ==")
flags = set(re.findall(r"--(?:show|hide) (\w+)", readme))
for f in sorted(flags):
    check(f"--show/--hide {f} is a real widget", f in cs.DEFAULT_SHOW)

print("\n== widgets table ==")
# Rows look like: | `id` | on | ... |   or   | `a`, `b`, `c` | off | ... |
_defaults = {}
for _ids, _state in re.findall(r"^\| ((?:`[a-z_]+`(?:, )?)+) \| (on|off) \|", readme, re.M):
    for w in re.findall(r"`([a-z_]+)`", _ids):
        _defaults[w] = (_state == "on")
for w in sorted(_defaults):
    check(f"table widget '{w}' is a real widget", w in cs.DEFAULT_SHOW)
for w in sorted(set(cs.DEFAULT_SHOW) - set(_defaults)):
    check(f"widget '{w}' is in the table", False, "add a row to the Widgets table")
for w, state in sorted(_defaults.items()):
    if w in cs.DEFAULT_SHOW:
        check(f"widget '{w}' default ({'on' if state else 'off'}) matches DEFAULT_SHOW",
              cs.DEFAULT_SHOW[w] == state)

print("\n== flags ==")
_known_flags = {r[0] for r in cs.CLI_FLAGS} | set(cs._CLI_SUBOPTIONS)
_known_flags.update({"--max-budget-usd", "--marketplace"})   # Claude Code's own flags
_readme_flags = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]+)", readme))
for f in sorted(_readme_flags - _known_flags):
    check(f"README flag {f} exists", False, "not in CLI_FLAGS or the sub-options")
check("every --flag the README names exists", not (_readme_flags - _known_flags),
      f"{len(_readme_flags)} distinct flags checked")

# The "Full flag list" block must be exactly what --help prints.
_block = re.search(r"## Full flag list.*?\n```\n(.*?)\n```", readme, re.S)
_captured = []
with mock.patch.object(cs, "utf8_print",
                       lambda *a, **k: _captured.append(" ".join(str(x) for x in a))):
    cs.cmd_help()
check("'Full flag list' block equals the --help output",
      bool(_block) and _block.group(1).rstrip() == "\n".join(_captured).rstrip(),
      "regenerate it with: python claude_status.py --help")

print("\n== providers ==")
# Ids used with --providers (enable/disable/label/remove/add-file) must start
# with a real provider type; <placeholders> are skipped.
_ids = []
for _arg in re.findall(
        r"--providers (?:enable|disable|label|remove|add-file) ([A-Za-z0-9:_,<>-]+)", readme):
    _ids += [i for i in _arg.split(",") if i and not i.startswith("<")]
check("README shows at least one --providers id", bool(_ids))
for pid in _ids:
    check(f"provider id '{pid}' uses a real type", pid.split(":")[0] in cs.PROVIDER_TYPES,
          f"types: {', '.join(cs.PROVIDER_TYPES)}")
for ptype in cs.PROVIDER_TYPES:
    check(f"provider type '{ptype}' appears in an id", f"`{ptype}" in readme)

print("\n== /pulse subcommands ==")
# A subcommand the README advertises must be one pulse.md routes: either a word
# it spells out, or a theme name (the routing table lists the themes).
for word in sorted(set(re.findall(r"/pulse ([a-z][a-z-]*)", readme))):
    check(f"'/pulse {word}' is handled by pulse.md",
          word in pulse_md or word in cs.THEMES)

print("\n== layouts / bar sizes ==")
for size in cs.BAR_SIZES:
    check(f"bar size '{size}' documented", size in readme)
for layout in cs.LAYOUTS:
    check(f"layout '{layout}' documented", layout in readme)

print("\n== example status line reflects current reality ==")
example = next((l for l in readme.split("\n") if l.startswith("Session ")), "")
check("example exists", bool(example))
check("example has no peak segment", "Peak" not in example, example[:80])
check("example uses a current model",
      any(m in example for m in ("Opus 5.5", "Fable", "Sonnet 5.5")),
      example[:120])

print("\n== no removed features referenced ==")
for gone in ("--peak-hours", "peak_hours", "In Peak", "Off-Peak"):
    check(f"'{gone}' absent", gone not in readme)
# 3.6.2 removed the activity widget (⚡ Active).
check("'⚡ Active' absent", "⚡ Active" not in readme)
check("no `activity` widget row", "| `activity` |" not in readme)
valid_parts = next((l for l in pulse_md.split("\n") if l.startswith("Valid parts:")), "")
check("pulse.md does not offer activity",
      "activity" not in re.split(r"[ ,:]+", valid_parts), valid_parts[:80])

print("\n== removed functions not referenced ==")
check("_check_peak_hours gone from code", not hasattr(cs, "_check_peak_hours"))
check("peak not in WIDGET_PRIORITY", "peak" not in cs.WIDGET_PRIORITY)

print("\n== settings key correctness ==")
check("README documents refreshInterval", "refreshInterval" in readme)
check("README does not promote the dead 'refresh' key",
      not re.search(r'"refresh"\s*:', readme))

print("\n== model claims ==")
for m in ("Fable", "Opus", "Sonnet", "Haiku"):
    check(f"model family '{m}' mentioned", m in readme)

print("\n== links / assets exist ==")
for src in re.findall(r'src="([^"]+)"', readme):
    if src.startswith("http"):
        continue
    check(f"asset {src} exists", (REPO / src).exists())

print("\n" + ("README AUDIT CLEAN" if not issues else f"{len(issues)} ISSUE(S):"))
for i in issues:
    print("  -", i)


class ReadmeClaimsTest(__import__("unittest").TestCase):
    """Lets ``unittest discover`` run this audit alongside the suite."""

    def test_readme_audit_is_clean(self):
        self.assertEqual(issues, [], "; ".join(issues))


if __name__ == "__main__":
    sys.exit(1 if issues else 0)
