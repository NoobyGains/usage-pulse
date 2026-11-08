#!/usr/bin/env python3
"""Claude Code status line — reads usage data from Claude Code's stdin and displays real-time bars."""

VERSION = "3.8.0"

import base64
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CACHE_TTL = 60
BAR_SIZES = {"small": 4, "small-medium": 6, "medium": 8, "medium-large": 10, "large": 12}
DEFAULT_BAR_SIZE = "large"
# "auto" is the shrink-to-fit mode: bars start at the large size and narrow
# until every section fits the width budget. A named size is exactly that
# wide whatever the window does; a line that does not fit is truncated at
# --max-width or wrapped with --wrap auto instead.
BAR_SIZE_AUTO = "auto"
BAR_SIZE_CHOICES = tuple(BAR_SIZES) + (BAR_SIZE_AUTO,)


def _bar_width_setting(config):
    """``(width, fit)`` for the configured ``bar_size``: the bar width in
    characters, and whether it may shrink to fit the terminal."""
    name = config.get("bar_size", DEFAULT_BAR_SIZE)
    if name == BAR_SIZE_AUTO:
        return BAR_SIZES["large"], True
    return BAR_SIZES.get(name, BAR_SIZES[DEFAULT_BAR_SIZE]), False
DEFAULT_MAX_WIDTH_PCT = 80  # percentage of terminal width to use
FILL = "\u2501"   # ━ (thin horizontal bar)
EMPTY = "\u2500"   # ─ (thin line)

# Bar styles — each maps to (filled_char, empty_char)
BAR_STYLES = {
    "classic": ("\u2501", "\u2500"),   # ━ ─
    "block":   ("\u2588", "\u2591"),   # █ ░
    "shade":   ("\u2593", "\u2591"),   # ▓ ░
    "pipe":    ("\u2503", "\u250A"),   # ┃ ┊
    "dot":     ("\u25CF", "\u25CB"),   # ● ○
    "square":  ("\u25A0", "\u25A1"),   # ■ □
    "star":    ("\u2605", "\u2606"),   # ★ ☆
}
DEFAULT_BAR_STYLE = "classic"

# Gradient bar styles — each maps to (gradient_string, empty_char).
# gradient_string: chars from empty to full; gives (len - 1) sub-levels per position.
# empty_char: visible placeholder for unfilled slots (rendered DIM).
BAR_GRADIENT_STYLES = {
    "braille": ("\u28C0\u28C4\u28E4\u28E6\u28F6\u28F7\u28FF", "\u28C0"),
    #  gradient: ⣀     ⣄     ⣤     ⣦     ⣶     ⣷     ⣿   empty: ⣀
}

# Auto-register gradient styles into BAR_STYLES (last char = filled, empty_char)
for _gname, (_gchars, _gempty) in BAR_GRADIENT_STYLES.items():
    BAR_STYLES[_gname] = (_gchars[-1], _gempty)

# Precompute all bar characters for rainbow detection
ALL_BAR_CHARS = set()
for _f, _e in BAR_STYLES.values():
    ALL_BAR_CHARS.add(_f)
    ALL_BAR_CHARS.add(_e)
for _gchars, _gempty in BAR_GRADIENT_STYLES.values():
    for _gc in _gchars:
        ALL_BAR_CHARS.add(_gc)


# Text layouts — controls how labels, bars, and percentages are arranged
LAYOUTS = ("standard", "compact", "minimal", "percent-first")
DEFAULT_LAYOUT = "standard"

# ANSI colour codes
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
WHITE = "\033[37m"
BRIGHT_WHITE = "\033[97m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_RED = "\033[91m"
ORANGE_256 = "\033[38;5;208m"
BRIGHT_ORANGE_256 = "\033[38;5;214m"
PRIDE_VIOLET = "\033[38;5;135m"
PRIDE_GREEN = "\033[38;5;49m"
PRIDE_PINK = "\033[38;5;199m"
FROST_ICE = "\033[38;5;159m"
FROST_STEEL = "\033[38;5;75m"
EMBER_GOLD = "\033[38;5;220m"
EMBER_HOT = "\033[38;5;202m"
CANDY_PINK = "\033[38;5;213m"
CANDY_PURPLE = "\033[38;5;141m"
CANDY_CYAN = "\033[38;5;51m"

# Theme definitions — each maps usage levels to ANSI colour codes
# "rainbow" uses representative colours for previews; actual rendering is animated
THEMES = {
    "default": {"low": GREEN, "mid": YELLOW, "high": RED},
    "ocean":   {"low": CYAN, "mid": BLUE, "high": MAGENTA},
    "sunset":  {"low": YELLOW, "mid": ORANGE_256, "high": RED},
    "mono":    {"low": WHITE, "mid": WHITE, "high": BRIGHT_WHITE},
    "neon":    {"low": BRIGHT_GREEN, "mid": BRIGHT_YELLOW, "high": BRIGHT_RED},
    "pride":   {"low": PRIDE_VIOLET, "mid": PRIDE_GREEN, "high": PRIDE_PINK},
    "frost":   {"low": FROST_ICE, "mid": FROST_STEEL, "high": BRIGHT_WHITE},
    "ember":   {"low": EMBER_GOLD, "mid": EMBER_HOT, "high": BRIGHT_RED},
    "candy":   {"low": CANDY_PINK, "mid": CANDY_PURPLE, "high": CANDY_CYAN},
    "rainbow": {"low": BRIGHT_GREEN, "mid": BRIGHT_YELLOW, "high": MAGENTA},
}

PLAN_NAMES = {
    "default_claude_ai": "Pro",
    "default_claude_max_5x": "Max 5x",
    "default_claude_max_20x": "Max 20x",
}

# Model id → short display name. Longest-prefix wins (see _model_short_name),
# so "claude-opus-4-8" beats the bare "claude-opus-4" entry.
MODEL_SHORT_NAMES = {
    "claude-fable-5-1": "Fable",
    "claude-fable-5": "Fable",
    "claude-mythos-5": "Mythos",
    "claude-mythos-preview": "Mythos",
    "claude-opus-5-5": "Opus",
    "claude-opus-5": "Opus",
    "claude-opus-4-8": "Opus",
    "claude-opus-4-7": "Opus",
    "claude-opus-4-6": "Opus",
    "claude-opus-4-5": "Opus",
    "claude-opus-4": "Opus",
    "claude-sonnet-5-5": "Sonnet",
    "claude-sonnet-5": "Sonnet",
    "claude-sonnet-4-6": "Sonnet",
    "claude-sonnet-4-5": "Sonnet",
    "claude-sonnet-4": "Sonnet",
    "claude-haiku-5-5": "Haiku",
    "claude-haiku-4-5": "Haiku",
    "claude-haiku-4": "Haiku",
    "claude-3-5-sonnet": "Sonnet",
    "claude-3-5-haiku": "Haiku",
    "claude-3-opus": "Opus",
}

# Context window sizes by model display name, as it arrives on stdin
# ("Opus 4.6", "Sonnet 5", ...) with a bare-family fallback. Only consulted
# when stdin omits context_window.context_window_size; when that field is
# present we always trust it over this table.
#
# The Claude 5 family and Opus/Sonnet 4.6+ are 1M-context; Haiku 4.5 and the
# Claude 3.x models remain 200K.
MODEL_CONTEXT_WINDOWS = {
    "Fable": 1_000_000,
    "Fable 5": 1_000_000,
    "Fable 5.1": 1_000_000,
    "Mythos": 1_000_000,
    "Mythos Preview": 1_000_000,
    "Mythos 5": 1_000_000,
    "Opus": 1_000_000,
    "Opus 5": 1_000_000,
    "Opus 5.5": 1_000_000,
    "Opus 4.8": 1_000_000,
    "Opus 4.7": 1_000_000,
    "Opus 4.6": 1_000_000,
    "Opus 4.5": 200_000,
    "Sonnet": 1_000_000,
    "Sonnet 5": 1_000_000,
    "Sonnet 5.5": 1_000_000,
    "Sonnet 4.6": 1_000_000,
    "Sonnet 4.5": 200_000,
    "Sonnet 4": 200_000,
    "Haiku": 200_000,
    "Haiku 4.5": 200_000,
    "Haiku 5.5": 1_000_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000

# API pricing per million tokens (USD).
# https://platform.claude.com/docs/en/about-claude/pricing
# Cache read is 0.1x input (0.05x for Opus 5.5 / Sonnet 5.5, 0.025x for
# Fable 5.1); 5-minute cache write is 1.25x input.
API_PRICING = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-mythos-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-mythos-preview": {"input": 25.0, "output": 125.0, "cache_read": 2.5, "cache_write": 31.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    # Sonnet 5 launched at a $2/$10 introductory rate due to rise to $3/$15
    # on 2026-09-01; Anthropic cancelled the increase and made the launch
    # rate the standard price (pricing docs, note
    # "claude-sonnet-5-introductory-pricing"), so $2/$10 is the list price.
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    # Haiku 5.5 rates are for prompts up to 100K tokens; see
    # API_PRICING_LONG_CONTEXT for the surcharge above that.
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    "claude-3-opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
}
# Display names for pricing table
API_PRICING_DISPLAY = {
    "claude-fable-5-1": "Fable 5.1",
    "claude-fable-5": "Fable 5",
    "claude-mythos-5": "Mythos 5",
    "claude-mythos-preview": "Mythos Preview",
    "claude-opus-5-5": "Opus 5.5",
    "claude-opus-5": "Opus 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-5": "Opus 4.5",
    "claude-opus-4": "Opus 4",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-sonnet-5": "Sonnet 5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-sonnet-4": "Sonnet 4",
    "claude-haiku-5-5": "Haiku 5.5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "claude-haiku-4-5": "Haiku 4.5",
    "claude-3-5-sonnet": "Sonnet 3.5",
    "claude-3-5-haiku": "Haiku 3.5",
    "claude-3-opus": "Opus 3",
}


# Introductory / promotional rates, applied until the stated UTC date.
# Keeping these separate from API_PRICING means the table remains the list
# price and the promo simply expires on its own without a code change.
# Currently empty: Sonnet 5's $2/$10 launch rate became the standard price
# (the scheduled 2026-09-01 increase was cancelled), so it lives in
# API_PRICING proper. Shape, keyed by model id:
#     "model-id": {"until": "YYYY-MM-DD", "pricing": {...API_PRICING shape...}},
API_PRICING_PROMOS = {}

# Long-context surcharge. Haiku 5.5 is the only model whose per-request price
# steps up with prompt size: a request whose prompt (input + cache reads +
# cache writes) exceeds ``threshold`` tokens is billed at the ``pricing`` rates
# for the WHOLE request, cache hits included. Every request is priced on its
# own. Source: the "Long context pricing" section of
# https://platform.claude.com/docs/en/about-claude/pricing
API_PRICING_LONG_CONTEXT = {
    "claude-haiku-5-5": {
        "threshold": 100_000,
        "pricing": {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625},
    },
}


def _pricing_for(model_id, at=None, prompt_tokens=None):
    """Return the per-MTok rates for *model_id*, honouring active promos.

    *at* is a UTC date (defaults to today). A promo whose end date has passed
    falls back to the list price automatically.

    *prompt_tokens* (input + cache reads + cache writes of one request) selects
    the long-context rates for models listed in ``API_PRICING_LONG_CONTEXT``
    (Haiku 5.5 above 100K). Left as None, the standard rates are returned.

    Known limitation: fast mode bills Opus 5 / 4.8 at $10/$50 rather than
    $5/$25, but transcripts do not record whether a request used it, so
    historical scans price fast-mode turns at the standard rate and will
    under-report them.
    """
    base = API_PRICING.get(model_id)
    if base is None:
        return None
    long_ctx = API_PRICING_LONG_CONTEXT.get(model_id)
    if long_ctx and isinstance(prompt_tokens, (int, float))             and prompt_tokens > long_ctx["threshold"]:
        return long_ctx["pricing"]
    promo = API_PRICING_PROMOS.get(model_id)
    if not promo:
        return base
    try:
        until = date.fromisoformat(promo["until"])
        today = at or datetime.now(timezone.utc).date()
        if today <= until:
            return promo["pricing"]
    except (ValueError, TypeError, KeyError):
        pass
    return base


def _model_short_name(model_id):
    """Map a model id to its short family name, longest prefix wins.

    ``MODEL_SHORT_NAMES`` holds both bare-family keys ("claude-opus-4") and
    versioned ones ("claude-opus-4-8"). A plain dict lookup misses dated
    variants like ``claude-haiku-4-5-20251001``, and a naive prefix scan would
    let "claude-opus-4" shadow "claude-opus-4-8", so match on the longest key
    that the id starts with.
    """
    if not model_id:
        return None
    best = None
    for key, short in MODEL_SHORT_NAMES.items():
        if model_id.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, short)
    return best[1] if best else None

# ---------------------------------------------------------------------------
# Hook infrastructure constants
# ---------------------------------------------------------------------------
HEARTBEAT_SPINNER = ["|", "/", "-", "\\"]  # Classic ASCII spinner — renders on every terminal
HOOK_STATE_FRESH_TTL = 300  # 5 minutes — keeps heartbeat visible between interactions
GIT_BRANCH_CACHE_TTL = 60

# ---------------------------------------------------------------------------
# Analytics constants
# ---------------------------------------------------------------------------
VELOCITY_ARROW_UP = "\u2191"    # ↑
VELOCITY_ARROW_FLAT = "\u2192"  # →
VELOCITY_ARROW_DOWN = "\u2193"  # ↓
STALENESS_WARN = 120
STALENESS_YELLOW = 300
STALENESS_RED = 600
CONTEXT_PRESSURE_PCT = 70
CONTEXT_PRESSURE_CRITICAL = 90
CONTEXT_PRESSURE_VELOCITY = 5.0

# ---------------------------------------------------------------------------
# Animation constants
# ---------------------------------------------------------------------------
ANIMATION_SPEEDS = {"slow": 0.5, "normal": 1.0, "fast": 2.0}
DEFAULT_ANIMATION_SPEED = "normal"
FLASH_THRESHOLDS = (50, 75, 90)
FLASH_DURATION = 3
FLASH_RENDER_TTL = 45.0
CELEBRATION_DURATION = 5
CELEBRATION_DROP_THRESHOLD = 20.0
CELEBRATION_CHAR = "\u2726"  # ✦

# ---------------------------------------------------------------------------
# Multi-session / Focus / Git drift constants
# ---------------------------------------------------------------------------
SESSION_STALE_SECONDS = 300
SESSION_DIR_NAME = "sessions"
POMODORO_DEFAULT_MINUTES = 25
POMODORO_BREAK_MINUTES = 5
POMODORO_FILE = "pomodoro.json"
GIT_DRIFT_CACHE_TTL = 300
GIT_DRIFT_FILE = "git_drift.json"
FILES_CHANGED_CACHE_TTL = 30
FILES_CHANGED_FILE = "files_changed.json"

_cached_terminal_width = None

def _detect_terminal_width():
    """Detect the real terminal width, even when stdout is piped.

    Tries progressively heavier approaches, returning the first success:
      1. os.get_terminal_size(stdout)  — direct TTY
      2. os.get_terminal_size(stderr)  — works when only stdout is piped
      3. os.get_terminal_size(stdin)   — same
      4. /dev/tty via os.get_terminal_size — POSIX controlling terminal
      5. COLUMNS env var               — honours explicit overrides
      6. /proc walk (Linux only)        — last resort; walks parent pids
         to find an ancestor with a terminal fd

    Returns the column count (int), or None if undetectable.
    The result is cached for the lifetime of the process (a single
    status-line render), avoiding repeated /proc I/O.
    """
    global _cached_terminal_width
    if _cached_terminal_width is not None:
        return _cached_terminal_width if _cached_terminal_width > 0 else None

    cols = _detect_terminal_width_uncached()
    _cached_terminal_width = cols if cols is not None else -1
    return cols


def _detect_terminal_width_uncached():
    """Internal: attempt each detection method in order."""
    # 0. Claude Code sets COLUMNS/LINES for the status-line command (statusline
    #    docs). When stdout is a pipe we are being run by it, and that value
    #    is the width of the area the line is drawn in — the controlling
    #    terminal's width (steps 4 and 6) can be wider than the pane.
    try:
        if not sys.stdout.isatty():
            cols = int(os.environ["COLUMNS"])
            if cols > 0:
                return cols
    except (KeyError, ValueError, AttributeError, OSError):
        pass
    # 1-3. Try each standard fd — stderr/stdin may still be a TTY
    for fd in (sys.stdout, sys.stderr, sys.stdin):
        try:
            return os.get_terminal_size(fd.fileno()).columns
        except (OSError, ValueError, AttributeError):
            pass
    # 4. POSIX controlling terminal — works on Linux + macOS even with piped stdio
    try:
        tty_fd = os.open("/dev/tty", os.O_RDONLY | getattr(os, "O_NOCTTY", 0))
        try:
            return os.get_terminal_size(tty_fd).columns
        finally:
            os.close(tty_fd)
    except OSError:
        pass
    # 5. Explicit COLUMNS environment variable
    try:
        cols = int(os.environ["COLUMNS"])
        if cols > 0:
            return cols
    except (KeyError, ValueError):
        pass
    # 6. Linux /proc walk — last resort (see _detect_width_from_proc)
    return _detect_width_from_proc()


def _detect_width_from_proc():
    """Query terminal width by walking the Linux process tree via /proc.

    When all standard detection methods fail (common inside Claude Code where
    stdout/stderr/stdin are all pipes and /dev/tty is unavailable), this walks
    up the parent process chain looking for an ancestor that holds an open
    file descriptor to a PTY.  It then queries TIOCGWINSZ on that fd to get
    the real terminal dimensions.

    Linux-only (/proc is not available on macOS/Windows).  Returns None on
    non-Linux platforms or if no terminal fd is found.  All filesystem and
    ioctl operations are individually caught so a failure at any point
    (permissions, process exit, container restrictions) is silently skipped.

    This function is self-contained — it can be removed without affecting
    the rest of the detection cascade, which will fall back to shutil.
    """
    if sys.platform != "linux":
        return None
    try:
        import fcntl, termios, struct  # Linux-only modules
        pid = os.getpid()
        for _ in range(10):
            with open(f"/proc/{pid}/stat") as f:
                raw = f.read()
            # Parse ppid safely: comm field "(name)" can contain spaces
            # and parens, so find the *last* ')' before splitting.
            ppid = int(raw[raw.rfind(")") + 2:].split()[1])
            if ppid <= 1:
                break
            fd_dir = f"/proc/{ppid}/fd"
            try:
                for fd_name in os.listdir(fd_dir):
                    try:
                        target = os.readlink(f"{fd_dir}/{fd_name}")
                        if "/pts/" not in target and "/tty" not in target:
                            continue
                        fd = os.open(f"{fd_dir}/{fd_name}", os.O_RDONLY)
                        try:
                            buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
                            cols = struct.unpack("HHHH", buf)[1]
                            if cols > 0:
                                return cols
                        finally:
                            os.close(fd)
                    except (OSError, PermissionError):
                        continue
            except (OSError, PermissionError):
                pass
            pid = ppid
    except (OSError, ValueError):
        pass
    return None

def _sanitize(text):
    """Strip ANSI/terminal escape sequences and control characters from untrusted strings."""
    # Strip CSI (\x1b[...), OSC (\x1b]...), DCS (\x1bP...) and other escape sequences
    cleaned = re.sub(r'\x1b[^a-zA-Z]*[a-zA-Z]', '', str(text))
    # Strip remaining control characters (keep \n for multi-line contexts)
    return re.sub(r'[\x00-\x09\x0b-\x1f\x7f-\x9f]', '', cleaned)

# Named text colours for non-bar text (labels, percentages, separators)
TEXT_COLORS = {
    "white": "\033[37m",
    "bright_white": "\033[97m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "magenta": "\033[35m",
    "red": "\033[31m",
    "orange": "\033[38;5;208m",
    "violet": "\033[38;5;135m",
    "pink": "\033[38;5;199m",
    "dim": "\033[2;37m",
    "default": "\033[39m",
    "none": "",
}

# Accent text colour per theme — used in previews/demos to make each theme look distinct
THEME_DEMO_TEXT = {
    "default": "green",
    "ocean":   "cyan",
    "sunset":  "yellow",
    "mono":    "dim",
    "neon":    "green",
    "pride":   "violet",
    "frost":   "cyan",
    "ember":   "yellow",
    "candy":   "pink",
    "rainbow": "none",
}

# Recommended text colour per theme — chosen for good contrast with bars
# so the rainbow has something to contrast against
THEME_TEXT_DEFAULTS = {
    "default": "white",
    "ocean":   "white",
    "sunset":  "white",
    "mono":    "dim",
    "neon":    "white",
    "pride":   "white",
    "frost":   "white",
    "ember":   "white",
    "candy":   "white",
    "rainbow": "none",
}

# Widget priorities — lower number = rendered first (leftmost).
# Users can override via config["widget_priority"] = {"session": 1, "weekly": 2, ...}
WIDGET_PRIORITY = {
    "session": 10, "weekly": 20, "opus": 30, "sonnet": 40, "fable": 45, "extra": 50,
    "context": 60, "cache": 65, "cost": 70, "cumulative_cost": 72,
    "weekly_cost": 73, "lines": 75, "plan": 90,
    "streak": 100, "model": 110, "effort": 120, "fast_mode": 122,
    "thinking": 124, "agent": 126, "worktree": 130,
    "heartbeat": 140, "last_tool": 160, "branch": 170,
    "pr": 175,
    "subagents": 178, "team": 179, "sessions": 180, "budget": 185, "pomodoro": 190, "git_drift": 200,
    "files_changed": 210,
    # stdin widgets (3.4.0)
    "cache_warm": 66, "spend_limit": 52, "repo": 172, "session_name": 105,
}

# Reasoning-effort display. Claude Code reports low|medium|high|xhigh|max.
# Three renderings, because "med" reads as cryptic to anyone who hasn't
# memorised the abbreviations, while the full word costs width on a busy bar.
EFFORT_SHORT = {
    "low": "lo", "medium": "med", "high": "hi", "xhigh": "xh", "max": "max",
}
EFFORT_FULL = {
    "low": "Low", "medium": "Medium", "high": "High",
    "xhigh": "XHigh", "max": "Max",
}
EFFORT_FORMATS = ("short", "full", "labeled")
# Default to the labelled form: "med" is only legible if you already know the
# abbreviations, and even "Medium" on its own is ambiguous next to a model name.
DEFAULT_EFFORT_FORMAT = "labeled"


def _format_effort(level, effort_format=DEFAULT_EFFORT_FORMAT):
    """Render a reasoning-effort level per the user's ``effort_format``.

    short   → ``med``
    full    → ``Medium``
    labeled → ``Effort: Medium``  (default; unambiguous at a glance)

    An unrecognised level is passed through rather than dropped — a new effort
    tier should still show up, just without a prettier name.
    """
    if not level:
        return ""
    if effort_format not in EFFORT_FORMATS:
        effort_format = DEFAULT_EFFORT_FORMAT
    if effort_format == "short":
        return EFFORT_SHORT.get(level, level)
    full = EFFORT_FULL.get(level, level.title())
    return f"Effort: {full}" if effort_format == "labeled" else full

# Higher effort burns limits faster, so escalate the colour with it.
EFFORT_COLOURS = {
    "low": DIM, "medium": "", "high": "", "xhigh": YELLOW, "max": BRIGHT_RED,
}

# Pull-request review states, as reported on stdin.
PR_STATE_GLYPHS = {
    "approved": "✓",       # ✓
    "changes_requested": "✗",  # ✗
    "pending": "•",        # •
}
PR_STATE_COLOURS = {
    "approved": GREEN,
    "changes_requested": RED,
    "pending": YELLOW,
}

# Claude Code enforces its own per-session caps but exposes them neither on
# stdin nor in settings.json, so these are usage-pulse's own denominators.
# They default to Claude Code's documented values; override under "limits" in
# config.json if your setup differs. Set any to 0 to hide that denominator.
DEFAULT_LIMITS = {
    "subagent_spawns": 200,      # per-session subagent spawn cap
    "subagent_concurrent": 20,   # concurrently-running subagent cap
    "web_searches": 200,         # per-session WebSearch call cap
}

DEFAULT_SHOW = {
    # Core bars — always visible
    "session": True,
    "weekly": True,
    "context": True,
    "timer": True,
    "weekly_timer": True,
    # The bars themselves. Off keeps that window's percentage and timer and
    # drops the bar, for a narrower line (idea from @lucachitayat's fork).
    "session_bar": True,
    "weekly_bar": True,
    "context_bar": True,
    # Info line
    "cost": True,
    "model": True,
    "branch": True,
    # Opt-in: the spinner and tool counter are noise for most people, and both
    # need the PostToolUse hook anyway. --show heartbeat (or /pulse) turns them
    # on. Leaving them off by default also means a stock config asks for no
    # repaint timer at all.
    "heartbeat": False,
    "update": True,
    "claude_update": True,
    # Per-model caps (show when available)
    # Per-model weekly caps are not on stdin; drawing them costs one usage-API
    # call every model_caps_refresh_seconds, so they are opt-in (--show fable).
    "opus": False,
    "sonnet": False,
    "fable": False,
    # Opt-in features
    "plan": False,
    "extra": False,
    "effort": True,
    "fast_mode": True,
    "thinking": False,
    "agent": True,
    "worktree": True,
    "pomodoro": True,
    "context_warning": True,
    "staleness": True,
    "lines": True,
    # Hidden by default — opt-in with --show
    "cumulative_cost": False,
    "weekly_cost": False,
    "cache": False,
    "subagents": True,
    "team": True,
    "budget": True,
    "pr": False,
    "burn_rate": False,
    "sessions": False,
    "last_tool": False,
    "sparkline": False,
    "runway": False,
    "status_message": False,
    "streak": False,
    "pace": False,
    "git_drift": False,
    "files_changed": False,
    # stdin widgets (3.4.0)
    "cache_warm": False,
    "spend_limit": True,
    "repo": False,
    "session_name": False,
    # providers (3.4.0)
    "providers": True,
}

# Presets — one-command config bundles
PRESETS = {
    "minimal": {
        "description": "Compact bar that leaves room for Claude Code notifications",
        "config": {
            "bar_size": "small",
            "layout": "compact",
            "max_width": 60,
        },
        "show_overrides": {
            "plan": False,
            "model": False,
            "context": False,
            "sparkline": False,
            "runway": False,
            "status_message": False,
            "streak": False,
        },
    },
    "default": {
        "description": "Factory reset — all settings back to defaults",
        "config": {
            "theme": "default",
            "text_color": "auto",
            "animate": "off",
            "animation_speed": DEFAULT_ANIMATION_SPEED,
            "bar_size": DEFAULT_BAR_SIZE,
            "bar_style": DEFAULT_BAR_STYLE,
            "layout": DEFAULT_LAYOUT,
            "max_width": DEFAULT_MAX_WIDTH_PCT,
            "context_format": "percent",
            "extra_display": "auto",
            "currency": "$",
        },
        "show_overrides": dict(DEFAULT_SHOW),
    },
}

# Sparkline and history constants
SPARKLINE_CHARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
HISTORY_MAX_AGE = 86400  # 24 hours in seconds


# ---------------------------------------------------------------------------
# Rainbow animation helpers
# ---------------------------------------------------------------------------

# Ultrathink rainbow palette — matches Claude Code's ultrathink colors
_ULTRATHINK_BASE = [
    (235, 95, 87),   # red
    (245, 139, 87),  # orange
    (250, 195, 95),  # yellow
    (145, 200, 130), # green
    (130, 170, 220), # blue
    (155, 130, 200), # indigo
    (200, 130, 180), # violet
]

_ULTRATHINK_SHIMMER = [
    (250, 155, 147), # red shimmer
    (255, 185, 137), # orange shimmer
    (255, 225, 155), # yellow shimmer
    (185, 230, 180), # green shimmer
    (180, 205, 240), # blue shimmer
    (195, 180, 230), # indigo shimmer
    (230, 180, 210), # violet shimmer
]


def _lerp_color(c1, c2, t):
    """Linearly interpolate between two RGB tuples."""
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _ultrathink_color(pos, shimmer_t=0.0):
    """Map position (0.0-1.0) to an ultrathink rainbow color.

    shimmer_t: 0.0 = base colors, 1.0 = full shimmer colors.
    """
    n = len(_ULTRATHINK_BASE)
    scaled = pos * n
    idx = int(scaled) % n
    frac = scaled - int(scaled)
    next_idx = (idx + 1) % n
    base = _lerp_color(_ULTRATHINK_BASE[idx], _ULTRATHINK_BASE[next_idx], frac)
    if shimmer_t > 0.0:
        shimmer = _lerp_color(_ULTRATHINK_SHIMMER[idx], _ULTRATHINK_SHIMMER[next_idx], frac)
        return _lerp_color(base, shimmer, shimmer_t)
    return base


def _get_animation_speed(config=None):
    """Return the animation speed multiplier from config."""
    if config is None:
        config = load_config()
    speed_name = config.get("animation_speed", DEFAULT_ANIMATION_SPEED)
    return ANIMATION_SPEEDS.get(speed_name, 1.0)


def rainbow_colorize(text, color_all=True, shimmer=True, config=None):
    """Apply rainbow colouring — animated when processing, clean static when idle.

    shimmer=True  — Claude is processing: hue drifts each frame (smooth gradient shift).
    shimmer=False — Claude is idle: static rainbow gradient, no animation.

    color_all=True  — strip existing ANSI, rainbow every character.
    color_all=False — preserve ANSI-colored chars (bars), rainbow the rest.
    """
    now = time.time()
    speed = _get_animation_speed(config) if config else 1.0

    if shimmer:
        hue_drift = now * 0.8 * speed
    else:
        hue_drift = 0.0

    result = []
    visible_idx = 0
    has_existing_color = False
    i = 0

    while i < len(text):
        if text[i] == "\033":
            # Step over the whole escape sequence: a CSI colour code, or an
            # OSC 8 hyperlink (which ends in BEL, not a letter, and whose URL
            # can contain an "m" that looked like the end of a colour code).
            j = _skip_escape(text, i)
            seq = text[i:j]
            i = j
            if color_all:
                continue
            if seq.startswith("\033[") and seq.endswith("m"):
                has_existing_color = seq not in ("\033[0m", "\033[m")
            result.append(seq)
            continue

        if not color_all and has_existing_color:
            result.append(text[i])
        else:
            pos = ((visible_idx * 0.025) + hue_drift) % 1.0
            if shimmer:
                pulse = abs((now * 1.5 * speed) % 2.0 - 1.0)
            else:
                pulse = 0.0
            r, g, b = _ultrathink_color(pos, pulse)
            result.append(f"\033[38;2;{r};{g};{b}m{text[i]}\033[0m")

        visible_idx += 1
        i += 1

    result.append(RESET)
    return "".join(result)


def _brighten_rgb(r, g, b, factor):
    """Brighten an RGB colour by a multiplicative factor, clamped to 255."""
    return (min(255, int(r * factor)), min(255, int(g * factor)), min(255, int(b * factor)))


def _parse_ansi_color_rgb(ansi_code):
    """Extract RGB from an ANSI escape code, or return None."""
    if not ansi_code:
        return None
    m = re.match(r'\033\[38;2;(\d+);(\d+);(\d+)m', ansi_code)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r'\033\[38;5;(\d+)m', ansi_code)
    if m:
        n = int(m.group(1))
        if n < 16:
            _B16 = [(0,0,0),(128,0,0),(0,128,0),(128,128,0),(0,0,128),(128,0,128),(0,128,128),(192,192,192),
                    (128,128,128),(255,0,0),(0,255,0),(255,255,0),(0,0,255),(255,0,255),(0,255,255),(255,255,255)]
            return _B16[n]
        elif n < 232:
            n -= 16
            return ((n // 36) * 51, ((n % 36) // 6) * 51, (n % 6) * 51)
        else:
            v = 8 + (n - 232) * 10
            return (v, v, v)
    m = re.match(r'\033\[(\d+)m', ansi_code)
    if m:
        code = int(m.group(1))
        _BM = {30:(0,0,0),31:(170,0,0),32:(0,170,0),33:(170,170,0),34:(0,0,170),35:(170,0,170),
               36:(0,170,170),37:(170,170,170),90:(85,85,85),91:(255,85,85),92:(85,255,85),
               93:(255,255,85),94:(85,85,255),95:(255,85,255),96:(85,255,255),97:(255,255,255)}
        return _BM.get(code)
    return None


ANIMATE_MODES = ("off", "rainbow", "pulse", "glow", "shift")


def _anim_phase(config=None, freq=2.0):
    """Return a phase (0.0 to 1.0) based on time and animation speed."""
    speed = _get_animation_speed(config) if config else 1.0
    return 0.5 + 0.5 * math.sin(time.time() * freq * speed)


def _apply_bar_animation(colour, char_idx, bar_width, anim_mode, config=None):
    """Apply animation effect to a single bar character's colour.

    Returns an ANSI colour string, or None to keep original.
    """
    if not colour or anim_mode in ("off", "rainbow"):
        return None
    rgb = _parse_ansi_color_rgb(colour)
    if not rgb:
        return None
    speed = _get_animation_speed(config) if config else 1.0
    now = time.time()

    if anim_mode == "pulse":
        # Bar cycles through theme's low → mid → high colours over time;
        # the actual cycling is handled at bar level with _pulse_theme_color.
        return None

    elif anim_mode == "glow":
        # Each character is a different colour — gradient across the bar
        # Creates a visible multi-colour effect like a toned-down rainbow
        if bar_width <= 0:
            return None
        # Map char position + time offset to a hue shift
        pos = ((char_idx / max(bar_width, 1)) + now * 0.3 * speed) % 1.0
        # Lerp through a warm/cool version of the base colour
        warm = (min(255, rgb[0] + 80), max(0, rgb[1] - 40), max(0, rgb[2] - 60))
        cool = (max(0, rgb[0] - 60), min(255, rgb[1] + 40), min(255, rgb[2] + 80))
        if pos < 0.5:
            t = pos * 2
            r, g, b = int(rgb[0] + (warm[0] - rgb[0]) * t), int(rgb[1] + (warm[1] - rgb[1]) * t), int(rgb[2] + (warm[2] - rgb[2]) * t)
        else:
            t = (pos - 0.5) * 2
            r, g, b = int(warm[0] + (cool[0] - warm[0]) * t), int(warm[1] + (cool[1] - warm[1]) * t), int(warm[2] + (cool[2] - warm[2]) * t)
        return f"\033[38;2;{max(0,min(255,r))};{max(0,min(255,g))};{max(0,min(255,b))}m"

    elif anim_mode == "shift":
        # Bright highlight slides across — each char a different brightness
        if bar_width <= 0:
            return None
        pos = (now * 3.0 * speed) % bar_width
        dist = abs(char_idx - pos)
        if dist > bar_width / 2:
            dist = bar_width - dist
        # Sharp highlight: bright white at center, theme colour at edges
        intensity = max(0.0, 1.0 - dist / 2.0)
        intensity = intensity * intensity  # sharper falloff
        r = min(255, int(rgb[0] + (255 - rgb[0]) * intensity))
        g = min(255, int(rgb[1] + (255 - rgb[1]) * intensity))
        b = min(255, int(rgb[2] + (255 - rgb[2]) * intensity))
        return f"\033[38;2;{r};{g};{b}m"

    return None


def resolve_text_color(config):
    """Return the ANSI code for the configured text colour."""
    theme_name = config.get("theme", "default")
    tc = config.get("text_color", "auto")
    if tc == "auto":
        tc = THEME_TEXT_DEFAULTS.get(theme_name, "white")
    return TEXT_COLORS.get(tc, TEXT_COLORS["white"])


def apply_text_color(line, color_code):
    """Wrap non-bar text in a base colour so the rainbow has something to contrast against.

    Prepends the colour, re-applies it after every RESET, and appends a final RESET.
    Bar colours override this inline; after their RESET the base colour resumes.
    """
    if not color_code:
        return line
    # Prepend base colour, replace every \033[0m with \033[0m + base colour,
    # then append a final reset at the end
    return color_code + line.replace("\033[0m", "\033[0m" + color_code) + "\033[0m"



# ---------------------------------------------------------------------------
# Secure file helpers
# ---------------------------------------------------------------------------

def _secure_mkdir(path):
    """Create directory with 0o700 permissions on Unix. Normal mkdir on Windows."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
    if path.exists():
        return
    if sys.platform == "win32":
        path.mkdir(parents=True, exist_ok=True)
    else:
        old_umask = os.umask(0o077)
        try:
            path.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old_umask)


def _secure_open_write(filepath):
    """Open file for writing with 0o600 permissions on Unix. Normal open on Windows."""
    filepath = Path(filepath)
    if filepath.is_symlink():
        filepath.unlink()
    if sys.platform == "win32":
        # Verify resolved path matches expected path (catch junction/symlink re-creation)
        resolved = filepath.resolve()
        expected = filepath.parent.resolve() / filepath.name
        if resolved != expected:
            raise OSError(f"Path resolves unexpectedly: {resolved}")
        return open(filepath, "w", encoding="utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(filepath), flags, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


# Pauses (seconds) between attempts to swap a file into place on Windows,
# about half a second in all. See _replace_with_retry.
_REPLACE_RETRY_PAUSES = (0.01, 0.02, 0.05, 0.1, 0.1, 0.1, 0.1)


def _replace_with_retry(src, dst):
    """``os.replace(src, dst)``, retried briefly on Windows when it is refused.

    Windows will not replace a file another process has open without delete
    sharing, and answers PermissionError (WinError 5 or 32). The holder is
    usually passing through: an on-access virus scanner, the search indexer, a
    repaint reading the same config. A few short pauses ride that out. Any
    other error, and PermissionError elsewhere, is raised at once.
    """
    for pause in _REPLACE_RETRY_PAUSES:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32":
                raise
            time.sleep(pause)
    os.replace(src, dst)


def _json_text(data, indent):
    """``data`` as JSON text, keeping non-ASCII characters readable.

    A lone surrogate cannot be written as UTF-8, so text that holds one falls
    back to ``\\uXXXX`` escapes, which json.load reads straight back.
    """
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(data, indent=indent)
    return text


def _atomic_json_write(filepath, data, indent=2):
    """Atomically write JSON with 0o600 permissions on Unix.

    Writes to a temp sibling first, then uses os.replace() for an atomic swap.
    The sibling's name is unique per writer: two windows that write the same
    cache at the same moment must not truncate, rename or delete each other's
    file. Cleans up its own temp file on failure.

    A symlinked destination (a dotfile manager's settings.json) is written
    through: the swap happens at the link's target, so the link survives
    instead of being replaced by a plain file. Non-ASCII text is written as
    is, not as ``\\uXXXX`` escapes.
    """
    filepath = Path(filepath)
    if os.path.islink(filepath):
        try:
            target = Path(os.path.realpath(filepath))
        except (OSError, ValueError):
            target = filepath
        if target.parent.is_dir():
            filepath = target
    tmp_path = filepath.with_name(
        f"{filepath.name}.{os.getpid()}-{random.randrange(1 << 32):08x}.tmp")
    try:
        text = _json_text(data, indent)
        with _secure_open_write(tmp_path) as f:
            f.write(text)
        _replace_with_retry(str(tmp_path), str(filepath))
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _claude_config_dir():
    """Return Claude Code's active config dir, honouring ``CLAUDE_CONFIG_DIR``.

    Claude Code reads settings, commands and credentials from
    ``$CLAUDE_CONFIG_DIR`` when it is set, falling back to ``~/.claude``.
    ``install.sh`` / ``install.ps1`` already respect it; before this helper
    ``claude_status.py`` always wrote to ``~/.claude``, so a multi-profile user
    got the status line registered in one dir and the ``/pulse`` command in
    another, and the meter silently never appeared.

    An empty or whitespace-only value is treated as unset rather than as the
    process working directory, which is what ``Path("")`` would resolve to.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / ".claude"


def get_config_path():
    """Return path to user config — stored under XDG_CONFIG_HOME, outside the repo."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_dir = base / "claude-status"
    _secure_mkdir(config_dir)
    return config_dir / "config.json"


def _migrate_config_from_cache():
    """One-time migration: move config.json from ~/.cache to ~/.config."""
    new_path = get_config_path()
    if new_path.exists():
        return  # already migrated or user created config at new location
    # Build the old path (XDG_CACHE_HOME based)
    if sys.platform == "win32":
        return  # Windows uses LOCALAPPDATA for both; no migration needed
    old_base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    old_path = old_base / "claude-status" / "config.json"
    if old_path.exists():
        try:
            shutil.move(str(old_path), str(new_path))
        except OSError:
            pass


def _locale_country():
    """Two-letter country of the machine's locale ("DK" for da_DK), or ""."""
    import locale as _locale

    country = ""
    try:
        # getdefaultlocale deprecated in 3.13 but still works on Windows
        loc = _locale.getdefaultlocale()[0] or ""
        if "_" in loc:
            country = loc.split("_")[-1].upper()
    except (ValueError, AttributeError):
        pass
    if not country:
        try:
            loc = _locale.getlocale()[0] or ""
            if "_" in loc:
                country = loc.split("_")[-1].upper()
        except (ValueError, AttributeError):
            pass
    return country


def _detect_default_currency():
    """Auto-detect currency symbol from system locale/timezone.

    "kr" is the symbol of three currencies, so only Sweden gets it (it prices
    as SEK). Denmark and Norway get their ISO code, which names the currency
    unambiguously and which _get_exchange_rate understands.
    """
    country = _locale_country()

    _COUNTRY_CURRENCY = {
        "GB": "\u00a3", "UK": "\u00a3",
        "US": "$",
        "DE": "\u20ac", "FR": "\u20ac", "IT": "\u20ac", "ES": "\u20ac", "NL": "\u20ac",
        "BE": "\u20ac", "AT": "\u20ac", "IE": "\u20ac", "FI": "\u20ac", "PT": "\u20ac", "GR": "\u20ac",
        "JP": "\u00a5",
        "CA": "C$", "AU": "A$", "NZ": "NZ$",
        "CH": "Fr", "SE": "kr", "NO": "NOK", "DK": "DKK",
        "IN": "\u20b9", "KR": "\u20a9", "BR": "R$", "ZA": "R",
        "PL": "z\u0142", "CZ": "K\u010d", "TR": "\u20ba", "IL": "\u20aa",
        "SG": "S$", "HK": "HK$", "MX": "$",
    }

    if country in _COUNTRY_CURRENCY:
        return _COUNTRY_CURRENCY[country]

    # Fallback: timezone-based detection
    try:
        tz_name = datetime.now(timezone.utc).astimezone().tzinfo.tzname(None) or ""
        if tz_name in ("GMT", "BST"):
            return "\u00a3"
        if tz_name in ("CET", "CEST"):
            return "\u20ac"
        if tz_name == "JST":
            return "\u00a5"
    except (AttributeError, OSError):
        pass

    return "$"


def load_config():
    _migrate_config_from_cache()
    user_path = get_config_path()
    repo_path = Path(__file__).parent / "config.json"

    # User config takes priority, fall back to repo template
    # The README tells people to hand-edit this file, and Windows editors save
    # it in whatever they like. json.loads() on bytes detects UTF-8 (with or
    # without a BOM), UTF-16 and UTF-32. A file it cannot decode (ANSI with a
    # pound sign), cannot parse, cannot open, or that is not a JSON object is
    # skipped like a missing one: a raise here is a blank bar on every repaint.
    data = {}
    for path in (user_path, repo_path):
        try:
            with open(path, "rb") as f:
                loaded = json.loads(f.read())
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(loaded, dict):
            data = loaded
            break

    # Clean up removed settings
    data.pop("rainbow_bars", None)
    data.pop("rainbow_mode", None)
    # v3.2.0 removed the peak-hours widget. Drop the settings an older version
    # may have written so they don't linger in --config output forever. The
    # matching show flag is dropped below, once `show` has been read.
    data.pop("peak_hours", None)

    # Apply defaults
    data.setdefault("cache_ttl_seconds", DEFAULT_CACHE_TTL)
    data.setdefault("theme", "default")
    data.setdefault("animate", "off")
    # Legacy compat: True → "rainbow", False → "off"
    if data["animate"] is True:
        data["animate"] = "rainbow"
    elif data["animate"] is False:
        data["animate"] = "off"
    data.setdefault("animation_speed", DEFAULT_ANIMATION_SPEED)
    data.setdefault("text_color", "auto")
    data.setdefault("bar_size", DEFAULT_BAR_SIZE)
    data.setdefault("max_width", DEFAULT_MAX_WIDTH_PCT)
    data.setdefault("bar_style", DEFAULT_BAR_STYLE)
    data.setdefault("layout", DEFAULT_LAYOUT)
    data.setdefault("context_format", "percent")
    data.setdefault("effort_format", DEFAULT_EFFORT_FORMAT)
    data.setdefault("pace_style", DEFAULT_PACE_STYLE)
    data.setdefault("budget_usd", 0)
    limits = data.get("limits", {})
    if not isinstance(limits, dict):
        limits = {}
    for _k, _v in DEFAULT_LIMITS.items():
        limits.setdefault(_k, _v)
    data["limits"] = limits
    data.setdefault("extra_display", "auto")
    # providers (3.4.0): rows for other subscriptions
    data["providers"] = _normalize_providers(data.get("providers"))
    data.setdefault("providers_label_main", True)
    data.setdefault("providers_refresh_seconds", _PROVIDER_REFRESH_DEFAULT)
    if "currency" not in data:
        data["currency"] = _detect_default_currency()
    show = data.get("show", {})
    show.pop("peak", None)  # v3.2.0 — widget removed
    show.pop("activity", None)  # v3.6.2 — widget removed
    for key, default in DEFAULT_SHOW.items():
        show.setdefault(key, default)
    data["show"] = show
    return data


def save_config(config):
    config_path = get_config_path()
    # Only save user-facing keys, not internal ones
    save_data = {k: v for k, v in config.items() if not k.startswith("_")}
    _atomic_json_write(config_path, save_data)
    # Toggling animation or a time-based widget changes whether the status line
    # needs a repaint timer. Re-syncing here rather than at each of the ~20 CLI
    # call sites means a new setting can never forget to do it. Never raises:
    # a settings.json we can't touch must not fail an otherwise-good config save.
    try:
        sync_status_line_refresh(config)
    except Exception:
        pass


def _cleanup_hooks():
    """Remove any legacy usage-pulse hooks from settings.json.

    v2.2.0 removed all hooks — animation is now purely refresh-based.
    This runs once on upgrade and writes a marker to avoid repeat work.
    """
    state_dir = get_state_dir()
    marker = state_dir / "hooks_cleaned"
    if marker.exists():
        return
    settings_path = _claude_config_dir() / "settings.json"
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # No settings file or invalid — nothing to clean
        try:
            with _secure_open_write(marker) as f:
                pass
        except OSError:
            pass
        return

    changed = False
    script_name = "claude_status.py"
    # The hooks installed today share this script name and one of these events
    # (the heartbeat's PostToolUse), so the name alone cannot tell a legacy
    # hook from a current one: this cleanup ran on the first draw after setup
    # and deleted the heartbeat hook it had just installed. A command that
    # carries one of today's flags for its event is current, and stays.
    current_flags = {}
    for events in _HOOK_GROUPS.values():
        for event, flag, _blocking in events:
            current_flags.setdefault(event, []).append(flag)

    def is_legacy(command, event):
        command = str(command or "")
        return script_name in command and not any(
            flag in command for flag in current_flags.get(event, ()))

    for hook_type in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        hooks = settings.get("hooks", {}).get(hook_type, [])
        if hooks:
            filtered = []
            for h in hooks:
                is_pulse = False
                # Old format: {"command": "...claude_status.py..."}
                if is_legacy(h.get("command", ""), hook_type):
                    is_pulse = True
                # New nested format: {"hooks": [{"command": "...claude_status.py..."}]}
                for inner in h.get("hooks", []):
                    if is_legacy(inner.get("command", ""), hook_type):
                        is_pulse = True
                if not is_pulse:
                    filtered.append(h)
            if len(filtered) != len(hooks):
                settings.setdefault("hooks", {})[hook_type] = filtered
                changed = True

    # Remove empty hook types and hooks key if empty
    if "hooks" in settings:
        settings["hooks"] = {k: v for k, v in settings["hooks"].items() if v}
        if not settings["hooks"]:
            del settings["hooks"]
            changed = True

    if changed:
        _backup_settings(settings_path)
        _atomic_json_write(settings_path, settings)

    try:
        with _secure_open_write(marker) as f:
            pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Hook infrastructure — PostToolUse hook for live refresh
# ---------------------------------------------------------------------------

def _get_hook_state_path():
    return get_state_dir() / "hook_state.json"


def _read_hook_state():
    """The shared heartbeat snapshot, or None when missing, damaged or not an object."""
    _, data = _read_state_json(_get_hook_state_path())
    return data if isinstance(data, dict) else None


def _is_hook_state_fresh(hook_state, ttl=HOOK_STATE_FRESH_TTL):
    # The file is untrusted: valid JSON of the wrong shape (a list, a number) or
    # a stamp that is text, null, NaN or absurdly far ahead all read as stale
    # instead of raising, which would blank the bar on every repaint.
    if not isinstance(hook_state, dict) or not hook_state:
        return False
    refreshed = _cache_seconds(hook_state.get("last_refresh"))
    if refreshed is None:
        return False
    return abs(time.time() - refreshed) < ttl


def _format_elapsed(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds) // 60
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
    return f"{mins}m"


# What git says (branch, changed files, ahead/behind) is true of one repo
# folder, but every window shares this state dir. Each cache is therefore a
# small file holding one entry per repo folder, so a window never reads another
# window's repo, and a folder that is not a repo cannot hide a branch elsewhere.
GIT_BRANCH_FILE = "git_branch.json"
GIT_CACHE_MAX_REPOS = 16
# Claude Code cancels a repaint when the next update fires, so git on the
# render path must give up fast and leave the last known answer on screen.
GIT_RENDER_TIMEOUT = 1.5


def _git_repo_key():
    """The folder git runs in for this repaint, as a stable cache key."""
    try:
        return os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        return ""


def _read_git_cache(name, key):
    """Repo *key*'s entry in per-repo git cache *name*, or {} when unusable.

    The file is untrusted input: wrong JSON shape, bytes that are not UTF-8
    (a ValueError) or hand edits must read as "nothing cached", never raise.
    """
    try:
        with open(get_state_dir() / name, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    entry = repos.get(key) if isinstance(repos, dict) else None
    return entry if isinstance(entry, dict) else {}


def _cache_stamp(entry, field):
    """A time field of a git cache entry as finite seconds; 0.0 when unusable."""
    seconds = _cache_seconds(entry.get(field)) if isinstance(entry, dict) else None
    return seconds if seconds is not None else 0.0


def _git_cache_fresh(entry, ttl):
    """True when the answer, or an attempt to get one, is younger than *ttl*.

    A stamp in the future (clock change, damaged file) is never fresh, so it
    cannot pin an old answer on screen.
    """
    now = time.time()
    return any(0 <= now - stamp < ttl
               for stamp in (_cache_stamp(entry, "timestamp"), _cache_stamp(entry, "attempted"))
               if stamp)


def _cache_count(value):
    """A cached count as a non-negative int; 0 when unusable."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _write_git_cache(name, key, entry):
    """Store *entry* for repo *key*, keeping only the most recent few repos.

    Best effort: a cache that cannot be written just means git runs again.
    """
    try:
        path = get_state_dir() / name
        repos = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("repos"), dict):
                repos = data["repos"]
        except (OSError, ValueError, RecursionError):
            pass
        repos.pop(key, None)
        others = sorted(repos, key=lambda k: max(_cache_stamp(repos[k], "timestamp"),
                                                 _cache_stamp(repos[k], "attempted")),
                        reverse=True)[:GIT_CACHE_MAX_REPOS - 1]
        kept = {k: repos[k] for k in others}
        kept[key] = entry
        _atomic_json_write(path, {"repos": kept}, indent=None)
    except OSError:
        pass


def _get_git_branch():
    key = _git_repo_key()
    entry = _read_git_cache(GIT_BRANCH_FILE, key)
    cached_branch = entry.get("branch")
    if isinstance(cached_branch, str) and _git_cache_fresh(entry, GIT_BRANCH_CACHE_TTL):
        return cached_branch if cached_branch else None
    try:
        result = subprocess.run(
            [_GIT_PATH, "branch", "--show-current"],
            # git prints UTF-8 whatever the machine's code page is.
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3,
        )
        branch = result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        branch = ""
    _write_git_cache(GIT_BRANCH_FILE, key, {"branch": branch, "timestamp": time.time()})
    return branch if branch else None


# The heartbeat (tool count and elapsed time) describes one session. Each
# session keeps its own counters in hook_sessions/<session_id>.json beside
# hook_state.json, and hook_state.json carries a snapshot of whichever
# session's hook fired last, which is what the status line and the repaint
# timer read.
HOOK_SESSIONS_DIR = "hook_sessions"
HOOK_SESSION_MAX_AGE = 7 * 86400   # per-session files untouched this long are removed
HOOK_SESSION_PRUNE_BATCH = 50      # at most this many removals per hook call
HOOK_WRITE_ATTEMPTS = 3            # Windows refuses os.replace while a reader has the file open
# Only when a hook carries no session id: an idle gap this long starts a new count.
HEARTBEAT_IDLE_RESET = 3600


def _hook_sessions_dir():
    return _get_hook_state_path().parent / HOOK_SESSIONS_DIR


def _read_json_dict(path):
    """A JSON file's top-level dict; {} when missing, unreadable or another shape."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_session_heartbeat(session_id):
    """One session's heartbeat counters ({tool_count, session_start, ...}), or None."""
    sid = _safe_path_id(session_id)
    if not sid:
        return None
    return _read_json_dict(_hook_sessions_dir() / f"{sid}.json") or None


def _write_hook_state(path, data):
    """Atomic write that rides out a reader briefly holding the file (Windows)."""
    for attempt in range(HOOK_WRITE_ATTEMPTS):
        try:
            _atomic_json_write(path, data, indent=None)
            return
        except PermissionError:
            if attempt + 1 >= HOOK_WRITE_ATTEMPTS:
                raise
            time.sleep(0.02 * (attempt + 1))


def _prune_old_json(folder, max_age, limit, now):
    """Remove up to *limit* ``*.json`` files in *folder* older than *max_age*.

    Judged by file age alone, so nothing is opened or parsed, and bounded, so a
    backlog is cleared over several calls instead of stalling one repaint.
    """
    removed = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if removed >= limit:
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    if now - entry.stat().st_mtime > max_age:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        pass


def _beat_start(record, now):
    """A record's session_start if it is a sane past time, else *now*."""
    started = _cache_seconds(record.get("session_start"))
    return started if started and started <= now else now


def _read_stdin_text(limit):
    """Read up to ``limit`` bytes of stdin and decode them as UTF-8.

    Claude Code pipes UTF-8 JSON. A text-mode stdin on Windows decodes a pipe
    with the ANSI code page instead and keeps the bytes that page leaves
    undefined as lone surrogates: an accented name comes out garbled, and a
    Cyrillic, CJK or emoji one later raises UnicodeEncodeError and blanks the
    bar. So read the raw bytes and decode them here, replacing anything
    invalid. A leading byte-order mark is dropped. A stdin without a byte
    stream (or one that does not return bytes) falls back to the text read.
    """
    stream = getattr(sys.stdin, "buffer", None)
    if stream is not None:
        data = stream.read(limit)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data).decode("utf-8-sig", "replace")
    return sys.stdin.read(limit)


def _strip_surrogates(text):
    """``text`` with any lone surrogate replaced by ``?``.

    Versions up to 3.6.2 saved those in stdin_ctx.json; one of them kept in
    the line makes the final UTF-8 encode raise and blanks the bar.
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def hook_refresh(tool_name_arg):
    """Handle --hook-refresh: update hook state silently (no stdout).

    Tool name comes from stdin JSON (PostToolUse event data), not env vars.
    Falls back to tool_name_arg if stdin is unavailable. The session id comes
    from the same JSON, so each session counts its own tools and time.
    """
    # Parse tool name from stdin JSON (best practice per Claude Code docs)
    tool_name = tool_name_arg or "unknown"
    session_id = None
    if not sys.stdin.isatty():
        try:
            raw = _read_stdin_text(65536)
            if raw.strip():
                data = json.loads(raw)
                if isinstance(data, dict):
                    tool_name = _sanitize(data.get("tool_name", tool_name))
                    session_id = _safe_path_id(data.get("session_id")) or None
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    hook_state_path = _get_hook_state_path()
    now = time.time()
    if session_id:
        session_path = _hook_sessions_dir() / f"{session_id}.json"
        mine = _read_json_dict(session_path)
        state = {
            "tool_count": _cache_count(mine.get("tool_count")) + 1,
            "session_start": _beat_start(mine, now),
            "session_id": session_id,
        }
    else:
        session_path = None
        previous = _read_json_dict(hook_state_path)
        idle = now - _cache_stamp(previous, "last_refresh")
        if previous.get("session_id") or idle > HEARTBEAT_IDLE_RESET:
            previous = {}  # another session's snapshot, or long idle: start over
        state = {
            "tool_count": _cache_count(previous.get("tool_count")) + 1,
            "session_start": _beat_start(previous, now),
        }
    state["last_tool"] = _sanitize(str(tool_name))[:50]
    state["last_refresh"] = now
    if session_path is not None:
        try:
            _secure_mkdir(session_path.parent)
            if not mine:
                _prune_old_json(session_path.parent, HOOK_SESSION_MAX_AGE,
                                HOOK_SESSION_PRUNE_BATCH, now)
            _write_hook_state(session_path, state)
        except OSError:
            pass
    try:
        _write_hook_state(hook_state_path, state)
    except OSError:
        pass

    # The heartbeat becoming live (or going idle) changes whether the status
    # line needs a repaint timer, and this hook fires at exactly those moments.
    # Must run AFTER the state write above: the sync decides by reading the
    # persisted state, so syncing first would judge a just-woken heartbeat by
    # its stale on-disk timestamp and leave the timer unarmed. It is a no-op
    # when the answer is unchanged, so this costs one small read per tool call.
    try:
        sync_status_line_refresh()
    except Exception:
        pass


# Characters an unquoted interpreter path may hold and still be one plain
# command word in sh, Git Bash, pwsh 7 and Windows PowerShell 5.1 alike.
_SHELL_NEUTRAL_WORD = re.compile(r"[A-Za-z0-9/][A-Za-z0-9_./:~-]*")


def _blocking_hook_command(flag, python_cmd, script_path):
    """Return the command installed for a hook; every hook uses it.

    The name is from when only the hooks whose exit code can steer the team
    (SubagentStop and the three team events) were built this way. Now all of
    them are, so a moved or deleted install never makes Claude Code report a
    hook error after a tool call either.

    Claude Code runs a hook's command through sh, or on Windows through Git
    Bash — or PowerShell when Git Bash isn't installed. A ``$HOME/...``
    interpreter is a ParserError in both PowerShells, so there it would never
    run. An unquoted forward-slash absolute interpreter plus a
    single-quoted script path parses the same in all of them (checked in Git
    Bash, pwsh 7 and PowerShell 5.1, which also pass the payload through on
    stdin). A *quoted* interpreter cannot: PowerShell needs ``& "..."`` to
    run one and bash reads ``&`` as "background". So an interpreter path
    with a space (or anything else unsafe unquoted) falls back to the
    bash-only form, with ``python_cmd`` as the caller wrote it for its shell.

    ``; exit 0`` pins the status whichever shell runs it: Python exits 2 by
    itself when the script path is missing (a moved or deleted install), and
    for the blocking events exit 2 would delete every task the team creates,
    block every completion, or keep teammates from ever going idle.
    """
    python = Path(sys.executable).as_posix()
    script = Path(__file__).resolve().as_posix()
    if _SHELL_NEUTRAL_WORD.fullmatch(python) and "'" not in script:
        return f"{python} '{script}' {flag}; exit 0"
    return f'{python_cmd} "{script_path}" {flag}; exit 0'


# Hook groups, installable on their own (`--install-hooks --only agents,team`,
# and the setup wizard's hooks screen). Each entry is (event, flag, blocking):
# a blocking event is one whose exit code 2 steers Claude Code, and its entry
# takes no matcher. Every command, blocking or not, is pinned to exit 0 by
# _blocking_hook_command.
_HOOK_GROUPS = {
    # PostToolUse drives the heartbeat spinner, tool counter and last-tool
    # widget. It runs Python after every tool call, so it is opt-in.
    "heartbeat": (("PostToolUse", "--hook-refresh", False),),
    # SubagentStart / SubagentStop drive the live agent counter. They carry
    # agent_id and agent_type, so no polling is needed. SubagentStop is
    # blocking: exit 2 keeps the subagent running (hooks reference), and Python
    # exits 2 by itself when the script path has gone missing.
    "agents": (("SubagentStart", "--hook-subagent-start", False),
               ("SubagentStop", "--hook-subagent-stop", True)),
    # The three agent-team events drive the team segment. They take no matcher
    # (Claude Code silently ignores one), so their entries omit the key. Exit 2
    # keeps a teammate from going idle, deletes a new task, or refuses a
    # completion — all three are blocking.
    "team": (("TeammateIdle", "--hook-teammate-idle", True),
             ("TaskCreated", "--hook-task-created", True),
             ("TaskCompleted", "--hook-task-completed", True)),
}
HOOK_GROUP_NAMES = tuple(_HOOK_GROUPS)


def _parse_hook_groups(raw):
    """Parse ``heartbeat,agents,team`` (or ``all``) into a set of group names.

    Raises ValueError naming the first unknown group, so a typo never silently
    installs nothing.
    """
    names = [p.strip().lower() for p in str(raw or "").split(",") if p.strip()]
    if not names:
        raise ValueError("no hook group given")
    if "all" in names:
        return set(HOOK_GROUP_NAMES)
    for name in names:
        if name not in _HOOK_GROUPS:
            raise ValueError(f"unknown hook group: {_sanitize(name)}")
    return set(names)


def _is_pulse_command(hook, flag):
    """True when one inner ``{type, command}`` hook is ours for ``flag``."""
    if not isinstance(hook, dict):
        return False
    cmd = str(hook.get("command", ""))
    return "claude_status.py" in cmd and flag in cmd


def _is_pulse_hook_entry(entry, flag):
    """True when a settings.json hook entry carries our command for ``flag``."""
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    return any(_is_pulse_command(h, flag) for h in inner)


def _without_pulse_command(entry, flag):
    """``entry`` minus our command for ``flag``; None when nothing is left.

    Someone may have merged our command into an entry next to their own by
    hand. Only ours goes; the entry's matcher and other commands stay.
    """
    if not _is_pulse_hook_entry(entry, flag):
        return entry
    inner = [h for h in entry["hooks"] if not _is_pulse_command(h, flag)]
    if not inner:
        return None
    kept = dict(entry)
    kept["hooks"] = inner
    return kept


def _installed_pulse_hooks(settings):
    """Return the hook groups usage-pulse has entries for in ``settings``.

    A group counts as installed when any of its events carries one of our
    entries; re-installing it repairs a partial set.
    """
    found = set()
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return found
    for group, events in _HOOK_GROUPS.items():
        for event, flag, _blocking in events:
            entries = hooks.get(event)
            if isinstance(entries, list) and any(_is_pulse_hook_entry(e, flag) for e in entries):
                found.add(group)
                break
    return found


def _load_settings_for_write(settings_path):
    """Read settings.json for a read-modify-write.

    Returns ``(settings, None)``, with ``{}`` for a missing file, or
    ``(None, message)`` when the file exists but cannot be used. Writing over
    an unreadable or invalid file would throw away the user's whole Claude
    Code configuration, so callers must stop on an error.
    """
    if not settings_path.exists():
        return {}, None
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except json.JSONDecodeError:
        return None, f"{settings_path} contains invalid JSON. Fix or remove it, then run this again."
    except (OSError, UnicodeDecodeError) as e:
        return None, f"Cannot read {settings_path}: {e}"
    if not isinstance(settings, dict):
        return None, f"{settings_path} is not a JSON object. Fix or remove it, then run this again."
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None, f'"hooks" in {settings_path} is not an object. Fix it, then run this again.'
    return settings, None


_SETTINGS_BACKUP_KEEP = 5
_settings_backed_up = set()


def _backup_settings(settings_path):
    """Keep a copy of settings.json before usage-pulse first writes it.

    The copy goes to ``<state dir>/settings-backups/<config dir name>-settings.json.<stamp>``
    so a bad install or a wrong ``CLAUDE_CONFIG_DIR`` can be undone by hand.
    One backup per file per process; the newest ``_SETTINGS_BACKUP_KEEP`` are
    kept. Returns the backup path, or None when there was nothing to copy or
    the copy failed (a failed backup never blocks the install).
    """
    settings_path = Path(settings_path)
    key = str(settings_path)
    if key in _settings_backed_up or not settings_path.is_file():
        return None
    try:
        backup_dir = get_state_dir() / "settings-backups"
        _secure_mkdir(backup_dir)
        prefix = (_sanitize(settings_path.parent.name) or "claude")[:32]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"{prefix}-settings.json.{stamp}"
        n = 0
        while dest.exists():
            n += 1
            dest = backup_dir / f"{prefix}-settings.json.{stamp}-{n}"
        shutil.copy2(str(settings_path), str(dest))
        _settings_backed_up.add(key)
        olds = sorted(backup_dir.glob(f"{prefix}-settings.json.*"),
                      key=lambda q: (q.stat().st_mtime, q.name))
        for old in olds[:-_SETTINGS_BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest
    except (OSError, shutil.Error):
        return None


def _strip_pulse_hooks(hooks, groups):
    """Remove our entries for ``groups`` from a settings ``hooks`` dict in place.

    Foreign entries on the same events are kept. An event list that only held
    our entry is dropped rather than left behind as ``[]``.
    """
    for group in groups:
        for event, flag, _blocking in _HOOK_GROUPS[group]:
            entries = hooks.get(event)
            if not isinstance(entries, list):
                continue
            kept = [k for k in (_without_pulse_command(e, flag) for e in entries)
                    if k is not None]
            if kept:
                hooks[event] = kept
            elif event in hooks:
                del hooks[event]


def install_hooks(groups=None):
    """Install usage-pulse's hooks into Claude Code's settings.json.

    ``groups`` is any subset of ``HOOK_GROUP_NAMES`` (``heartbeat``,
    ``agents``, ``team``); ``None`` installs all three, which is what
    ``--install-hooks`` has always done. Only the chosen groups' events are
    touched: hooks from other groups, ours or anyone else's, are left as they
    are. Re-running is idempotent — our old entry for each event is replaced,
    never duplicated.

    Returns True when settings.json was written, False when it could not be
    (invalid JSON, unreadable file) — nothing is written in that case.
    """
    if groups is None:
        groups = set(HOOK_GROUP_NAMES)
    else:
        groups = set(groups)
        unknown = groups - set(HOOK_GROUP_NAMES)
        if unknown:
            raise ValueError(f"unknown hook group(s): {', '.join(sorted(unknown))}")
    settings_path = _claude_config_dir() / "settings.json"
    script_path = _win_portable_path(Path(__file__).resolve())
    python_cmd = _get_python_cmd()
    settings, error = _load_settings_for_write(settings_path)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return False

    hooks = settings.setdefault("hooks", {})
    commands = {}
    for group in HOOK_GROUP_NAMES:          # fixed order keeps the file stable
        if group not in groups:
            continue
        for event, flag, blocking in _HOOK_GROUPS[group]:
            # Every hook command is pinned to exit 0, the non-blocking ones too:
            # when the install is moved or deleted Python exits 2 by itself, and
            # Claude Code would show a hook error after each tool call.
            command = _blocking_hook_command(flag, _shell_python(python_cmd, settings),
                                             script_path)
            commands[event] = command
            existing = hooks.get(event)
            if not isinstance(existing, list):
                existing = []
            kept = [k for k in (_without_pulse_command(h, flag) for h in existing)
                    if k is not None]
            if blocking:
                kept.append({"hooks": [{"type": "command", "command": command}]})
            else:
                kept.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
            hooks[event] = kept

    settings["hooks"] = hooks
    _secure_mkdir(settings_path.parent)
    _backup_settings(settings_path)
    _atomic_json_write(settings_path, settings)
    names = ", ".join(g for g in HOOK_GROUP_NAMES if g in groups)
    utf8_print(f"{GREEN}Installed usage-pulse hooks ({names}) to {settings_path}{RESET}")
    if "heartbeat" in groups:
        utf8_print(f"  Command: {commands['PostToolUse']}")
    utf8_print(f"")
    utf8_print(f"This enables:")
    if "heartbeat" in groups:
        utf8_print(f"  {BOLD}Heartbeat{RESET}    — live spinner with tool count & elapsed time")
        utf8_print(f"  {BOLD}Last tool{RESET}    — shows last tool used (opt-in: --show last_tool)")
        utf8_print(f"  {BOLD}Git branch{RESET}   — current branch name")
    if "agents" in groups:
        utf8_print(f"  {BOLD}Live agents{RESET}  — subagents running now and spawned this session ({DIM}agents 3 live · 47/200{RESET})")
    if "team" in groups:
        utf8_print(f"  {BOLD}Agent team{RESET}   — teammates seen and shared tasks done ({DIM}team 3 · tasks 4/7{RESET})")
    utf8_print(f"")
    utf8_print(f"Restart Claude Code for hooks to take effect.")
    return True


def remove_hooks(groups):
    """Remove usage-pulse's entries for ``groups`` from settings.json.

    Used by the setup wizard when a hook group is unticked. Other tools' hooks
    on the same events are kept. Returns True when settings.json was written
    (or there was nothing to remove), False when it could not be read.
    """
    groups = set(groups or ())
    unknown = groups - set(HOOK_GROUP_NAMES)
    if unknown:
        raise ValueError(f"unknown hook group(s): {', '.join(sorted(unknown))}")
    settings_path = _claude_config_dir() / "settings.json"
    settings, error = _load_settings_for_write(settings_path)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return False
    if not groups or not (groups & _installed_pulse_hooks(settings)):
        return True
    hooks = settings.get("hooks", {})
    _strip_pulse_hooks(hooks, groups)
    settings["hooks"] = hooks
    _backup_settings(settings_path)
    _atomic_json_write(settings_path, settings)
    names = ", ".join(g for g in HOOK_GROUP_NAMES if g in groups)
    utf8_print(f"Removed usage-pulse hooks ({names}) from {settings_path}")
    return True


# ---------------------------------------------------------------------------
# Cache — stores usage data alongside the rendered line so rainbow can
# re-render each call without re-hitting the API.
# ---------------------------------------------------------------------------

def get_state_dir():
    """Return the shared state/cache directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    state_dir = base / "claude-status"
    _secure_mkdir(state_dir)
    return state_dir


def get_account_state_dir():
    """Per-account state dir for files that describe one Claude account.

    The default account (``~/.claude``) keeps the shared dir, so nothing moves
    for anyone with one account. Another ``CLAUDE_CONFIG_DIR`` gets
    ``<state>/accounts/<name>-<sha1[:8]>`` so two accounts' windows stop
    overwriting each other's usage cache, stdin context, history and sessions.
    """
    base = get_state_dir()
    try:
        config_dir = _claude_config_dir()
        key = os.path.normcase(str(Path(config_dir).expanduser().resolve()))
        default = os.path.normcase(str((Path.home() / ".claude").resolve()))
    except (OSError, RuntimeError, ValueError):
        return base
    if key == default:
        return base
    name = _safe_path_id(Path(key).name, 32) or "account"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    account_dir = base / "accounts" / f"{name}-{digest}"
    try:
        _secure_mkdir(account_dir)
    except OSError:
        return base
    return account_dir


def get_cache_path():
    return get_account_state_dir() / "cache.json"




# ---------------------------------------------------------------------------
# Update checker — compares local git HEAD to GitHub remote (cached 1 hour)
# ---------------------------------------------------------------------------

_UPDATE_CHECK_BUDGET = 6  # seconds; hard ceiling on one update check
UPDATE_CHECK_TTL = 3600  # check at most once per hour
GITHUB_REPO = "NoobyGains/usage-pulse"
# The project was called claude-pulse until 3.5.0; clones from before still
# name that in their origin, and GitHub redirects the old name.
GITHUB_REPO_ALIASES = (GITHUB_REPO.lower(), "noobygains/claude-pulse")
_GIT_PATH = shutil.which("git") or "git"  # resolve once at import time
_CLAUDE_PATH = shutil.which("claude")  # resolve once at import time


def _detect_status_bar_conflict():
    """Detect conditions where Claude Code's status bar is too crowded for pulse.

    When Claude Code migrates from npm to native installer, it shows a long
    notification that fills the entire status bar area, causing pulse output
    to wrap.  Suppress output until the user resolves the migration.

    Recent npm versions also show this notification to encourage migration,
    so we check for the npm package presence regardless of which binary
    ``shutil.which("claude")`` resolves to (the npm shim may shadow the
    native binary on PATH).
    """
    try:
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            if not appdata:
                return False
            npm_pkg = os.path.join(appdata, "npm", "node_modules",
                                   "@anthropic-ai", "claude-code")
            return os.path.isdir(npm_pkg)
        elif sys.platform == "darwin":
            for prefix in ("/usr/local", "/opt/homebrew"):
                npm_pkg = os.path.join(prefix, "lib", "node_modules",
                                       "@anthropic-ai", "claude-code")
                if os.path.isdir(npm_pkg):
                    return True
    except Exception:
        pass
    return False


def _capped_timeout(default, deadline):
    """Cap *default* to the seconds left before *deadline* (an epoch).

    Returns None when the budget is spent, so callers can skip a blocking
    operation entirely instead of starting one they have no time for. This is
    what makes _UPDATE_CHECK_BUDGET a real ceiling: checking the deadline only
    *between* operations still lets each one run its full fixed timeout, and
    the sum of those (~13s) is far past the advertised budget.
    """
    if deadline is None:
        return default
    remaining = deadline - time.time()
    if remaining < 0.05:
        return None
    return min(default, remaining)


# ---------------------------------------------------------------------------
# Render-path networking — a fetch may fail, but it may never hold up the bar
# ---------------------------------------------------------------------------
#
# Claude Code cancels an in-flight status line script whenever the next update
# fires, and with `refreshInterval` set that is every N seconds (--install
# writes 2 while animation is on). A repaint that blocks on the network past
# that is killed before it prints — and, left to itself, before it can record
# that it tried, so the next repaint walks into the same stall and the bar
# never renders again. Seen in the wild on a network that advertises IPv6 but
# black-holes it: registry.npmjs.org publishes 12 AAAA + 12 A records and
# urllib's timeout applies *per address*, so a "3 second" check took 36.

_NET_FETCH_BUDGET = 1.5  # seconds; hard wall-clock ceiling on one fetch
_NET_CONNECT_ATTEMPT = 0.5  # seconds; cap on each attempt of the quick pass
_NET_QUICK_CANDIDATES = 4  # addresses the quick pass tries (two per family)
_NET_PATIENT_ATTEMPT = 3.0  # seconds; cap on each attempt of the patient pass
_NET_RETRY_BACKOFF = 600  # seconds before a failed or cut-short check retries
_USAGE_KILL_GUARD = 60  # seconds a cut-short usage fetch keeps the next one away
_NET_MIN_SPACING = 5  # seconds between optional checks: one per repaint, not a stack


_own_stamps = {}  # key -> the stamp value this process wrote


def _stamp_path(key):
    """Where *key*'s stamp lives.

    "usage" guards one account's fetch, with that account's token, feeding
    that account's cache, so it sits beside get_cache_path(): another
    account's cut-short fetch must not hold this one's off. The default
    account's dir is the shared one, so its stamp has not moved. Every other
    key, "_last" above all, is about this machine's network and stays shared:
    the turn-taking is per machine on purpose.
    """
    base = get_account_state_dir() if key == "usage" else get_state_dir()
    return base / ("net_attempt_%s.stamp" % re.sub(r"[^A-Za-z0-9_-]", "_", key))


def _read_stamp(key):
    try:
        with open(_stamp_path(key), "r", encoding="utf-8") as f:
            return float(f.read(64).strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _write_stamp(key, value):
    """Write one stamp, in a file of its own.

    Sessions share the state dir. With every stamp in one JSON file, a session
    that read the file before another wrote to it would write the other's
    stamp back out of existence. One file per key leaves nothing to clobber;
    the temp name carries the pid so two writers can't collide on it either.
    """
    path = _stamp_path(key)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with _secure_open_write(tmp) as f:
            f.write(repr(float(value)))
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _begin_net_attempt(key, backoff=None, primary=False):
    """Record that *key* is about to touch the network. False means: don't.

    The stamp is written BEFORE the network call on purpose. A repaint that
    Claude Code kills mid-fetch gets no chance to note the failure afterwards,
    so the note has to already be on disk; the next repaint then sees a recent
    attempt and renders straight away instead of repeating the stall.

    Each check fits inside a refresh interval, but two back to back do not, so
    the optional ones also take turns: after any attempt the rest wait
    _NET_MIN_SPACING for a later repaint. Waiting is not failing — it leaves no
    stamp. *primary* (the usage fetch: the bar itself) never waits.
    """
    now = time.time()
    if backoff is None:
        backoff = _NET_RETRY_BACKOFF
    if 0 <= now - _read_stamp(key) < backoff:
        return False
    if not primary and 0 <= now - _read_stamp("_last") < _NET_MIN_SPACING:
        return False
    try:
        _write_stamp(key, now)
    except OSError:
        # No stamp means a kill during this fetch would leave no trace and
        # loop forever. Skipping one check is the cheaper failure.
        return False
    _own_stamps[key] = float(now)
    try:
        _write_stamp("_last", now)
    except OSError:
        pass  # only the turn-taking; the kill guard above is what matters
    return True


def _end_net_attempt(key):
    """Lift this process's stamp for *key*: the call came back, one way or another.

    For callers that already remember their own failures and only need the
    stamp to survive a kill. Only a stamp this process wrote is lifted — by
    the time a slow request returns, another session may have armed its own.
    Best effort: a stamp left behind just means one backoff window of cache.
    """
    mine = _own_stamps.pop(key, None)
    if mine is None or _read_stamp(key) != mine:
        return
    try:
        _stamp_path(key).unlink()
    except OSError:
        pass


def _interleave_families(infos):
    """Alternate address families, keeping the resolver's order within each.

    The resolver hands back every IPv6 address before the first IPv4 one, so
    trying them in order pays one timeout per address of a dead family before
    reaching a live one. Alternating (RFC 8305 §4) caps that at one attempt.
    """
    queues = {}
    for info in infos:
        queues.setdefault(info[0], []).append(info)
    ordered = []
    pending = list(queues.values())
    while pending:
        for queue in pending:
            ordered.append(queue.pop(0))
        pending = [queue for queue in pending if queue]
    return ordered


def _bounded_connector(deadline):
    """Build a drop-in for ``socket.create_connection`` that honours *deadline*.

    ``create_connection`` applies its timeout to each address in turn, so the
    real ceiling is timeout x address count. Here the whole connect stops at
    *deadline*, in two passes:

    quick    a few interleaved addresses, a short cap each — steps past a dead
             family in half a second instead of a full timeout per address.
    patient  only if that found nothing and the caller allowed more time:
             every address again with a generous cap, so a link that is merely
             slow still connects. IPv4 leads this pass — by now something is
             wrong, and a black-holed IPv6 is the usual culprit.

    The DNS lookup is held to *deadline* too. ``getaddrinfo`` has no timeout
    of its own, and a VPN whose DNS servers have gone away can hang it for
    many seconds, so with a deadline it runs on a daemon thread that is simply
    abandoned when the time is up: socket.timeout, an OSError, which urllib
    reports as a URLError like any failed connect. No socket exists yet to
    leak, and a daemon thread can't keep the process alive. Without a
    deadline the lookup runs right here, as in ``create_connection``.

    *timeout* is read the way ``create_connection`` reads it, and becomes the
    socket's timeout for everything after the connect (the TLS handshake and
    the response): a number is the timeout per operation, None blocks, and
    the default sentinel leaves the process default
    (``socket.getdefaulttimeout()``). With a deadline, whichever of those
    applies is capped once, when the connect succeeds, at the time left, so
    even "no timeout" never leaves one operation blocked for good. That cap
    is per operation, not a wall-clock limit: the handshake, each send and
    each read get it afresh, so a slow peer can still carry the whole
    request past *deadline*. The connect attempts themselves always use the
    caps above.

    The hard wall-clock ceiling lives elsewhere: ``_fetch_bounded`` gives
    the optional fetches one. The token requests through ``_safe_opener``
    have none, and for the primary usage fetch that is deliberate: a hard
    cap there could starve the per-model bars (Fable and the rest) on a slow
    but working network. A repaint cut short mid-fetch is caught by the
    write-ahead stamp (``_begin_net_attempt``) instead.
    """
    def resolve(host, port):
        if deadline is None:
            return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        budget = _capped_timeout(float("inf"), deadline)
        if budget is None:
            raise socket.timeout("no time left to look up %s" % host)
        answer = []

        def lookup():
            try:
                answer.append((True, socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)))
            except BaseException as exc:  # handed to the caller below
                answer.append((False, exc))

        worker = threading.Thread(target=lookup, name="usage-pulse-dns", daemon=True)
        try:
            worker.start()
        except RuntimeError as exc:  # no thread to be had: fail the fetch, don't hang it
            raise OSError("could not look up %s: %s" % (host, exc)) from None
        worker.join(budget)
        if not answer:
            raise socket.timeout("looking up %s took too long" % host)
        found, value = answer[0]
        if found:
            return value
        raise value

    def after_connect(timeout):
        """The socket timeout for the TLS handshake and the response."""
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
            timeout = socket.getdefaulttimeout()
        if deadline is None:
            return timeout
        left = _capped_timeout(float("inf"), deadline)
        if left is None:
            raise socket.timeout("connect budget exhausted")
        return left if timeout is None else min(timeout, left)

    def attempt(info, cap, timeout, source_address):
        family, socktype, proto, _canon, sockaddr = info
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(cap)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            # From here each operation (handshake, send, read) may block for at
            # most the time the deadline had left at connect, not the remainder.
            sock.settimeout(after_connect(timeout))
        except BaseException:
            sock.close()
            raise
        return sock

    def connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None,
                **_kwargs):
        host, port = address
        infos = _interleave_families(resolve(host, port))
        passes = (
            (infos[:_NET_QUICK_CANDIDATES], _NET_CONNECT_ATTEMPT),
            (sorted(infos, key=lambda info: info[0] != socket.AF_INET),
             _NET_PATIENT_ATTEMPT),
        )
        last_error = None
        for candidates, cap in passes:
            for info in candidates:
                capped = _capped_timeout(cap, deadline)
                if capped is None:
                    raise last_error or OSError("connect budget exhausted")
                try:
                    return attempt(info, capped, timeout, source_address)
                except OSError as exc:
                    last_error = exc
        raise last_error or OSError("no addresses to connect to")
    return connect


if hasattr(urllib.request, "HTTPSHandler"):
    class _BoundedHTTPSHandler(urllib.request.HTTPSHandler):
        """HTTPS whose TCP connect goes through ``_bounded_connector``.

        Only the connect is replaced. ``https_open`` is the stock one, so the
        connection gets exactly the TLS settings a stock handler gives it on
        this Python: on 3.12+ the context the handler built when it was made
        (so ``_safe_opener``, made at import, keeps the trust it started with,
        and a later change to ``ssl._create_default_https_context`` can't
        reach it); on 3.8-3.11 context and check_hostname as given, which by
        default leaves the connection to build a default context, as stock
        does there. Certificate verification, hostname checks and proxy
        tunnelling stay exactly as the standard library does them.
        """

        def __init__(self, deadline=None):
            super().__init__()
            self._deadline = deadline

        def do_open(self, http_class, req, **http_conn_args):
            deadline = self._deadline
            if deadline is None and isinstance(req.timeout, (int, float)):
                deadline = time.time() + req.timeout  # a shared opener: per request
            connect = _bounded_connector(deadline)

            def connection(host, **kwargs):
                conn = http_class(host, **kwargs)
                conn._create_connection = connect  # the seam http.client connects through
                return conn

            return super().do_open(connection, req, **http_conn_args)
else:
    _BoundedHTTPSHandler = None  # Python built without ssl: urllib has no HTTPS at all


def _open_and_read(req, max_bytes, deadline):
    """Fetch *req* and return up to *max_bytes* of the body. Raises on failure.

    An error status comes out as urllib's HTTPError, which is also the still
    open response: the caller reads it or closes it. Without ssl there is no
    HTTPS handler to bound; the plain opener then fails an https URL as a
    URLError, as 3.6.0 did, rather than the module failing to load.
    """
    timeout = _capped_timeout(_NET_FETCH_BUDGET, deadline)
    if timeout is None:
        return None
    if _BoundedHTTPSHandler is not None:
        opener = urllib.request.build_opener(_BoundedHTTPSHandler(deadline))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(max_bytes)


def _fetch_bounded(req, max_bytes, budget=None):
    """Fetch *req*, returning up to *max_bytes* of the body, or None on failure.

    Never takes longer than *budget* seconds of wall-clock time, whatever the
    network does. Socket timeouts alone can't promise that: ``getaddrinfo``
    has no timeout at all, and a VPN whose DNS servers have gone away can hang
    it for many seconds. So the fetch runs on a daemon thread and is simply
    abandoned at the ceiling — the process exits moments later and takes the
    thread with it.

    An error answer is closed on that thread, even one that lands after the
    ceiling: nothing reads it, and left to garbage collection it would hold
    its socket until then. A body that is read is closed by
    ``_open_and_read`` itself.
    """
    if budget is None:
        budget = _NET_FETCH_BUDGET
    deadline = time.time() + budget
    result = []

    def work():
        try:
            result.append(_open_and_read(req, max_bytes, deadline))
        except urllib.error.HTTPError as exc:
            # "No answer", but still a live response with an unread body.
            try:
                exc.close()
            except Exception:
                pass  # 3.8/3.9: one made without a body has nothing to close
        except Exception:
            pass  # any other failure is simply "no answer"

    worker = threading.Thread(target=work, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        return None
    worker.join(budget)
    return result[0] if result else None


def get_local_commit(deadline=None):
    """Get the local git HEAD commit hash (short). Returns None on failure."""
    timeout = _capped_timeout(2, deadline)
    if timeout is None:
        return None
    repo_dir = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            [_GIT_PATH, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _on_default_branch(deadline=None):
    """True when this checkout sits on main/master; False otherwise; None on error.

    A checkout parked on a topic or preview branch is a developer or a preview
    tester, and is typically *ahead* of upstream main — where "update
    available" is not just noise but actively wrong. Detached HEADs are a
    deliberate pin, not a plain tracking install, so they don't nag either.
    """
    timeout = _capped_timeout(2, deadline)
    if timeout is None:
        return None
    repo_dir = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            [_GIT_PATH, "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(repo_dir),
        )
        if result.returncode == 1:
            return False  # -q's documented "not a symbolic ref": detached HEAD
        if result.returncode != 0:
            return None  # broken repo/permissions — unknown, don't cache a verdict
        return result.stdout.strip() in ("main", "master")
    except Exception:
        return None


def get_remote_commit(deadline=None, leg=None):
    """Latest commit hash of upstream main. Returns None on failure.

    Asks the GitHub API first: one small request, no git process. The API
    allows only 60 unauthenticated requests an hour per address, and a
    machine running several tools can use them all, so a failed or odd API
    answer falls back to ``git ls-remote`` against origin, which GitHub serves
    without that limit. Callers verify that origin is the upstream repo first.

    That chain is for ``--update`` and ``--config``. Two network operations in
    a row don't fit in one repaint, so the status line asks for one *leg*
    instead: "api" or "git", each held to _NET_FETCH_BUDGET, and
    check_for_update runs the git leg on a later repaint.
    """
    if leg not in (None, "api", "git"):
        raise ValueError("leg must be None, 'api' or 'git', not %r" % (leg,))
    budget = _capped_timeout(_NET_FETCH_BUDGET, deadline)
    if budget is None:
        return None
    if leg == "git":
        return _git_remote_commit(deadline=time.time() + budget)
    sha = ""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/main"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github.sha",
            "User-Agent": "usage-pulse-update-checker",
        })
        body = _fetch_bounded(req, 1024, budget=budget)
        sha = body.decode().strip() if body is not None else ""
    except Exception:
        sha = ""
    if re.fullmatch(r'[0-9a-f]{40}', sha):
        return sha
    if leg == "api":
        return None  # the status line asks git on a later repaint
    # Not a SHA (rate-limited, error page, offline): ask git instead.
    return _git_remote_commit(deadline=deadline)


def _git_remote_commit(deadline=None):
    """``git ls-remote origin refs/heads/main`` as a 40-hex SHA, or None."""
    timeout = 8
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"   # never hang on a credential prompt
    # A timed-out call abandons git, but its HTTPS helper can outlive it, hung
    # on the remote. Have curl drop a transfer stuck under 1 byte/s for as long
    # as this call could wait anyway: that never cuts short an answer a caller
    # is still waiting for, and it ends the helper instead of leaving that to
    # the remote. (Connect and TLS fall under curl's own 300s connect timeout.)
    # Values already in the environment win.
    env.setdefault("GIT_HTTP_LOW_SPEED_LIMIT", "1")
    env.setdefault("GIT_HTTP_LOW_SPEED_TIME", str(timeout))
    out = _git("ls-remote", "origin", "refs/heads/main", timeout=timeout,
               deadline=deadline, env=env, network=True)
    if not out:
        return None
    sha = out.split()[0]
    return sha if re.fullmatch(r'[0-9a-f]{40}', sha) else None


def _git(*args, timeout=2, deadline=None, env=None, network=False):
    """Run git in the install dir, returning stripped stdout or None.

    The default timeout is deliberately short: these calls sit behind the
    hourly update check, and several run in series, so a slow filesystem or a
    hung git could otherwise stall a repaint for tens of seconds. *deadline*
    caps it further to whatever remains of the caller's overall budget.
    *env* replaces the child's environment (None inherits ours).

    *network* is for a call that talks to a remote. git hands that to a helper
    (git-remote-https, ssh) that inherits the child's stdio, and on a timeout
    subprocess.run kills only git; on Windows it then waits for end-of-file on
    pipes the helper still holds, for as long as the remote hangs. So such a
    call gets no pipes at all: stdout goes to a temp file, stdin and stderr to
    the null device, and the timeout is a real ceiling on every OS.
    """
    timeout = _capped_timeout(timeout, deadline)
    if timeout is None:
        return None
    repo_dir = Path(__file__).resolve().parent
    extra = {"env": env} if env is not None else {}
    try:
        if network:
            import tempfile  # only this path needs it
            with tempfile.TemporaryFile() as out:
                result = subprocess.run(
                    [_GIT_PATH, *args],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                    timeout=timeout, cwd=str(repo_dir), **extra,
                )
                if result.returncode != 0:
                    return None
                out.seek(0)
                return out.read(65536).decode("utf-8", "replace").strip()
        result = subprocess.run(
            [_GIT_PATH, *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(repo_dir), **extra,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _tracks_upstream(deadline=None):
    """True when this checkout's origin is the upstream usage-pulse repo.

    A fork's origin points somewhere else, and its own commits are never on
    upstream's main, so the plain ``local != remote`` comparison flagged an
    update forever. Returns None when origin can't be read at all.
    """
    url = _git("remote", "get-url", "origin", timeout=3, deadline=deadline)
    if url is None:
        return None
    normalised = url.lower().rstrip("/")
    if normalised.endswith(".git"):
        normalised = normalised[:-4]
    # Matches both git@github.com:owner/repo and https://github.com/owner/repo
    return any(normalised.endswith(alias) for alias in GITHUB_REPO_ALIASES)


def _remote_is_ancestor(remote, deadline=None):
    """True when *remote* is already contained in local history (we're ahead).

    Returns None when the commit isn't present locally, so ancestry can't be
    decided without fetching — which the status line must never do.
    """
    if _git("cat-file", "-e", f"{remote}^{{commit}}", timeout=3,
            deadline=deadline) is None:
        return None
    timeout = _capped_timeout(2, deadline)
    if timeout is None:
        return None
    repo_dir = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            [_GIT_PATH, "merge-base", "--is-ancestor", remote, "HEAD"],
            capture_output=True, text=True, timeout=timeout, cwd=str(repo_dir),
        )
    except Exception:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None  # any other status means git could not answer


def _cache_seconds(value):
    """A cache file's time field as finite seconds, or None when unusable.

    The file is untrusted input. Besides text and null, valid JSON can hold an
    int too big for a float (float() raises OverflowError, which is not a
    ValueError), Infinity, whose arithmetic reads as fresh (or as an active
    backoff) forever, or NaN, which compares false to everything and crashes
    the staleness label (int() of NaN raises ValueError).
    """
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) else None


def _cache_age(cached):
    """Age in seconds of a cache dict, or +inf when its timestamp is unusable.

    Returning infinity makes a corrupt entry look infinitely stale, so callers
    naturally fall through to a fresh check instead of raising on the subtraction,
    and the staleness label (when shown) marks it red.
    """
    try:
        timestamp = _cache_seconds(cached.get("timestamp", 0))
    except AttributeError:
        return float("inf")
    return float("inf") if timestamp is None else time.time() - timestamp


def _is_plugin_install():
    """True when this script runs from a marketplace plugin's version folder.

    Claude Code updates those itself, and the folder has no ``.git``, so
    asking git there finds nothing or, worse, finds a repo further up (a
    dotfiles-managed ``~/.claude``). A developer's checkout that merely sits in
    a folder named like the plugin has a ``.git`` beside the script and is not
    a plugin install.
    """
    try:
        here = Path(__file__).resolve()
    except (OSError, RuntimeError):
        here = Path(__file__)
    return _is_plugin_cache_path(here) and not (here.parent / ".git").exists()


def _cache_quiet_update_verdict(update_cache, script_mtime, reason, local=None):
    """Remember "nothing to nag about" so the next repaints don't ask git again.

    Written like the positive verdicts, keyed on the script's mtime and the
    hour's TTL, with the *reason* beside it ("no_git", "fork").
    """
    entry = {
        "timestamp": time.time(),
        "update_available": False,
        "reason": reason,
        "script_mtime": script_mtime,
    }
    if local:
        entry["local"] = local[:8]
    try:
        with _secure_open_write(update_cache) as f:
            json.dump(entry, f)
    except OSError:
        pass


def check_for_update(*, interactive=False):
    """Check if a newer version is available on GitHub. Returns True/False/None.

    Cached for 1 hour. Fully silent on any error — never blocks the status line.

    *interactive* is for a command the user runs (``--config``): nobody cancels
    it mid-check, so it skips the status line's turn-taking and backoff and
    runs the full API-then-git chain inside _UPDATE_CHECK_BUDGET. Its verdict
    lands in the same cache. Keyword-only, so a stray positional True can't
    skip the status line's gates.
    """
    if _is_plugin_install():
        return None  # Claude Code updates plugins; nothing here to compare
    state_dir = get_state_dir()
    update_cache = state_dir / "update_check.json"

    # `git rev-parse HEAD` costs ~20ms, and it was previously run on every
    # repaint purely to validate an hourly cache. Instead, stamp the cache with
    # the script's mtime: if the file hasn't changed since the cache was
    # written, the checkout hasn't moved either, so the cached verdict stands
    # and git never runs. A pull rewrites claude_status.py and invalidates it.
    try:
        script_mtime = Path(__file__).resolve().stat().st_mtime
    except OSError:
        script_mtime = None

    # The cache is ours but still untrusted input. Missing, unreadable, not
    # UTF-8, not JSON, nested past the parser's limit, not an object, or a
    # verdict that isn't a boolean: every one of those is a miss, never an
    # error (--config calls this outside any handler).
    cached = None
    try:
        with open(update_cache, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError, RecursionError):
        cached = None
    if not (isinstance(cached, dict)
            and isinstance(cached.get("update_available"), bool)):
        cached = None
    # _cache_age turns an unusable timestamp into +inf: stale, never an error.
    fresh = cached is not None and _cache_age(cached) < UPDATE_CHECK_TTL

    if fresh and script_mtime is not None and cached.get("script_mtime") == script_mtime:
        return cached["update_available"]

    # On the status line one round of this check is spread over repaints,
    # because a repaint gets one network operation (see "Render-path
    # networking"): the GitHub API on one, then, only if that brought no SHA,
    # git ls-remote alone on a later repaint with a free turn. Each is held to
    # _NET_FETCH_BUDGET. The "update_check" stamp marks when a round began;
    # no new round starts until it is _NET_RETRY_BACKOFF old.
    #
    # Every new round starts with the API again. It is the light path (one
    # request, no git process, no credentials), and GitHub's quota is hourly
    # while a new round comes an hour after a verdict (UPDATE_CHECK_TTL) or a
    # backoff after a failure. So a quota that is still used up costs one fast
    # 403 per round, and git still answers a repaint later.
    #
    # The round stamp is also the git leg's only backoff: one git leg per
    # round, whenever in the round it ran, so a late leg in one round doesn't
    # push the next round's later too.
    #
    # Until the network leg is about to run, the stamps are only peeked at.
    # A repaint that ends up answering from the cache (the script rewritten at
    # the same commit) or stopping at the branch or fork check must not spend
    # the round's git leg, or the turn another check in this repaint could use.
    # And a repaint with no leg it could run returns before spawning any git,
    # unless a fresh cache may still answer once git names the commit.
    leg = "api"
    round_began = 0.0
    if not interactive:
        round_began = _read_stamp("update_check")
        if 0 <= time.time() - round_began < _NET_RETRY_BACKOFF:
            leg = "git"

    def leg_can_run():
        """Peek, never take: could this repaint run its leg? Writes nothing."""
        now = time.time()
        if 0 <= now - _read_stamp("_last") < _NET_MIN_SPACING:
            return False  # another check has this repaint's network turn
        if leg == "git":
            # A git stamp from this round means its git leg ran, or was cut
            # short, already. One from the future (the clock was set back)
            # says nothing about this round, so like _begin_net_attempt it
            # is ignored rather than allowed to hold every round's leg off.
            return not round_began <= _read_stamp("update_check_git") <= now
        return True

    if not interactive and not fresh and not leg_can_run():
        return None  # nothing to try on this repaint, and no cache to answer

    # Cache is stale or unverifiable — now it's worth asking git. Everything
    # from here is bounded by _UPDATE_CHECK_BUDGET: the deadline is threaded
    # into every blocking call, capping each one's timeout to the budget that
    # remains, so a slow git or a slow GitHub can never hold a repaint open
    # for the sum of the individual timeouts.
    _deadline = time.time() + _UPDATE_CHECK_BUDGET

    local = get_local_commit(deadline=_deadline)
    if not local:
        # Not a git install (the curl / Invoke-WebRequest installs have no
        # .git), or git is unusable. Remember it for the TTL: left uncached,
        # every repaint spawned `git rev-parse` again for the same answer.
        _cache_quiet_update_verdict(update_cache, script_mtime, "no_git")
        return None  # skip silently

    if fresh and cached.get("local") == local[:8]:
        return cached["update_available"]

    if not interactive and not leg_can_run():
        return None  # the cache was for another commit, and nothing is free to ask

    # Only a main/master checkout gets the nag; topic and preview branches are
    # developers, usually ahead of upstream. Cache the verdict — returning
    # None uncached would re-run git on every repaint once the TTL lapses.
    # Known tradeoff: the cache keys on commit + script mtime, not branch, so
    # switching branches at the *same* commit serves the old verdict for up to
    # one TTL. Keying on branch would cost a git subprocess per repaint.
    on_default = _on_default_branch(deadline=_deadline)
    if on_default is None:
        return None
    if not on_default:
        try:
            with _secure_open_write(update_cache) as f:
                json.dump({
                    "timestamp": time.time(),
                    "update_available": False,
                    "local": local[:8],
                    "script_mtime": script_mtime,
                }, f)
        except OSError:
            pass
        return False

    # A fork's own commits are never on upstream main, so a bare inequality
    # would nag forever. Only ever prompt a checkout that actually tracks
    # upstream (issue: forks false-positive on the update indicator).
    if time.time() > _deadline:
        return None
    if _tracks_upstream(deadline=_deadline) is False:
        # Cached like the branch check above: a fork re-ran three git
        # processes per repaint to learn the same thing every time.
        _cache_quiet_update_verdict(update_cache, script_mtime, "fork", local)
        return False

    # Perform the check
    if time.time() > _deadline:
        return None
    if interactive:
        remote = get_remote_commit(deadline=_deadline)
    else:
        # Only now is the turn taken, its stamp written ahead of the network
        # so that a repaint killed mid-leg still counts as having tried. The
        # git leg passes backoff=0: the round check above is its backoff.
        if leg == "git":
            if not leg_can_run() or not _begin_net_attempt("update_check_git", backoff=0):
                return None  # another session ran it, or took the turn, meanwhile
        elif not _begin_net_attempt("update_check"):
            return None  # a check just failed or was cut short — don't pile on
        remote = get_remote_commit(deadline=_deadline, leg=leg)
    if not remote:
        return None  # network error, skip silently

    if local == remote:
        update_available = False
    else:
        ancestor = _remote_is_ancestor(remote, deadline=_deadline)
        if ancestor is None:
            if _deadline - time.time() < 0.05:
                # The budget ran out mid-ancestry-check: that is a lack of
                # time, not of information. Caching the pessimistic guess
                # would pin a false "update available" on an ahead-of-main
                # checkout for an hour, so skip silently and retry next time.
                return None
            # Can't decide locally (commit not fetched) — fall back to the
            # simple comparison, which is right for a plain tracking install.
            update_available = True
        else:
            # We're only behind when upstream's tip is NOT already in our history.
            update_available = not ancestor

    # Cache the result
    try:
        with _secure_open_write(update_cache) as f:
            json.dump({
                "timestamp": time.time(),
                "update_available": update_available,
                "local": local[:8],
                "remote": remote[:8],
                "script_mtime": script_mtime,
            }, f)
    except OSError:
        pass

    return update_available


def append_update_indicator(line, config=None):
    """Append a visible update indicator if a newer version is available."""
    try:
        if config:
            show = config.get("show", DEFAULT_SHOW)
            if not show.get("update", True):
                return line
        if check_for_update():
            return line + f" {BRIGHT_YELLOW}\u2191 Pulse Update{RESET}"
    except Exception:
        pass  # never break the status line for an update check
    return line


# --- Claude Code versions: comparing, the release channel, what is installed --

_CLAUDE_DIST_TAGS_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
_CLAUDE_UPDATE_TEXT = "↑ Claude Update"
_CLAUDE_RESTART_TEXT = "↻ Restart Claude"
_VERSION_PREFIX_RE = re.compile(r"v?(\d+(?:\.\d+)*)")
# A native install keeps one file per version, named for it ("2.1.317"; some
# builds add ".exe").
_VERSION_ENTRY_RE = re.compile(r"(\d+(?:\.\d+)+)(?:\.exe)?", re.IGNORECASE)


def _version_tuple(text):
    """The dotted numeric prefix of a version as a tuple of ints, or None.

    "2.1.317" is (2, 1, 317); a suffix is ignored ("2.1.317-rc.1" is the same
    tuple); text with no such prefix ("nightly", "") is None. The text is
    capped first, so a hostile run of digits can't reach int()'s size limit.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_PREFIX_RE.match(text.strip()[:64])
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _version_cmp(a, b):
    """1, 0 or -1 as version *a* is newer than, equal to or older than *b*.

    None when either side is unparseable. A short version is padded with
    zeros, so "2.1" equals "2.1.0".
    """
    ta, tb = _version_tuple(a), _version_tuple(b)
    if ta is None or tb is None:
        return None
    width = max(len(ta), len(tb))
    ta += (0,) * (width - len(ta))
    tb += (0,) * (width - len(tb))
    return (ta > tb) - (ta < tb)


def _claude_update_channel():
    """The release channel Claude Code follows: "stable", or "latest".

    Read from ``autoUpdatesChannel`` in the user's Claude ``settings.json``
    (``$CLAUDE_CONFIG_DIR`` when set). A missing file or key, unreadable or
    invalid JSON, or any value but "stable" means "latest", the default.
    """
    try:
        with open(_claude_config_dir() / "settings.json", "r", encoding="utf-8") as f:
            settings = json.loads(f.read(1_000_000))
    except Exception:  # noqa: BLE001 - a settings problem must never break the check
        return "latest"
    value = settings.get("autoUpdatesChannel") if isinstance(settings, dict) else None
    return "stable" if value == "stable" else "latest"


def _parse_claude_dist_tags(body):
    """``{tag: version}`` from the registry's dist-tags document, or None.

    Unusable unless it is a JSON object with a parseable "latest": that tag is
    the default channel, so a document without it is a bad answer, not data.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    tags = {}
    for name, version in data.items():
        if isinstance(name, str) and isinstance(version, str) and len(tags) < 32:
            tags[_sanitize(name)[:32]] = _sanitize(version)[:64]
    return tags if _version_tuple(tags.get("latest")) is not None else None


def _cached_claude_tags(cached):
    """The tags stored in a cache entry, or None when absent or malformed."""
    tags = cached.get("tags")
    if not (isinstance(tags, dict) and len(tags) <= 32
            and all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items())):
        return None
    return tags if _version_tuple(tags.get("latest")) is not None else None


def _claude_channel_verdict(tags, channel, local_version):
    """``(channel version, update available)`` for the running *local_version*.

    An update is available only when the channel's version is strictly newer.
    A channel missing from the tags offers nothing.
    """
    remote_version = tags.get(channel) or ""
    return remote_version, _version_cmp(remote_version, local_version) == 1


def _write_claude_update_cache(path, timestamp, local, remote, update_available,
                               channel, tags):
    try:
        with _secure_open_write(path) as f:
            json.dump({
                "timestamp": timestamp,
                "update_available": update_available,
                "local": local,
                "remote": remote,
                "channel": channel,
                "tags": tags,
            }, f)
    except OSError:
        pass


def _claude_data_dir():
    """Claude Code's data dir: ``$XDG_DATA_HOME/claude``, else ``~/.local/share/claude``."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".local" / "share"
    return base / "claude"


def _newest_native_version(versions_dir):
    """The highest version-named entry of a native install's versions dir, or None."""
    try:
        names = os.listdir(versions_dir)
    except OSError:
        return None
    best = None
    for name in names:
        match = _VERSION_ENTRY_RE.fullmatch(name)
        if match and (best is None or _version_cmp(match.group(1), best) == 1):
            best = match.group(1)
    return best


def _npm_package_version(package_dir):
    """The ``version`` in an installed @anthropic-ai/claude-code package.json, or None."""
    try:
        with open(Path(package_dir) / "package.json", "r", encoding="utf-8") as f:
            data = json.loads(f.read(262_144))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("name") != "@anthropic-ai/claude-code":
        return None
    version = data.get("version")
    if isinstance(version, str) and _version_tuple(version) is not None:
        return _sanitize(version).strip()
    return None


def _installed_version_for_launcher(launcher):
    """The Claude Code version the launcher at *launcher* leads to, or None."""
    real = Path(os.path.realpath(launcher))
    # Native on macOS, Linux and WSL: the launcher is a symlink into
    # <data>/claude/versions/<VERSION>, so the target's name is the version.
    if real.parent.name == "versions":
        match = _VERSION_ENTRY_RE.fullmatch(real.name)
        if match:
            return match.group(1)
    # npm: the package sits above the resolved launcher (POSIX symlink), or
    # beside the shim in node_modules (Windows).
    for folder in list(real.parents)[:6]:
        if folder.name == "claude-code" and folder.parent.name == "@anthropic-ai":
            version = _npm_package_version(folder)
            if version:
                return version
            break
    version = _npm_package_version(
        Path(launcher).parent / "node_modules" / "@anthropic-ai" / "claude-code")
    if version:
        return version
    # Native on Windows: the launcher is a real file, a copy of the newest
    # version, and the versions dir holds one file per version.
    if sys.platform == "win32" and real.suffix.lower() == ".exe":
        return _newest_native_version(_claude_data_dir() / "versions")
    return None


def _installed_claude_version():
    """The newest Claude Code version installed on disk, or None when unknown.

    Claude Code updates itself in the background; the new version runs only
    after a restart. Reads files only (a link, a directory listing, a
    package.json), never ``claude --version``: this runs on the render path.
    """
    try:
        if _CLAUDE_PATH:
            launchers = [str(_CLAUDE_PATH)]
        else:
            name = "claude.exe" if sys.platform == "win32" else "claude"
            launchers = [str(Path.home() / ".local" / "bin" / name)]
        for launcher in launchers:
            version = _installed_version_for_launcher(launcher)
            if version:
                return version
    except Exception:  # noqa: BLE001 - never break the bar over a file we can't read
        pass
    return None


def check_claude_code_update(local_version=None, *, interactive=False):
    """Check if a newer Claude Code version is available on npm. Returns True/False/None.

    True only when the version on the user's release channel ("latest", or
    "stable" per ``autoUpdatesChannel``) is strictly newer than the running
    one. Cached for 1 hour, as the registry's dist-tags (so a channel switch
    needs no new request). Fully silent on any error — never blocks the status
    line.

    *interactive* is for a command the user runs (``--config``): it skips the
    status line's turn-taking and backoff, may use the whole
    _UPDATE_CHECK_BUDGET, and still writes the shared cache. Keyword-only, so
    ``check_claude_code_update(version, True)`` can't skip those gates.

    *local_version* is the running version, which Claude Code reports on stdin.
    Passing it matters for latency: resolving it by shelling out to
    ``claude --version`` costs ~80ms, and because the version is needed to
    validate the cache, that subprocess used to run on *every* repaint — by far
    the most expensive thing the status line did. The subprocess remains as a
    fallback for older Claude Code builds that don't send the field.
    """
    state_dir = get_state_dir()
    update_cache = state_dir / "claude_code_update.json"

    local_version = _sanitize(str(local_version or "")).strip()
    if not local_version:
        if not _CLAUDE_PATH:
            return None
        try:
            result = subprocess.run(
                [_CLAUDE_PATH, "--version"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode != 0:
                return None
            # Parse "2.1.37 (Claude Code)" → "2.1.37"
            local_version = result.stdout.strip().split()[0]
        except Exception:
            return None

    # Read cached result — skip if local version changed (e.g. after claude update).
    # Untrusted input, as in check_for_update: anything unreadable or of the
    # wrong shape (a list, a non-numeric timestamp, a non-boolean verdict) is
    # a miss, never an error.
    cached = None
    try:
        with open(update_cache, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError, RecursionError):
        cached = None
    if not (isinstance(cached, dict)
            and isinstance(cached.get("update_available"), bool)):
        cached = None
    fresh = cached is not None and _cache_age(cached) < UPDATE_CHECK_TTL
    if fresh and cached.get("local") == _sanitize(local_version):
        # The cache holds the registry's tags, not a verdict for one channel:
        # the verdict is worked out here, for the channel the user follows now.
        tags = _cached_claude_tags(cached)
        if tags is not None:
            channel = _claude_update_channel()
            remote_version, update_available = _claude_channel_verdict(
                tags, channel, _sanitize(local_version))
            if (cached["update_available"] != update_available
                    or cached.get("remote") != remote_version
                    or cached.get("channel") != channel):
                # The channel changed: keep the file in step (--config reads
                # "remote"), under the original timestamp so the hour isn't extended.
                _write_claude_update_cache(
                    update_cache, cached.get("timestamp"), _sanitize(local_version),
                    remote_version, update_available, channel, tags)
            return update_available
        # No tags (written before 3.7.0), or damaged ones: a miss.

    # Get the dist-tags from the npm registry: one request serves both channels.
    if not interactive and not _begin_net_attempt("claude_code_update"):
        return None  # a check just failed or was cut short — don't pile on
    try:
        req = urllib.request.Request(
            _CLAUDE_DIST_TAGS_URL,
            headers={"User-Agent": "usage-pulse-update-checker"},
        )
        body = _fetch_bounded(req, 100_000,
                              budget=_UPDATE_CHECK_BUDGET if interactive else None)
        if body is None:
            return None
        tags = _parse_claude_dist_tags(body)
        if tags is None:
            return None
    except Exception:
        return None

    channel = _claude_update_channel()
    remote_version, update_available = _claude_channel_verdict(
        tags, channel, _sanitize(local_version))

    # Cache the result
    _write_claude_update_cache(
        update_cache, time.time(), _sanitize(local_version),
        remote_version, update_available, channel, tags)

    return update_available


def _claude_update_text(running=None):
    """The indicator's words: restart when the offered version is already installed.

    Claude Code updates itself in the background and applies the new version
    on the next start, so a channel version that is already on disk needs a
    restart, not an update. Anything not known for sure keeps "Update".
    """
    try:
        with open(get_state_dir() / "claude_code_update.json", "r", encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict):
            return _CLAUDE_UPDATE_TEXT
        if running and cached.get("local") != _sanitize(str(running)).strip():
            return _CLAUDE_UPDATE_TEXT  # the cache is for another version
        tags = _cached_claude_tags(cached)
        if tags is None:
            return _CLAUDE_UPDATE_TEXT
        remote_version = tags.get(_claude_update_channel())
        installed = _installed_claude_version()
        if installed is not None and _version_cmp(installed, remote_version) in (0, 1):
            return _CLAUDE_RESTART_TEXT
    except Exception:  # noqa: BLE001 - the wording must never break the bar
        pass
    return _CLAUDE_UPDATE_TEXT


def append_claude_update_indicator(line, config=None, stdin_ctx=None):
    """Append a visible Claude Code update indicator if a newer version is available.

    The text is "Claude Update" (up arrow) when the channel's version still
    has to be installed, and "Restart Claude" (cycle arrow) when it is already
    installed and only a restart applies it. Both use the flag ``claude_update``.
    """
    try:
        if config:
            show = config.get("show", DEFAULT_SHOW)
            if not show.get("claude_update", True):
                return line
        running = (stdin_ctx or {}).get("cc_version")
        if check_claude_code_update(running):
            return line + f" {BRIGHT_YELLOW}{_claude_update_text(running)}{RESET}"
    except Exception:
        pass  # never break the status line for an update check
    return line


def _read_version_from_file(script_path):
    """Read VERSION from a script file on disk (may differ from in-memory VERSION after git pull)."""
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VERSION"):
                    # VERSION = "1.7.0"
                    return line.split('"')[1]
    except Exception:
        pass
    return None


def _fetch_remote_version():
    """Fetch the VERSION string from the latest main on GitHub. Returns None on failure."""
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/claude_status.py"
        req = urllib.request.Request(url, headers={"User-Agent": "usage-pulse-update-checker"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace")
                if line.startswith("VERSION"):
                    version = line.split('"')[1]
                    return re.sub(r'[^a-zA-Z0-9.\-]', '', version) or None
    except Exception:
        pass
    return None


def cmd_update():
    """Pull the latest version from GitHub."""
    repo_dir = Path(__file__).resolve().parent
    script_path = Path(__file__).resolve()
    utf8_print(f"{BRIGHT_WHITE}usage-pulse update{RESET}\n")
    utf8_print(f"  Current version: {BRIGHT_WHITE}v{VERSION}{RESET}")

    # Check if we're in a git repo
    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        utf8_print(f"  {RED}Not a git repository.{RESET}")
        utf8_print(f"  Re-clone from: https://github.com/{GITHUB_REPO}")
        return

    # Verify the git remote points to the expected repository
    try:
        origin_result = subprocess.run(
            [_GIT_PATH, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
            cwd=str(repo_dir),
        )
        origin_url = origin_result.stdout.strip().lower() if origin_result.returncode == 0 else ""
        expected_suffixes = [
            sep + alias + ext
            for alias in GITHUB_REPO_ALIASES
            for sep in ("/", ":")        # https://github.com/user/repo, git@github.com:user/repo
            for ext in ("", ".git")
        ]
        if not any(origin_url.endswith(s) for s in expected_suffixes):
            utf8_print(f"  {RED}Origin URL does not match expected repository.{RESET}")
            utf8_print(f"  Expected: {GITHUB_REPO}")
            utf8_print(f"  Got:      {_sanitize(origin_url)}")
            return
    except Exception:
        utf8_print(f"  {RED}Could not verify git remote.{RESET}")
        return

    # Check current status
    local = get_local_commit()
    remote = get_remote_commit()

    if remote is None:
        utf8_print(f"  {RED}Could not reach GitHub to verify the update (API and git both failed).{RESET}")
        utf8_print(f"  Check your network connection and try again.")
        return

    if local and local == remote:
        utf8_print(f"  {GREEN}No update found — you're on the latest version (v{VERSION}).{RESET}")
        return

    # A checkout that already contains upstream's latest commit has local
    # commits on top of it: nothing to pull, and nothing to roll back.
    if _remote_is_ancestor(remote) is True:
        utf8_print(f"  {GREEN}No update found — this checkout is ahead of main with your own commits (v{VERSION}).{RESET}")
        return

    # Only a clean checkout of main is updated. A side branch would take a
    # merge, and uncommitted edits are exactly what a failed update must never
    # touch. Untracked files are ignored: git refuses to overwrite one.
    if _on_default_branch() is False:
        branch = _git("symbolic-ref", "--short", "-q", "HEAD")
        where = f"on branch '{_sanitize(branch)}'" if branch else "on a detached HEAD"
        utf8_print(f"  {RED}This checkout is {where}, not main, so it was not updated.{RESET}")
        utf8_print(f"  Switch to main (git checkout main) and run --update again.")
        return
    dirty = _git("status", "--porcelain", "--untracked-files=no", timeout=10)
    if dirty is None:
        utf8_print(f"  {RED}Could not read the git status of {_sanitize(str(repo_dir))}; not updating.{RESET}")
        return
    if dirty:
        utf8_print(f"  {RED}You have uncommitted changes in {_sanitize(str(repo_dir))}, so it was not updated.{RESET}")
        utf8_print(f"  Commit or stash them (git stash), then run --update again.")
        return

    # Fetch remote version to show what's available
    remote_version = _fetch_remote_version()
    if remote_version and remote_version != VERSION:
        utf8_print(f"  {BRIGHT_YELLOW}Update found! v{VERSION} -> v{_sanitize(remote_version)}{RESET}")
    else:
        utf8_print(f"  {BRIGHT_YELLOW}Update found! New changes available{RESET}")

    # Ask for confirmation unless --confirm was passed
    if "--confirm" not in sys.argv:
        if sys.stdin.isatty():
            try:
                answer = input(f"  Apply update? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    utf8_print(f"  {DIM}Update cancelled.{RESET}")
                    return
            except (EOFError, KeyboardInterrupt):
                utf8_print(f"\n  {DIM}Update cancelled.{RESET}")
                return
        else:
            utf8_print(f"  {DIM}Non-interactive mode. Run with --update --confirm to apply.{RESET}")
            return

    # Capture local commit before pulling so we can show changelog after
    pre_pull_commit = local

    # Run git pull
    utf8_print(f"  Pulling latest from GitHub...")
    try:
        result = subprocess.run(
            [_GIT_PATH, "pull", "--ff-only", "origin", "main"],
            capture_output=True, text=True, timeout=30,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            # Verify post-pull HEAD matches the expected remote commit
            post_pull_head = get_local_commit()
            if post_pull_head and post_pull_head != remote:
                utf8_print(f"  {RED}Integrity check failed: HEAD after pull ({post_pull_head[:8]}) does not match expected remote ({remote[:8]}).{RESET}")
                if pre_pull_commit:
                    utf8_print(f"  Rolling back to previous commit ({pre_pull_commit[:8]})...")
                    # --keep, not --hard: it refuses to overwrite a file that
                    # has local changes instead of silently discarding them.
                    rolled_back = False
                    try:
                        reset = subprocess.run(
                            [_GIT_PATH, "reset", "--keep", pre_pull_commit],
                            capture_output=True, text=True, timeout=10,
                            cwd=str(repo_dir),
                        )
                        rolled_back = reset.returncode == 0
                    except Exception:
                        pass
                    if not rolled_back:
                        utf8_print(f"  {YELLOW}Could not roll back automatically. To go back by hand run: git -C \"{_sanitize(str(repo_dir))}\" reset --keep {pre_pull_commit[:12]}{RESET}")
                utf8_print(f"  {YELLOW}Update aborted. Please try again or re-clone the repository.{RESET}")
                return
            # Read the new version from the updated file on disk
            new_version = _sanitize(_read_version_from_file(script_path) or "")
            if new_version and new_version != VERSION:
                utf8_print(f"  {GREEN}Updated to v{new_version}!{RESET}")
            else:
                utf8_print(f"  {GREEN}Updated successfully!{RESET}")
            if result.stdout.strip():
                for ln in result.stdout.strip().split("\n"):
                    utf8_print(f"  {DIM}{_sanitize(ln)}{RESET}")
            # Show changelog — commits between old HEAD and new HEAD
            if pre_pull_commit:
                try:
                    log_result = subprocess.run(
                        [_GIT_PATH, "log", f"{pre_pull_commit}..HEAD", "--oneline", "--no-decorate", "-20"],
                        capture_output=True, text=True, timeout=5,
                        cwd=str(repo_dir),
                    )
                    if log_result.returncode == 0 and log_result.stdout.strip():
                        utf8_print(f"\n  {BOLD}Changelog:{RESET}")
                        for ln in log_result.stdout.strip().split("\n"):
                            utf8_print(f"    {DIM}{_sanitize(ln)}{RESET}")
                except Exception:
                    pass
            # Clear all caches so the update indicator disappears immediately
            state_dir = get_state_dir()
            for cache_file in (state_dir / "update_check.json", get_cache_path()):
                try:
                    cache_file.unlink()
                except OSError:
                    pass
            # The release may have changed commands/pulse.md (the /pulse
            # command lists the options this version understands); copy the
            # new one in, or /pulse keeps advertising the old version.
            install_pulse_command()
            utf8_print(f"\n  Restart Claude Code to use v{new_version or 'latest'}.")
        else:
            utf8_print(f"  {RED}Update failed:{RESET}")
            if result.stderr.strip():
                for ln in result.stderr.strip().split("\n"):
                    utf8_print(f"  {DIM}{_sanitize(ln)}{RESET}")
    except subprocess.TimeoutExpired:
        utf8_print(f"  {RED}Timed out. Check your network connection.{RESET}")
    except Exception as e:
        utf8_print(f"  {RED}Update error: {type(e).__name__}{RESET}")


_ERROR_CACHE_TTL = 10   # seconds — retry errors faster than normal data
_RATE_LIMIT_CACHE_TTL = 120  # seconds — back off longer on 429 to avoid retry storms

# Per-model weekly caps (seven_day_opus/_sonnet/_fable) are not on stdin and
# must come from the OAuth usage endpoint. They are refreshed at most this
# often, and served from the last cached value in between, so a repaint costs
# a network call once every 5 minutes rather than on every draw.
_MODEL_CAPS_TTL = 300  # seconds; config "model_caps_refresh_seconds" overrides
_MODEL_CAPS_MIN_TTL = 60
# Hard ceiling on that request. The status line blocks on it, so it must be
# short — matches the spirit of _UPDATE_CHECK_BUDGET (6s) for the same reason.
_MODEL_CAPS_TIMEOUT = 5  # seconds
# Minimum gap between two refresh *attempts*, whatever their outcome. Claude
# Code cancels an in-flight status-line run when a newer repaint arrives, and
# a cancelled run leaves no fetched_at stamp — so without this, a busy session
# would start the request again on every repaint until one survived.
_MODEL_CAPS_RETRY = 60  # seconds


def _model_caps_ttl(config):
    """Return the per-model caps refresh interval from config, floored."""
    try:
        ttl = int((config or {}).get("model_caps_refresh_seconds", _MODEL_CAPS_TTL))
    except (TypeError, ValueError):
        return _MODEL_CAPS_TTL
    return max(_MODEL_CAPS_MIN_TTL, ttl)


# --- Untrusted state files ---------------------------------------------------
#
# Every file under the state dir can be damaged (a hand edit, a disk or
# antivirus fault, another version's format) or caught mid-write by a window
# that still writes in place. A raise on the render path is a blank bar on
# every repaint, and an empty read written straight back erases the data. The
# helpers below turn a file into only what the renderer can use.

def _usage_number(value):
    """*value* as a finite float, or None: text, bool, NaN, Infinity, an int too big."""
    try:
        return _stdin_number(value)
    except OverflowError:   # an int past the float range
        return None


_USAGE_WINDOW_KEYS = ("five_hour", "seven_day", "seven_day_opus",
                      "seven_day_sonnet", "seven_day_fable")
_EXTRA_USAGE_NUMBERS = ("monthly_limit", "used_credits", "utilization")


def _clean_usage(usage):
    """The part of an untrusted usage dict that the renderer can draw.

    A window is kept only if it is a dict whose utilization is a finite number
    (or null, which the API sends and the renderer reads as 0); its reset time
    must be text or null. Anything else is dropped, so that window draws as
    missing instead of raising. The extra_usage numbers are coerced the same
    way, to null. A window the plan lacks (null) is kept as null.
    """
    if not isinstance(usage, dict):
        return {}
    clean = {}
    for key in _USAGE_WINDOW_KEYS:
        if key not in usage:
            continue
        window = usage[key]
        if window is None:
            clean[key] = None
            continue
        if not isinstance(window, dict):
            continue
        utilization = window.get("utilization")
        if utilization is not None and _usage_number(utilization) is None:
            continue
        window = dict(window)
        resets_at = window.get("resets_at")
        if resets_at is not None and not isinstance(resets_at, str):
            window["resets_at"] = None
        clean[key] = window
    extra = usage.get("extra_usage")
    if isinstance(extra, dict):
        extra = dict(extra)
        for key in _EXTRA_USAGE_NUMBERS:
            if extra.get(key) is not None and _usage_number(extra[key]) is None:
                extra[key] = None
        clean["extra_usage"] = extra
    elif "extra_usage" in usage and extra is None:
        clean["extra_usage"] = None
    return clean


def _clean_cache_entry(cached):
    """Make a cache dict read from disk safe to draw from (in place); returns it.

    Usage is cleaned; if nothing in it is usable it is removed, so the caller
    falls back to the cached line instead of drawing zeros. A line or plan that
    is not text is removed.
    """
    if "usage" in cached:
        if isinstance(cached["usage"], dict):
            cleaned = _clean_usage(cached["usage"])
            if cleaned or not cached["usage"]:
                cached["usage"] = cleaned
            else:
                del cached["usage"]
        else:
            del cached["usage"]
    for key in ("line", "plan"):
        if key in cached and not isinstance(cached[key], str):
            del cached[key]
    return cached


_SAFE_FALLBACK_LINE = "Usage unavailable \u2014 retrying shortly"


def _guarded_status_line(usage, plan, config, stdin_ctx, cache_age, fallback=""):
    """build_status_line that never raises, so a bad file never means a blank bar.

    First the data as given; then the data cleaned down to what the renderer can
    draw; then the cached line, if there is one; then a short message. Only
    Exception is caught, so Claude Code cancelling a repaint still works.
    """
    try:
        return build_status_line(usage, plan, config, stdin_ctx, cache_age=cache_age)
    except Exception:
        pass
    try:
        return build_status_line(_clean_usage(usage), plan, config, stdin_ctx,
                                 cache_age=cache_age)
    except Exception:
        pass
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    return _SAFE_FALLBACK_LINE


# How long a state file that cannot be parsed is left alone before it is
# treated as damaged for good. A window that still writes in place leaves the
# file empty or half-written for a few milliseconds; another window must not
# take that for "no data" and write a one-sample file over a day of history.
_DAMAGED_STATE_GRACE = 10


# While another window swaps a state file in with os.replace, Windows makes
# open() on it fail for a moment with PermissionError (measured: a third of
# the reads in a tight loop against a writer, about one in fifty at a write per
# 10 ms). Read as "no data" that erased the 429 backoff, so the read is tried
# again a couple of times first. A PermissionError that persists is real.
_STATE_READ_TRIES = 3
_STATE_READ_PAUSE = 0.005


def _read_state_file(path, binary=False):
    """Contents of a state file (bytes if *binary*, else UTF-8 text)."""
    for attempt in range(_STATE_READ_TRIES):
        try:
            if binary:
                with open(path, "rb") as f:
                    return f.read()
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except PermissionError:
            if attempt == _STATE_READ_TRIES - 1:
                raise
            time.sleep(_STATE_READ_PAUSE)


def _read_state_json(path):
    """Read a JSON state file ahead of a read-modify-write: ``(ok, data)``.

    A missing file is ``(True, None)``. A file that cannot be parsed or decoded
    is ``(False, None)`` while it was changed within _DAMAGED_STATE_GRACE
    seconds (the caller skips this write) and ``(True, None)`` once it has sat
    that way (the caller starts again from empty, which heals it). A file that
    cannot be opened at all is ``(False, None)``.
    """
    try:
        return True, json.loads(_read_state_file(path, binary=True))
    except FileNotFoundError:
        return True, None
    except (ValueError, RecursionError):
        # Not UTF-8, not JSON (a truncated write), or an int past the digit limit.
        try:
            recent = abs(time.time() - os.stat(path).st_mtime) <= _DAMAGED_STATE_GRACE
        except OSError:
            recent = True
        return (not recent), None
    except OSError:
        return False, None


def _load_samples(path, numeric_keys):
    """``(ok, samples)`` from a history-style file: a list of dicts with a numeric ``t``.

    An entry is kept only if its ``t`` and every key in *numeric_keys* that it
    carries are finite numbers. A file of any other shape holds no samples.
    ``ok`` is False when the caller should skip its write (see _read_state_json).
    """
    ok, raw = _read_state_json(path)
    if not ok or not isinstance(raw, list):
        return ok, []
    samples = []
    for sample in raw:
        if not isinstance(sample, dict) or _usage_number(sample.get("t")) is None:
            continue
        if any(key in sample and _usage_number(sample[key]) is None for key in numeric_keys):
            continue
        samples.append(sample)
    return True, samples


# Orphans from atomic writes that were killed between the temp file and the
# rename are named <file>.<pid>-<8 hex>.tmp (see _atomic_json_write). A write
# takes milliseconds, so anything this old belongs to no live writer.
_TMP_ORPHAN_RE = re.compile(r"\.\d+-[0-9a-f]{8}\.tmp$")
_TMP_ORPHAN_AGE = 600
_TMP_SWEEP_ODDS = 0.02   # about one cache write in fifty


def _sweep_stale_tmp_files(directory, max_age=_TMP_ORPHAN_AGE):
    """Delete this script's own leftover temp files older than *max_age* seconds."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    cutoff = time.time() - max_age
    for entry in entries:
        if not _TMP_ORPHAN_RE.search(entry.name):
            continue
        try:
            if entry.is_file(follow_symlinks=False) and \
                    entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass


def read_cache(cache_path, ttl):
    """Return the full cache dict if fresh, else None."""
    try:
        cached = json.loads(_read_state_file(cache_path))
        # The file is ours, but it is still untrusted input: a truncated write,
        # a disk error, or a hand-edit can leave valid JSON of the wrong shape.
        # Anything other than a dict is treated as a cache miss rather than
        # allowed to raise — read_cache sits on the hot path, so an exception
        # here blanks the status bar.
        if not isinstance(cached, dict):
            return None
        # An active 429 backoff wins over the normal TTL: keep serving what we
        # have until the backoff expires. Without this, a rate-limited session
        # that still had usable cached usage re-hit the API on every refresh —
        # exactly the retry storm the backoff exists to prevent.
        blocked_until = _cache_seconds(cached.get("rate_limited_until")) or 0
        if blocked_until and time.time() < blocked_until:
            return _clean_cache_entry(cached)
        # Error-only entries expire faster, except rate limit errors which back off longer
        if "usage" in cached:
            effective_ttl = ttl
        elif cached.get("rate_limited"):
            effective_ttl = _RATE_LIMIT_CACHE_TTL
        else:
            effective_ttl = _ERROR_CACHE_TTL
        timestamp = _cache_seconds(cached.get("timestamp", 0))
        if timestamp is None:
            return None  # unusable timestamp — treat as a miss
        if time.time() - timestamp < effective_ttl:
            return _clean_cache_entry(cached)
    except (OSError, ValueError, RecursionError, KeyError):
        # Missing or unreadable (OSError); not UTF-8, not JSON, or an int past
        # the int/str digit limit (each a ValueError); nested past the parser's
        # depth (RecursionError). Every one is a miss: a raise blanks the bar.
        pass
    return None


def _read_stale_cache(cache_path):
    """Return cached data regardless of age, or None if unavailable.

    Prefers entries with 'usage' data, but also accepts entries with just
    a rendered 'line' — this prevents sticky error states when a 429 hits
    before any usage data has been cached.
    """
    try:
        cached = json.loads(_read_state_file(cache_path))
        # Same untrusted-shape rule as read_cache. This one feeds the 429
        # fallback, where raising means no status line is printed at all:
        # a cache file containing bare `42` made `"usage" in cached` raise.
        if not isinstance(cached, dict):
            return None
        # Values the renderer cannot draw are dropped here too, so the 429
        # branch and the stdin merge never carry them forward.
        _clean_cache_entry(cached)
        if "usage" in cached or "line" in cached:
            return cached
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        pass  # the same misses as read_cache
    return None


_USAGE_CACHE_KEYS = {
    "five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
    "seven_day_fable", "extra_usage",
}

def write_cache(cache_path, line, usage=None, plan=None,
                rate_limited_until=None, rate_limit_fails=None,
                data_timestamp=None, model_caps_fetched_at=None,
                model_caps_attempted_at=None):
    """Persist the rendered line plus optional usage/plan and 429 backoff state.

    ``rate_limited_until`` is an epoch after which it is worth calling the API
    again; ``rate_limit_fails`` is the consecutive-429 count that produced it.
    Both are omitted on a normal write, which is what clears the backoff once a
    request succeeds.

    ``data_timestamp`` is the epoch the usage was actually fetched. The 429
    fallback re-writes usage it just read from a stale cache, and stamping
    that copy with the current time made old quota data look freshly fetched
    on every failed retry — the staleness warning could never fire.
    """
    try:
        data = {"timestamp": data_timestamp or time.time(), "line": line}
        # `usage` can arrive from a stale cache file (the 429 fallback path
        # re-writes what it just read), so it is not guaranteed to be a dict.
        if isinstance(usage, dict):
            data["usage"] = {k: v for k, v in usage.items() if k in _USAGE_CACHE_KEYS}
        if plan is not None:
            data["plan"] = plan
        if rate_limited_until:
            data["rate_limited_until"] = rate_limited_until
            data["rate_limited"] = True
        if rate_limit_fails:
            data["rate_limit_fails"] = rate_limit_fails
        if model_caps_fetched_at:
            data["model_caps_fetched_at"] = model_caps_fetched_at
        if model_caps_attempted_at:
            data["model_caps_attempted_at"] = model_caps_attempted_at
        # Atomic, like stdin_ctx.json: written in place, the file is empty for
        # most of each write, and a second window that read it then took that
        # for a miss and wrote it back without the 429 backoff and the caps. A
        # replace that fails (Windows refuses while a reader holds the file
        # open) is OSError: skip this write, the next repaint writes again.
        _atomic_json_write(cache_path, data, indent=None)
        if random.random() < _TMP_SWEEP_ODDS:
            _sweep_stale_tmp_files(Path(cache_path).parent)
    except OSError:
        pass


_RATE_LIMIT_BACKOFF_BASE = 60    # seconds for the first 429
_RATE_LIMIT_BACKOFF_MAX = 900    # cap at 15 minutes
_RATE_LIMIT_JITTER = 10          # spread concurrent sessions out
# The data time a rewrite records when the real one is unknown: the first
# second of the epoch, which every reader takes as long stale. Not 0, which
# write_cache treats as "now".
_UNKNOWN_DATA_TIME = 1.0


def _rate_limit_backoff(fail_count):
    """Return (seconds_to_wait, next_fail_count) for a 429.

    Doubles per consecutive failure up to a cap, plus jitter so several Claude
    Code windows that were rate-limited together don't all retry on the same
    tick and re-trigger the limit.
    """
    try:
        fails = max(0, int(fail_count or 0)) + 1
    except (TypeError, ValueError, OverflowError):
        # cache.json is untrusted: int() raises OverflowError for an
        # Infinity, which json reads as a float. Unusable restarts the count.
        fails = 1
    # Cap the shift before computing the power: 2 ** a large fail count would
    # build a huge int before min() ever saw it.
    delay = _RATE_LIMIT_BACKOFF_BASE * (2 ** min(fails - 1, 8))
    delay = min(delay, _RATE_LIMIT_BACKOFF_MAX)
    return delay + random.uniform(0, _RATE_LIMIT_JITTER), fails


def _retry_after_seconds(headers):
    """Seconds a 429's Retry-After header asks us to wait, or 0.0 if unusable.

    Only a plain non-negative number of seconds counts: RFC 9110 section
    10.2.3 delay-seconds, plus a decimal fraction, which earlier releases
    honoured too. Anthropic documents seconds, so an HTTP-date falls back like
    any other shape (as it always did). The wait is capped at six hours, the
    default ceiling of urllib3 and other popular HTTP clients: far above the
    multi-thousand-second values the usage endpoint really sends, yet a bad
    value can no longer park the caller for good. A bare float() let "inf",
    "1e9" or a long run of digits write an endless rate_limited_until.
    Callers take max() with their exponential schedule, so 0.0 leaves that
    schedule in charge.
    """
    try:
        value = headers.get("retry-after")
    except (AttributeError, TypeError):
        return 0.0
    if not isinstance(value, str):
        return 0.0
    value = value.strip()
    # [0-9] is ASCII only: str.isdigit() would pass superscripts, which
    # float() then rejects. A long digit run parses to inf, which the cap
    # turns into the ceiling, as a huge delay-seconds deserves.
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]*)?", value):
        return 0.0
    return min(float(value), 6 * 60 * 60.0)


# ---------------------------------------------------------------------------
# Credentials & API
# ---------------------------------------------------------------------------
# SECURITY: OAuth tokens are ONLY sent to these Anthropic-owned domains.
# They are never written to cache/state files, never logged, and never
# sent anywhere else. The _authorized_request() guard enforces this.
_TOKEN_ALLOWED_DOMAINS = frozenset({"api.anthropic.com", "console.anthropic.com", "platform.claude.com"})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block HTTP redirects to prevent tokens from leaking to third-party domains.

    Only https on an allowed domain is followed. The stdlib copies every header
    onto the redirected request, Authorization included, so an http:// target,
    even on an allowed host, would re-send the token in plain text.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or target.hostname not in _TOKEN_ALLOWED_DOMAINS:
            raise urllib.error.HTTPError(
                newurl, code, f"Redirect to non-allowed domain blocked", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Bounded connect: a dead address family costs one short attempt instead of the
# whole request timeout (see "Render-path networking"). A Python without ssl has
# no HTTPS to bound, and its token requests fail as a URLError, as they did.
if _BoundedHTTPSHandler is not None:
    _safe_opener = urllib.request.build_opener(_NoRedirectHandler, _BoundedHTTPSHandler())
else:
    _safe_opener = urllib.request.build_opener(_NoRedirectHandler)


def _authorized_request(url, token, headers=None, data=None, method=None, timeout=10):
    """Make an HTTP request with an auth token, but ONLY to allowed Anthropic domains.

    Raises ValueError if the URL is not https or its domain is not in the
    allowlist, before the token is put on anything.
    This prevents tokens from ever being sent to third-party servers, or in
    plain text (a token refresh carries the refresh token in its body),
    even if the code is modified or a URL is misconfigured.
    Redirects are held to the same rule to prevent token exfiltration.
    """
    parts = urlparse(url)
    if parts.scheme != "https":  # urlparse lowercases it: HTTPS:// is https
        raise ValueError("Token request blocked: the URL is not https")
    domain = parts.hostname
    if domain not in _TOKEN_ALLOWED_DOMAINS:
        raise ValueError(f"Token request blocked: {_sanitize(domain)} is not an allowed domain")
    hdrs = dict(headers) if headers else {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.setdefault("User-Agent", f"usage-pulse/{VERSION}")
    req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
    return _safe_opener.open(req, timeout=timeout)

def _oauth_section(data):
    """The ``claudeAiOauth`` dict of a credentials file, or {} for anything else.

    The file is not ours to validate: an explicit ``null`` (or a list, or text)
    in place of the section is valid JSON, and ``.get('claudeAiOauth', {})``
    only supplies its default when the key is missing, not when it is null.
    """
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else {}


def _read_credential_data():
    """Read raw credential data from file or macOS Keychain. Returns (dict, source)."""
    # 1. File-based (~/.claude/.credentials.json)
    creds_path = _claude_config_dir() / ".credentials.json"
    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if _oauth_section(data).get("accessToken"):
            return data, "file"
    except (OSError, ValueError, RecursionError):
        # Missing, locked or unreadable (OSError); not UTF-8 or not JSON (each
        # a ValueError). Any of them means no credentials here, not a raise on
        # the render path.
        pass

    # 2. macOS Keychain fallback
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout.strip())
                if _oauth_section(data).get("accessToken"):
                    return data, "keychain"
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError):
            pass

    return None, None


def _extract_credentials(data):
    """Extract token and plan from credential data dict."""
    oauth = _oauth_section(data)
    token = oauth.get("accessToken")
    tier = oauth.get("rateLimitTier")
    if not token or not isinstance(token, str):
        return None, None
    if not isinstance(tier, str):
        tier = ""   # an explicit null (or any non-text) tier means no plan name
    plan = PLAN_NAMES.get(tier, _sanitize(tier.replace("default_claude_", "").replace("_", " ").title()))
    return token, plan



def _refresh_oauth_token(refresh_token):
    """Use refresh token to obtain a new access token. Returns new token data or None."""
    try:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }).encode("utf-8")
        with _authorized_request(
            "https://platform.claude.com/v1/oauth/token",
            None,  # no Bearer token — this uses the refresh token in the body
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        ) as resp:
            return json.loads(resp.read(100_000))
    except Exception:
        return None


def get_credentials():
    """Read OAuth token from credentials file, macOS Keychain, or env var."""
    data, source = _read_credential_data()
    if data:
        token, plan = _extract_credentials(data)
        if token:
            return token, plan

    # Environment variable fallback (all platforms)
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return env_token, ""

    return None, None


def refresh_and_retry(plan):
    """Attempt to refresh expired OAuth token. Returns (new_token, plan) or (None, plan)."""
    data, source = _read_credential_data()
    if not data:
        return None, plan
    oauth = data.get("claudeAiOauth", {})
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return None, plan

    token_data = _refresh_oauth_token(refresh_token)
    if not token_data or "access_token" not in token_data:
        return None, plan

    # Return refreshed token in-memory only — don't write back to
    # credential store to avoid race conditions with Claude Code
    return token_data["access_token"], plan


# ---------------------------------------------------------------------------
# Plan name from the OAuth profile
# ---------------------------------------------------------------------------
# The credentials file carries only the rate-limit tier ("default_claude_max_20x")
# and the names the plans are sold under have moved on from it: an Ultra
# account reads "Max 20x" from the tier. The profile's
# ``organization.plan_display_name`` is the current name. It is the only field
# read from that reply; the e-mail and account name beside it are discarded.
_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
_PLAN_LABEL_TTL = 86400       # re-check once a day
_PLAN_LABEL_RETRY = 3600      # after a failed or empty attempt
_PLAN_LABEL_TIMEOUT = 5       # seconds; the call is also bounded by _NET_FETCH_BUDGET


def _epoch_or_zero(value):
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def fetch_plan_label(token, timeout=_PLAN_LABEL_TIMEOUT):
    """``organization.plan_display_name`` from the OAuth profile, or None."""
    with _authorized_request(_PROFILE_URL, token,
                             headers={"anthropic-beta": "oauth-2025-04-20",
                                      "Accept": "application/json"},
                             timeout=timeout) as resp:
        data = json.loads(resp.read(50_000))
    org = data.get("organization") if isinstance(data, dict) else None
    name = org.get("plan_display_name") if isinstance(org, dict) else None
    return _clean_provider_label(name) if isinstance(name, str) else None


def _plan_shown(config):
    """True when the plan name is drawn: the ``plan`` widget, or provider tiers."""
    show = config.get("show", DEFAULT_SHOW) if isinstance(config, dict) else DEFAULT_SHOW
    return bool(isinstance(show, dict) and show.get("plan", False))


def _plan_label_path():
    return get_account_state_dir() / "plan_label.json"


def _read_plan_label():
    """``{"label", "fetched_at", "attempted_at"}`` for this account, or ``{}``."""
    try:
        data, _ = _read_small_json(_plan_label_path(), limit=4_000)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _display_plan(plan, config):
    """The plan name to draw: the profile's name when known, else ``plan``."""
    if not _plan_shown(config):
        return plan
    label = _read_plan_label().get("label")
    return label if isinstance(label, str) and label else plan


def _refresh_plan_label(config, blocked_until=None, token=None, now=None):
    """Ask the profile for the plan name when due; returns the label or None.

    Due means: the plan is drawn, the last successful fetch is over a day
    old, the last attempt over an hour old, and no 429 backoff
    (``blocked_until``) is running. The attempt is stamped before the request
    (Claude Code may cancel the repaint), the request takes its turn with the
    other optional network checks, and it has a hard wall-clock ceiling.
    ``token`` saves a second credentials read when the caller holds one.
    Never raises.
    """
    now = time.time() if now is None else now
    if not _plan_shown(config):
        return None
    state = _read_plan_label()
    label = state.get("label") if isinstance(state.get("label"), str) else None
    if now - _epoch_or_zero(state.get("fetched_at")) < _PLAN_LABEL_TTL:
        return label
    if now - _epoch_or_zero(state.get("attempted_at")) < _PLAN_LABEL_RETRY:
        return label
    if blocked_until and now < _epoch_or_zero(blocked_until):
        return label
    try:
        if token is None:
            token, _ = get_credentials()
    except Exception:
        token = None
    if not token:
        return label
    if not _begin_net_attempt("plan_label", backoff=_PLAN_LABEL_RETRY):
        return label
    state = dict(state)
    state["attempted_at"] = now
    try:
        _atomic_json_write(_plan_label_path(), state)
    except Exception:
        _end_net_attempt("plan_label")
        return label          # unsaved attempt: never risk one fetch per repaint
    try:
        fresh = _call_bounded(lambda tok=token: fetch_plan_label(tok), _NET_FETCH_BUDGET)
    except BaseException:
        fresh = None
    finally:
        token = None
        _end_net_attempt("plan_label")
    if fresh:
        state.update({"label": fresh, "fetched_at": now})
        try:
            _atomic_json_write(_plan_label_path(), state)
        except Exception:
            pass
        return fresh
    return label


def _normalize_usage(usage):
    """Fold model-scoped weekly caps out of ``limits[]`` into top-level keys.

    Alongside the flat ``seven_day_opus`` / ``seven_day_sonnet`` fields, the
    oauth/usage payload carries a ``limits`` list describing per-model weekly
    budgets::

        {"kind": "weekly_scoped", "percent": 82, "resets_at": "...",
         "scope": {"model": {"display_name": "Fable"}}}

    Mapping those to ``seven_day_<name>`` lets the renderer treat a Fable cap
    exactly like the Opus and Sonnet ones, and picks up any future model
    without another code change. A flat field that already carries a
    utilization always wins — this only fills gaps.
    """
    if not isinstance(usage, dict):
        return usage
    limits = usage.get("limits")
    if not isinstance(limits, list):
        return usage
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if not isinstance(model, dict):
            continue
        name = model.get("display_name") or model.get("id")
        if not name:
            continue
        # "Fable" → seven_day_fable; "Sonnet 4.6" → seven_day_sonnet_4_6
        slug = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
        if not slug:
            continue  # e.g. display_name "!!!" — would yield a bare "seven_day_"
        key = "seven_day_" + slug
        existing = usage.get(key)
        if isinstance(existing, dict) and existing.get("utilization") is not None:
            continue
        pct = entry.get("percent")
        if pct is None:
            continue
        try:
            utilization = float(pct)
        except (TypeError, ValueError):
            continue
        usage[key] = {
            "utilization": utilization,
            "resets_at": entry.get("resets_at"),
        }
    return usage


def fetch_usage(token, timeout=10):
    with _authorized_request(
        "https://api.anthropic.com/api/oauth/usage",
        token,
        headers={"anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"},
        timeout=timeout,
    ) as resp:
        return _normalize_usage(json.loads(resp.read(1_000_000)))  # 1 MB max


# ---------------------------------------------------------------------------
# MiniMax provider extension (additive, see PR description)
# ---------------------------------------------------------------------------
# When running under mxclaude (a wrapper that points the `claude` binary at
# MiniMax's Anthropic-protocol-compatible endpoint, e.g. with model
# `MiniMax-M3`), three of Pulse's stdin's Anthropic-format fields don't
# accurately describe the session, and a fourth is missing entirely:
#
#   1. `context_window.used_percentage` is INPUT-ONLY per Anthropic's docs
#      (input_tokens + cache_creation + cache_read). Claude Code's own
#      "X% context used" warning includes OUTPUT too. Result: the bar
#      visibly disagrees with the warning.
#
#   2. `context_window_size` is unreliable from the Anthropic-protocol shim.
#      MiniMax's shim reports 200,000 for M3 even though the real window is
#      1,000,000 (verified 2026-06-02 against the model's docs). Inflates
#      the % display by ~5×.
#
#   3. `cost.total_cost_usd` is Anthropic-priced and wrong for MiniMax.
#      Recompute from token counts using MiniMax's tiered pricing.
#
#   4. The `rate_limits` block is absent (only Claude.ai Pro/Max subscribers
#      get it on stdin). Fall back to `mmx quota show` and reshape to
#      Pulse's existing _rate_limits schema so the renderer — which is
#      data-shape-agnostic — picks it up automatically.
#
# The patch is purely additive: no changes to the OAuth flow, the
# `_TOKEN_ALLOWED_DOMAINS` allowlist, the renderer, or any existing
# user-visible behavior. The new code is feature-gated on the env
# (`ANTHROPIC_BASE_URL` contains `minimax.io` + `ANTHROPIC_MODEL` starts
# with `MiniMax-`), so it cannot activate for non-MiniMax users.

# Per-model context window sizes. Don't trust the shim's stdin value.
MINIMAX_CONTEXT_SIZES = {
    "MiniMax-M3":              1_000_000,
    "MiniMax-M2.7":              128_000,
    "MiniMax-M2.7-highspeed":    128_000,
}

# Pricing tiers (USD per 1M tokens, no 7-day discount).
# Tier1 = ≤512K, tier2 = 512K~1M. Per https://platform.minimax.io/docs/token-plan/claude-code
MINIMAX_PRICING = {
    "tier1": {"max_tokens":   512_000, "in_per_m": 0.60, "out_per_m": 2.40},
    "tier2": {"max_tokens": 1_000_000, "in_per_m": 1.20, "out_per_m": 4.80},
}


def _parse_minimax_quota(mmx_json):
    """Convert `mmx quota show --output json` into Pulse's _rate_limits shape.

    Skips buckets with status_code 3 (un-used-this-period, e.g. video when
    the user hasn't generated any video).
    """
    out = {}
    if not isinstance(mmx_json, dict):
        return out
    for entry in mmx_json.get("model_remains", []):
        if not isinstance(entry, dict) or entry.get("current_interval_status") == 3:
            continue
        interval_left = entry.get("current_interval_remaining_percent")
        weekly_left   = entry.get("current_weekly_remaining_percent")
        end_ms        = entry.get("end_time")
        weekly_end_ms = entry.get("weekly_end_time")
        # Guard each bucket independently: a malformed percent or timestamp
        # in one window must not lose the other (or crash the status line).
        if interval_left is not None and end_ms:
            try:
                out["five_hour"] = {
                    "utilization": 100.0 - float(interval_left),
                    "resets_at":   datetime.fromtimestamp(float(end_ms) / 1000, tz=timezone.utc).isoformat(),
                }
            except (ValueError, TypeError, OSError, OverflowError):
                pass
        if weekly_left is not None and weekly_end_ms:
            try:
                out["seven_day"] = {
                    "utilization": 100.0 - float(weekly_left),
                    "resets_at":   datetime.fromtimestamp(float(weekly_end_ms) / 1000, tz=timezone.utc).isoformat(),
                }
            except (ValueError, TypeError, OSError, OverflowError):
                pass
        break
    return out


# ---------------------------------------------------------------------------
# Providers: other subscriptions as extra status rows (3.4.0)
# ---------------------------------------------------------------------------
# One row per *other* subscription: a second Claude account (another
# CLAUDE_CONFIG_DIR), Codex CLI, Gemini through Antigravity CLI, MiniMax, or
# any JSON file a script keeps up to date. Every source is a local file except
# two: a non-active Claude account costs one /api/oauth/usage call at most
# every providers_refresh_seconds (and none while that account has a window of
# its own open, because each window seeds its own account's entry from stdin),
# and MiniMax runs `mmx quota show`, which is why it is never enabled by
# default.
#
# Another account's OAuth token is only ever read, never refreshed: a refresh
# rotates the refresh token and would log that account's running session out.
# Nothing in this block can raise into the status line; every source degrades
# to "no data".

PROVIDER_TYPES = ("claude", "codex", "gemini", "minimax", "file")
PROVIDER_DEFAULT_LABELS = {
    "claude": "Claude", "codex": "Codex", "gemini": "Gemini",
    "minimax": "MiniMax", "file": "Custom",
}
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,31}$")
_PROVIDER_LABEL_MAX = 24
_MAX_PROVIDERS = 8
_MAX_PROVIDER_ROWS = 6
_PROVIDER_REFRESH_DEFAULT = 300   # config providers_refresh_seconds
_PROVIDER_REFRESH_MIN = 60
# Minimum gap between two refresh *attempts* for one account, whatever their
# outcome. Claude Code cancels an in-flight status-line run when a newer
# repaint arrives; the attempt is stamped before the request so a cancelled
# run cannot retry on the very next repaint.
_PROVIDER_RETRY = 60
_PROVIDER_TIMEOUT = 3             # seconds; the repaint waits on this
_PROVIDER_AUTH_BACKOFF = 900      # 401/403: the other window will refresh it
_PROVIDER_ERROR_BACKOFF = 300     # network/other errors
_PROVIDER_STALE_AFTER = 1800      # polled rows older than this render "~29%"
_PROVIDER_FILE_MAX = 1_000_000    # bytes; agy / custom JSON files
_CODEX_TAIL_BYTES = 2_000_000     # rollouts reach ~100 MB; read only the tail
_CODEX_DAY_DIRS = 2
_CODEX_MAX_FILES = 3
_MINIMAX_PROVIDER_TTL = 120
_MINIMAX_TIMEOUT = 3              # seconds; hard ceiling on one `mmx quota show`
# Hard wall-clock ceiling on a second Claude account's usage fetch. Kept at the
# render path's fetch budget so it fits inside the shortest refresh interval.
_PROVIDER_FETCH_BUDGET = _NET_FETCH_BUDGET
_PROVIDER_RETRY_JITTER = 8        # seconds; spread so accounts' retries differ
_GEMINI_BUCKETS = {
    "gemini": ("gemini-5h", "gemini-weekly"),
    "3p": ("3p-5h", "3p-weekly"),   # Claude/GPT-OSS models inside Antigravity
}
_GEMINI_STATUS_FILE = "statusline_last_live.json"
_ACCOUNTS_FILE_NAME = ".claude-accounts.json"


def _normalize_provider_id(value):
    """Return a lower-cased provider id, or None if it is not a valid one."""
    if not isinstance(value, str):
        return None
    pid = value.strip().lower()
    return pid if _PROVIDER_ID_RE.match(pid) else None


def _clean_provider_label(value):
    """Sanitise a user- or file-supplied label: no escapes, one line, <=24 chars."""
    if not isinstance(value, str):
        return None
    text = " ".join(_sanitize(value).split())[:_PROVIDER_LABEL_MAX].strip()
    return text or None


def _coerce_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    return default


def _normalize_providers(raw):
    """Validate ``config["providers"]``. Never raises; invalid items are dropped.

    Each kept entry is a fresh dict with only known keys::

        {"id": "claude:b", "type": "claude", "enabled": True,
         "label": "Claude (Sunny)",        # optional, sanitised, <=24 chars
         "dir": "~/.claude-b"}              # claude: "dir"; others: "path"
        {"id": "gemini", "type": "gemini", "enabled": True, "bucket": "3p"}

    Ids are lower-cased and must match ``[a-z0-9][a-z0-9:_-]{0,31}``; the first
    of two duplicate ids wins; at most eight entries are kept. A ``file`` entry
    without a ``path`` is dropped. Paths are stored as written (``~`` intact)
    and expanded only at use.
    """
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if len(out) >= _MAX_PROVIDERS:
            break
        try:
            if not isinstance(item, dict):
                continue
            ptype = item.get("type")
            if ptype not in PROVIDER_TYPES:
                continue
            if "id" in item:
                pid = _normalize_provider_id(item.get("id"))
            else:
                pid = ptype if ptype in ("codex", "gemini", "minimax") else None
            if not pid or pid in seen:
                continue
            entry = {"id": pid, "type": ptype,
                     "enabled": _coerce_bool(item.get("enabled", True))}
            label = _clean_provider_label(item.get("label"))
            if label:
                entry["label"] = label
            key = "dir" if ptype == "claude" else "path"
            loc = item.get(key)
            if loc is None and ptype == "claude":
                loc = item.get("path")
            if isinstance(loc, str) and loc.strip() and len(loc) <= 1024 \
                    and "\x00" not in loc:
                entry[key] = loc.strip()
            elif ptype == "file":
                continue
            if ptype == "gemini" and item.get("bucket") == "3p":
                entry["bucket"] = "3p"
            seen.add(pid)
            out.append(entry)
        except Exception:
            continue
    return out


_PATH_KEY_MEMO = {}


def _path_key(path):
    """Comparable form of a path: resolved, case-folded where the OS is.

    Memoised per process: one repaint compares the same handful of dirs
    several times, and ``resolve()`` is a syscall per call on Windows.
    """
    raw = os.path.expanduser(str(path))
    key = _PATH_KEY_MEMO.get(raw)
    if key is None:
        try:
            resolved = Path(raw).resolve()
        except (OSError, RuntimeError, ValueError):
            resolved = Path(raw)
        key = os.path.normcase(str(resolved))
        if len(_PATH_KEY_MEMO) > 256:
            _PATH_KEY_MEMO.clear()
        _PATH_KEY_MEMO[raw] = key
    return key


def _tilde(path):
    """Display form of a path: ``~/...`` with forward slashes when under home."""
    try:
        p = Path(os.path.expanduser(str(path)))
        rel = p.relative_to(Path.home())
        return "~/" + rel.as_posix() if str(rel) != "." else "~"
    except (ValueError, OSError, RuntimeError):
        return str(path)


def _provider_suffix(pid):
    return pid.split(":", 1)[1] if ":" in pid else ""


def _codex_home(entry=None):
    loc = (entry or {}).get("path")
    if loc:
        return Path(os.path.expanduser(loc))
    env = os.environ.get("CODEX_HOME", "").strip()
    return Path(os.path.expanduser(env)) if env else Path.home() / ".codex"


def _provider_location(entry):
    """Where a provider's data lives (a dir or file Path), or None."""
    ptype = entry.get("type")
    loc = entry.get("dir") or entry.get("path")
    if ptype == "codex":
        return _codex_home(entry)
    if loc:
        return Path(os.path.expanduser(loc))
    suffix = _provider_suffix(entry.get("id", ""))
    if ptype == "claude":
        if not suffix or suffix == "default":
            return Path.home() / ".claude"
        return Path.home() / f".claude-{suffix}"
    if ptype == "gemini":
        name = "antigravity-cli" + (f"-{suffix}" if suffix else "")
        return Path.home() / ".gemini" / name
    return None


def _claude_provider_id_for_dir(path):
    """``~/.claude`` -> ``claude:default``; ``~/.claude-b`` -> ``claude:b``."""
    if _path_key(path) == _path_key(Path.home() / ".claude"):
        return "claude:default"
    name = Path(os.path.expanduser(str(path))).name
    suffix = name[len(".claude-"):] if name.lower().startswith(".claude-") else name.lstrip(".")
    suffix = re.sub(r"[^a-z0-9_-]", "", suffix.lower())[:24].strip("-_") or "other"
    return "claude:" + suffix


_ACCOUNT_LABELS_MEMO = {}


def _read_accounts_labels():
    """Map path keys to labels from ``~/.claude-accounts.json`` (if present).

    That file (written by account-switcher scripts) looks like
    ``{"accounts": {"a": {"dir": "~/.claude", "label": "Work", ...}}}``. Only
    ``dir`` and ``label`` are read; any e-mail or name field is ignored.
    """
    path = Path.home() / _ACCOUNTS_FILE_NAME
    try:
        st = path.stat()
        memo_key = (str(path), st.st_mtime, st.st_size)
    except OSError:
        return {}
    if memo_key in _ACCOUNT_LABELS_MEMO:
        return _ACCOUNT_LABELS_MEMO[memo_key]
    labels = {}
    try:
        if st.st_size <= 256_000:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            accounts = data.get("accounts") if isinstance(data, dict) else None
            if isinstance(accounts, dict):
                for acct in accounts.values():
                    if not isinstance(acct, dict) or not isinstance(acct.get("dir"), str):
                        continue
                    label = acct.get("label")
                    labels[_path_key(acct["dir"])] = (
                        _clean_provider_label(label) if isinstance(label, str) else "")
    except (OSError, ValueError, TypeError, AttributeError):
        labels = {}
    _ACCOUNT_LABELS_MEMO.clear()
    _ACCOUNT_LABELS_MEMO[memo_key] = labels
    return labels


def _default_claude_label(path):
    """``Claude`` / ``Claude (Work)`` / ``Claude (b)`` for a config dir."""
    hint = _read_accounts_labels().get(_path_key(path))
    if hint is not None:
        low = (hint or "").lower()
        if not hint or low == "claude" or low.startswith("account "):
            return "Claude"
        return f"Claude ({hint[:_PROVIDER_LABEL_MAX - 9]})"
    pid = _claude_provider_id_for_dir(path)
    suffix = _provider_suffix(pid)
    if suffix == "default":
        return "Claude"
    return f"Claude ({suffix[:_PROVIDER_LABEL_MAX - 9]})"


def _provider_default_label(entry):
    ptype = entry.get("type")
    suffix = _provider_suffix(entry.get("id", ""))
    if ptype == "claude":
        loc = _provider_location(entry)
        return _default_claude_label(loc) if loc else "Claude"
    if ptype == "gemini":
        return f"Gemini ({suffix[:15]})" if suffix else "Gemini"
    if ptype == "file":
        return _clean_provider_label(suffix) or PROVIDER_DEFAULT_LABELS["file"]
    return PROVIDER_DEFAULT_LABELS.get(ptype, "Provider")


def _active_provider_id(config, entries=None):
    """Id of the ``claude`` entry that is this window's own account, or None."""
    if entries is None:
        entries = _normalize_providers((config or {}).get("providers"))
    try:
        active = _path_key(_claude_config_dir())
    except Exception:
        return None
    for entry in entries:
        if entry.get("type") != "claude":
            continue
        loc = _provider_location(entry)
        if loc is not None and _path_key(loc) == active:
            return entry["id"]
    return None


def _provider_refresh_seconds(config):
    try:
        value = int((config or {}).get("providers_refresh_seconds", _PROVIDER_REFRESH_DEFAULT))
    except (TypeError, ValueError):
        value = _PROVIDER_REFRESH_DEFAULT
    return max(_PROVIDER_REFRESH_MIN, value)


# --- provider cache (shared by every account's windows) --------------------

def _provider_state_dir(create=False):
    """``<state>/providers`` — deliberately shared across accounts: the numbers
    describe *other* subscriptions, and one cache means one fetch per TTL for
    every window on the machine. Created on first write only."""
    d = get_state_dir() / "providers"
    if create:
        _secure_mkdir(d)
    return d


def _provider_cache_path(pid, create=False):
    # Ids never contain "." and never start with one, so ":" -> "." is
    # collision-free and keeps the name filesystem-safe everywhere.
    name = _safe_path_id(str(pid).replace(":", ".")) or "provider"
    return _provider_state_dir(create) / f"{name}.json"


def _read_provider_cache(pid):
    try:
        path = _provider_cache_path(pid)
        if path.stat().st_size > 256_000:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


_PROVIDER_CACHE_FIELDS = (
    "timestamp", "usage", "src", "plan", "plan_label", "plan_label_at", "last_attempt",
    "rate_limited_until", "rate_limit_fails", "auth_failed_until", "error_until",
)


def _write_provider_cache(pid, data):
    """Persist only known fields (never a token) via an atomic write.

    Returns True when the file was written; a caller that is about to make a
    network request must not when the attempt stamp cannot be saved.
    """
    try:
        clean = {k: data[k] for k in _PROVIDER_CACHE_FIELDS
                 if k in data and data[k] is not None}
        if "usage" in clean:
            clean["usage"] = _clean_provider_usage(clean["usage"])
        _atomic_json_write(_provider_cache_path(pid, create=True), clean, indent=None)
        return True
    except Exception:
        return False


_PROVIDER_LOCK_STALE = 30   # seconds; an older lock belongs to a killed run


def _acquire_provider_lock(pid):
    """Take ``<cache>.lock`` for one provider; None when another process holds it.

    Two windows repainting in the same second must not both call the usage
    API for the same account: whoever creates the lock file (O_EXCL) fetches,
    the other keeps its cached numbers. A lock older than
    ``_PROVIDER_LOCK_STALE`` was left by a killed run and is broken.
    """
    path = _provider_cache_path(pid, create=True).with_suffix(".lock")
    for _attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            return path
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                return None
            if age <= _PROVIDER_LOCK_STALE:
                return None
            try:
                path.unlink()
            except OSError:
                return None
        except OSError:
            return None
    return None


def _release_provider_lock(path):
    try:
        os.unlink(str(path))
    except OSError:
        pass


# --- time / window helpers --------------------------------------------------

def _iso_z(value):
    """Normalise ISO 8601 for ``datetime.fromisoformat`` on Python 3.8-3.10.

    Those versions reject a trailing ``Z`` (agy writes ``...T06:13:21Z``) and
    accept only 3 or 6 fractional digits.
    """
    s = str(value).strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    m = re.search(r"\.(\d+)", s)
    if m:
        s = s[:m.start()] + "." + (m.group(1) + "000000")[:6] + s[m.end():]
    return s


def _parse_reset(value):
    """Epoch seconds/ms (number or numeric string) or ISO 8601 -> aware datetime."""
    if value is None or isinstance(value, bool):
        return None
    epoch = None
    if isinstance(value, (int, float)):
        epoch = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s or len(s) > 64:
            return None
        try:
            epoch = float(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(_iso_z(s))
            except ValueError:
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    else:
        return None
    if not math.isfinite(epoch) or epoch <= 0:
        return None
    if epoch > 1e11:  # milliseconds
        epoch /= 1000.0
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_from_epoch(seconds):
    dt = _parse_reset(seconds)
    return dt.isoformat() if dt else None


def _provider_window(pct, reset, now=None):
    """Build ``{"utilization", "resets_at"}``; None when ``pct`` is unusable.

    A reset already in the past means the window rolled over since the source
    was written: 0 % and no reset ("starts on next use").
    """
    if pct is None or isinstance(pct, bool):
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pct):
        return None
    pct = max(0.0, min(100.0, pct))
    now = time.time() if now is None else now
    dt = _parse_reset(reset)
    if dt is not None:
        try:
            if dt.timestamp() <= now:
                return {"utilization": 0.0, "resets_at": None}
        except (OverflowError, OSError, ValueError):
            dt = None
    return {"utilization": pct, "resets_at": dt.isoformat() if dt else None}


def _clean_provider_usage(usage, now=None):
    """Keep only well-formed ``five_hour`` / ``seven_day`` windows.

    Also applies the roll-over rule, so a cached window whose reset has since
    passed reads 0 %.
    """
    if not isinstance(usage, dict):
        return {}
    out = {}
    for key in ("five_hour", "seven_day"):
        w = usage.get(key)
        if not isinstance(w, dict):
            continue
        win = _provider_window(w.get("utilization"), w.get("resets_at"), now)
        if win:
            out[key] = win
    return out


def _read_small_json(path, limit=_PROVIDER_FILE_MAX):
    """Return ``(data, stat)``; data is None for an oversized file."""
    st = os.stat(path)
    if st.st_size > limit:
        return None, st
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f), st


# --- Claude (another CLAUDE_CONFIG_DIR) -------------------------------------

def _plan_from_tier(tier):
    if not isinstance(tier, str) or not tier:
        return None
    name = PLAN_NAMES.get(tier) or tier.replace("default_claude_", "").replace("_", " ").title()
    return _clean_provider_label(name)


def _read_other_claude_token(config_dir, now=None):
    """Return ``(token, plan)`` from ``<dir>/.credentials.json``, or ``(None, plan)``.

    File only: no Keychain, no environment token. A token expiring within 30 s
    (or with no expiry recorded) is treated as unusable. This function must
    never refresh: rotating another account's refresh token logs out the
    session that account is running in.
    """
    now = time.time() if now is None else now
    path = Path(os.path.expanduser(str(config_dir))) / ".credentials.json"
    try:
        data, _ = _read_small_json(path, limit=64_000)
    except (OSError, ValueError):
        return None, None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None, None
    plan = _plan_from_tier(oauth.get("rateLimitTier"))
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None, plan
    try:
        expires = float(oauth.get("expiresAt") or 0) / 1000.0
    except (TypeError, ValueError):
        expires = 0.0
    if expires <= now + 30:
        return None, plan
    return token, plan


def _provider_retry_seconds(pid):
    """Minimum gap between two refresh attempts for *pid*.

    ``_PROVIDER_RETRY`` plus a small offset taken from the id: it is stable for
    one account but differs between accounts, so their retries (and the main
    fetch's, which keeps the plain 60 s) stop falling due on the same repaint.
    """
    return _PROVIDER_RETRY + sum(str(pid).encode("utf-8", "replace")) % _PROVIDER_RETRY_JITTER


def _call_bounded(fn, budget):
    """Return ``fn()``, giving up after *budget* seconds of wall-clock time.

    A socket timeout restarts for every address and ``getaddrinfo`` has none at
    all, so neither promises a ceiling (see ``_fetch_bounded``). ``fn`` runs on
    a daemon thread that is simply abandoned at the ceiling: the process exits
    moments later and takes it along, and a late answer is dropped. What ``fn``
    raises is raised here (a ``BaseException`` too); running out of time raises
    ``TimeoutError``.
    """
    outcome = []

    def work():
        try:
            outcome.append((True, fn()))
        except BaseException as exc:  # handed back to the caller below
            outcome.append((False, exc))

    worker = threading.Thread(target=work, daemon=True)
    worker.start()          # RuntimeError (no thread to spare) is the caller's error
    worker.join(budget)
    if not outcome:
        raise TimeoutError("no answer within %.1fs" % budget)
    ok, value = outcome[0]
    if ok:
        return value
    raise value


def _refresh_claude_provider(entry, config, now=None, budget=None):
    """Return the cache for a non-active Claude account, refreshing it if due.

    Synchronous on purpose (one short request, no child process). A request
    is made only when all of these hold: the data is older than
    ``providers_refresh_seconds``; no 429 / auth / error backoff is running;
    the last *attempt* is over 60 s old; the account has an unexpired token on
    disk; ``budget`` (a one-item list shared by one repaint) still allows
    a network call; and the repaint has not already made one (the main usage
    fetch or any other check: ``_begin_net_attempt`` lets the optional checks
    take turns, so this one simply waits for a later repaint). The attempt is
    stamped before the request, and the request has a hard wall-clock ceiling.
    """
    now = time.time() if now is None else now
    pid = entry["id"]
    retry = _provider_retry_seconds(pid)
    cache = _read_provider_cache(pid) or {}

    def _num(key):
        try:
            return float(cache.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    if now - _num("timestamp") < _provider_refresh_seconds(config):
        return cache
    if now < _num("rate_limited_until") or now < _num("auth_failed_until") \
            or now < _num("error_until"):
        return cache
    if now - _num("last_attempt") < retry:
        return cache
    if budget is not None and budget[0] <= 0:
        return cache
    loc = _provider_location(entry)
    if loc is None:
        return cache
    token, plan = _read_other_claude_token(loc, now)
    if not token:
        return cache
    lock = _acquire_provider_lock(pid)
    if lock is None:
        return cache            # another window is fetching this account now
    try:
        # Re-read under the lock: that window may just have stamped or refreshed.
        cache = _read_provider_cache(pid) or cache
        if now - _num("timestamp") < _provider_refresh_seconds(config) \
                or now - _num("last_attempt") < retry:
            return cache
        # One network call per repaint: when the main usage fetch (or another
        # optional check) has just run, wait for a later repaint. Waiting is
        # not failing, so nothing is stamped and no budget is spent.
        net_key = "provider:%s" % pid
        if not _begin_net_attempt(net_key, backoff=retry):
            return cache
        if budget is not None:
            budget[0] -= 1
        cache = dict(cache)
        cache["last_attempt"] = now
        if not _write_provider_cache(pid, cache):
            _end_net_attempt(net_key)
            return cache        # unsaved attempt: never risk one fetch per repaint
        try:
            usage = _call_bounded(
                lambda tok=token: fetch_usage(tok, timeout=_PROVIDER_TIMEOUT),
                _PROVIDER_FETCH_BUDGET)
            fresh = {"timestamp": time.time(), "usage": _clean_provider_usage(usage),
                     "plan": plan or cache.get("plan"), "last_attempt": now}
            # The plan's sold name, once a day while the token is in hand and
            # only when tiers are drawn; a failed look-up waits an hour.
            label_at = _num("plan_label_at")
            if isinstance(cache.get("plan_label"), str) and cache.get("plan_label"):
                fresh["plan_label"], fresh["plan_label_at"] = cache["plan_label"], label_at
            if _plan_shown(config) and now - label_at >= _PLAN_LABEL_TTL:
                try:
                    label = _call_bounded(lambda tok=token: fetch_plan_label(tok),
                                          _PROVIDER_FETCH_BUDGET)
                except BaseException:
                    label = None
                if label:
                    fresh["plan_label"], fresh["plan_label_at"] = label, now
                else:
                    fresh["plan_label_at"] = now - _PLAN_LABEL_TTL + _PLAN_LABEL_RETRY
            token = None
            _end_net_attempt(net_key)
            _write_provider_cache(pid, fresh)
            return fresh
        except urllib.error.HTTPError as exc:
            _end_net_attempt(net_key)
            code = getattr(exc, "code", None)
            if code == 429:
                retry_after = _retry_after_seconds(getattr(exc, "headers", None))
                delay, fails = _rate_limit_backoff(cache.get("rate_limit_fails"))
                cache["rate_limited_until"] = now + max(delay, retry_after)
                cache["rate_limit_fails"] = fails
            elif code in (401, 403):
                # Do not refresh: the window running that account will.
                cache["auth_failed_until"] = now + _PROVIDER_AUTH_BACKOFF
            else:
                cache["error_until"] = now + _PROVIDER_ERROR_BACKOFF
        except Exception:
            _end_net_attempt(net_key)
            cache["error_until"] = now + _PROVIDER_ERROR_BACKOFF
        token = None
        _write_provider_cache(pid, cache)
        return cache
    finally:
        _release_provider_lock(lock)


def _seed_active_provider_cache(usage, config, now=None):
    """Write this window's own stdin windows into its ``claude:<key>`` cache.

    Each window knows its own account's usage for free (Claude Code puts it
    on stdin), so with both accounts' windows open the API is never called
    for either row. Only runs when the user has providers configured.
    """
    try:
        if not isinstance(config, dict) or not config.get("providers"):
            return
        now = time.time() if now is None else now
        clean = _clean_provider_usage(usage, now)
        if not clean:
            return
        pid = _active_provider_id(config) or _claude_provider_id_for_dir(_claude_config_dir())
        prev = _read_provider_cache(pid) or {}
        merged = _clean_provider_usage(prev.get("usage"), now)
        merged.update(clean)
        try:
            prev_ts = float(prev.get("timestamp") or 0)
        except (TypeError, ValueError):
            prev_ts = 0.0
        if prev.get("usage") == merged and now - prev_ts < 60:
            return  # unchanged and recent: skip the write
        data = {k: prev[k] for k in ("plan", "plan_label", "plan_label_at",
                                     "rate_limited_until", "rate_limit_fails")
                if k in prev}
        # This window knows its own plan's sold name; the other window's row
        # for this account should say the same.
        own = _read_plan_label().get("label")
        if isinstance(own, str) and own:
            data["plan_label"] = own
        data.update({"timestamp": now, "usage": merged, "src": "stdin"})
        _write_provider_cache(pid, data)
    except Exception:
        pass


# --- Gemini / Antigravity CLI ----------------------------------------------

def _read_gemini_quota(path, bucket="gemini", now=None):
    """Read agy's ``statusline_last_live.json`` (``path`` = its dir or the file).

    ``quota`` maps bucket ids to ``{remaining_fraction, reset_time}``;
    utilisation is ``(1 - remaining_fraction) * 100``. ``bucket`` "gemini"
    reads ``gemini-5h``/``gemini-weekly``, "3p" the third-party-model pool.
    Unknown buckets are ignored. The file is undocumented, so any surprise
    reads as no data. Returns ``{"usage", "data_ts", "tier"}`` or None.
    """
    try:
        p = Path(os.path.expanduser(str(path)))
        if p.suffix.lower() != ".json":
            p = p / _GEMINI_STATUS_FILE
        data, st = _read_small_json(p)
        if not isinstance(data, dict):
            return None
        quota = data.get("quota")
        quota = quota if isinstance(quota, dict) else {}
        five_key, week_key = _GEMINI_BUCKETS.get(bucket, _GEMINI_BUCKETS["gemini"])
        usage = {}
        for out_key, key in (("five_hour", five_key), ("seven_day", week_key)):
            b = quota.get(key)
            if not isinstance(b, dict):
                continue
            try:
                remaining = float(b.get("remaining_fraction"))
            except (TypeError, ValueError):
                continue
            win = _provider_window((1.0 - remaining) * 100.0, b.get("reset_time"), now)
            if win:
                usage[out_key] = win
        tier = _clean_provider_label(data.get("plan_tier"))
        return {"usage": usage, "data_ts": st.st_mtime, "tier": tier}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


# --- Codex CLI --------------------------------------------------------------

def _numbered_subdirs(path, digits):
    try:
        names = os.listdir(path)
    except OSError:
        return []
    return sorted((n for n in names if len(n) == digits and n.isascii() and n.isdigit()),
                  reverse=True)


def _find_codex_rollouts(codex_home, limit=_CODEX_MAX_FILES):
    """Newest ``rollout-*.jsonl`` files, newest first.

    Only the two newest ``sessions/YYYY/MM/DD`` day dirs are listed (names sort
    chronologically), so the cost is a handful of listdirs however large the
    tree grows. Two days covers a session started yesterday and still going.
    """
    root = Path(codex_home) / "sessions"
    days = []
    for y in _numbered_subdirs(root, 4):
        for m in _numbered_subdirs(root / y, 2):
            for d in _numbered_subdirs(root / y / m, 2):
                days.append(root / y / m / d)
                if len(days) >= _CODEX_DAY_DIRS:
                    break
            if len(days) >= _CODEX_DAY_DIRS:
                break
        if len(days) >= _CODEX_DAY_DIRS:
            break
    files = []
    for day in days:
        try:
            with os.scandir(day) as it:
                for e in it:
                    if e.name.startswith("rollout-") and e.name.endswith(".jsonl") \
                            and e.is_file():
                        files.append((e.stat().st_mtime, e.path))
        except OSError:
            continue
    files.sort(reverse=True)
    return [Path(p) for _, p in files[:limit]]


def _find_codex_rollout(codex_home):
    found = _find_codex_rollouts(codex_home, 1)
    return found[0] if found else None


def _codex_usage_from_rate_limits(rl, now=None):
    """Map a Codex ``RateLimitSnapshot`` to five_hour / seven_day windows.

    Every field is optional upstream. A window is placed by its
    ``window_minutes`` (<= 1 day -> five_hour) and by position otherwise.
    """
    usage = {}
    for key, fallback in (("primary", "five_hour"), ("secondary", "seven_day")):
        w = rl.get(key)
        if not isinstance(w, dict):
            continue
        try:
            minutes = float(w.get("window_minutes"))
        except (TypeError, ValueError):
            minutes = None
        target = fallback if not minutes else ("five_hour" if minutes <= 1440 else "seven_day")
        if target in usage:
            continue
        win = _provider_window(w.get("used_percent"), w.get("resets_at"), now)
        if win:
            usage[target] = win
    return usage


def _codex_rate_limits_from_tail(path, now=None):
    """Newest usable ``rate_limits`` in the last 2 MB of a rollout, or None.

    Line shape: ``{"type": "event_msg", "payload": {"type": "token_count",
    "rate_limits": {"limit_id": "codex", "primary": {...}, "secondary":
    {...}, "plan_type": "plus"}}}``. ``limit_id`` values other than "codex"
    (e.g. "premium") describe a different pool and are skipped.
    """
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        start = max(0, size - _CODEX_TAIL_BYTES)
        fh.seek(start)
        blob = fh.read(_CODEX_TAIL_BYTES)
    lines = blob.decode("utf-8", "ignore").split("\n")
    if start > 0 and lines:
        lines = lines[1:]  # first line is partial
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        rl = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(rl, dict) or rl.get("limit_id") not in (None, "codex"):
            continue
        usage = _codex_usage_from_rate_limits(rl, now)
        if not usage:
            continue
        return usage, _codex_tier_label(rl.get("plan_type"))
    return None


def _codex_tier_label(plan):
    """``"plus"`` -> ``"Plus"``; None for anything that is not a string."""
    if not isinstance(plan, str):
        return None
    return _clean_provider_label(plan.replace("_", " ").title())


def _read_codex_quota(codex_home, cached=None, now=None):
    """Codex windows from the newest rollouts, cached on every candidate
    file's path + mtime + size, so a quiet tree costs a few stats and no read.

    Every candidate counts, not just the newest: a brand-new session has no
    rate limits yet, so the numbers come from an older, still-running one,
    and a change to that file must invalidate the cache too.

    Returns ``{"usage", "data_ts", "tier", "src", "changed"}`` or None when
    there are no rollouts. ``changed`` tells the caller to rewrite the cache.
    """
    files = _find_codex_rollouts(codex_home)
    if not files:
        return None
    src = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        src.append({"path": str(f), "mtime": st.st_mtime, "size": st.st_size})
    if not src:
        return None
    if isinstance(cached, dict) and cached.get("src") == src:
        return {"usage": _clean_provider_usage(cached.get("usage"), now),
                "data_ts": cached.get("timestamp"), "tier": cached.get("plan"),
                "src": src, "changed": False}
    for f in files:
        try:
            f_mtime = f.stat().st_mtime
            found = _codex_rate_limits_from_tail(f, now)
        except OSError:
            continue
        if found:
            usage, tier = found
            return {"usage": usage, "data_ts": f_mtime, "tier": tier,
                    "src": src, "changed": True}
    return {"usage": {}, "data_ts": None, "tier": None, "src": src, "changed": True}


# --- Codex (ChatGPT usage API) ----------------------------------------------
# The Codex CLI signs in with a ChatGPT login and asks chatgpt.com for its
# rate limits. The same request from here sees the whole pool, which is shared
# with the web app and every other machine on the account, so it is the first
# choice and the local rollout files are the fallback. The CLI's token goes to
# chatgpt.com only (its own allowlist and redirect guard, separate from the
# Anthropic one), is never refreshed (OpenAI rotates the refresh token: a
# refresh here would sign the CLI out) and is never written to any cache.
_CODEX_TOKEN_DOMAINS = frozenset({"chatgpt.com"})
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class _CodexNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would forward the bearer header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "Codex usage redirect blocked", headers, fp)


_codex_opener = urllib.request.build_opener(_CodexNoRedirectHandler)


def _read_codex_token(codex_home, now=None):
    """``(access_token, account_id, plan_type)`` from ``<codex_home>/auth.json``.

    All None when the file is missing, the login is an API key (no
    ``tokens``) or the token's JWT ``exp`` is within 60 s: the Codex CLI
    renews its token on its next use, this never does. ``plan_type`` is the
    token's ``chatgpt_plan_type`` claim ("plus"), None when absent.
    """
    now = time.time() if now is None else now
    try:
        data, _ = _read_small_json(Path(codex_home) / "auth.json", limit=64_000)
    except (OSError, ValueError, TypeError):
        return None, None, None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        return None, None, None
    plan = None
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        if float(claims["exp"]) <= now + 60:
            return None, None, None
        auth = claims.get("https://api.openai.com/auth")
        if isinstance(auth, dict) and isinstance(auth.get("chatgpt_plan_type"), str):
            plan = auth["chatgpt_plan_type"]
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        pass  # not a JWT, or no expiry in it: let the server judge the token
    account = tokens.get("account_id")
    return token, (account if isinstance(account, str) else None), plan


def _fetch_codex_usage(token, account_id, now=None):
    """GET the usage endpoint the Codex CLI uses; returns ``(usage, tier)``.

    ``rate_limit.primary_window`` / ``secondary_window`` carry ``used_percent``,
    ``limit_window_seconds`` and ``reset_at`` (epoch seconds, with
    ``reset_after_seconds`` as the fallback); ``plan_type`` names the plan.
    Raises on HTTP errors and on any other payload shape.
    """
    now = time.time() if now is None else now
    if urlparse(_CODEX_USAGE_URL).hostname not in _CODEX_TOKEN_DOMAINS:
        raise ValueError("Codex token request blocked")
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": f"usage-pulse/{VERSION}"}
    if account_id:
        hdrs["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(_CODEX_USAGE_URL, headers=hdrs)
    with _codex_opener.open(req, timeout=_PROVIDER_TIMEOUT) as resp:
        data = json.loads(resp.read(256_000))
    rl = data.get("rate_limit") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        raise ValueError("unexpected Codex usage payload")
    snapshot = {}
    for src, dst in (("primary_window", "primary"), ("secondary_window", "secondary")):
        w = rl.get(src)
        if not isinstance(w, dict):
            continue
        secs = w.get("limit_window_seconds")
        minutes = secs / 60 if isinstance(secs, (int, float)) and not isinstance(secs, bool) else None
        reset = w.get("reset_at")
        after = w.get("reset_after_seconds")
        if reset is None and isinstance(after, (int, float)) and not isinstance(after, bool):
            reset = now + after
        snapshot[dst] = {"used_percent": w.get("used_percent"),
                         "window_minutes": minutes, "resets_at": reset}
    return _codex_usage_from_rate_limits(snapshot, now), _codex_tier_label(data.get("plan_type"))


def _refresh_codex_api(entry, config, now=None, budget=None):
    """The ``<id>:api`` cache for a Codex row, refreshed when due.

    Same gates as ``_refresh_claude_provider``: the data is older than
    ``providers_refresh_seconds``; no 429 / auth / error backoff is running;
    the last *attempt* is over 60 s old; a usable token is on disk; and
    ``budget`` (one network call per repaint, shared with the Claude rows)
    still allows it. The attempt is stamped before the request. Returns the
    cache, which may be empty.
    """
    now = time.time() if now is None else now
    pid = entry["id"] + ":api"
    cache = _read_provider_cache(pid) or {}

    def _num(key):
        try:
            return float(cache.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    if now - _num("timestamp") < _provider_refresh_seconds(config):
        return cache
    if now < _num("rate_limited_until") or now < _num("auth_failed_until") \
            or now < _num("error_until"):
        return cache
    if now - _num("last_attempt") < _PROVIDER_RETRY:
        return cache
    if budget is not None and budget[0] <= 0:
        return cache
    loc = _provider_location(entry)
    token, account, jwt_plan = _read_codex_token(loc, now) if loc else (None, None, None)
    if not token:
        return cache
    lock = _acquire_provider_lock(pid)
    if lock is None:
        return cache            # another window is fetching this row now
    try:
        cache = _read_provider_cache(pid) or cache
        if now - _num("timestamp") < _provider_refresh_seconds(config) \
                or now - _num("last_attempt") < _PROVIDER_RETRY:
            return cache
        # One network call per repaint, taking turns with the other optional
        # checks; waiting is not failing, so nothing is stamped.
        net_key = "provider:%s" % pid
        if not _begin_net_attempt(net_key, backoff=_PROVIDER_RETRY):
            return cache
        if budget is not None:
            budget[0] -= 1
        cache = dict(cache)
        cache["last_attempt"] = now
        if not _write_provider_cache(pid, cache):
            _end_net_attempt(net_key)
            return cache        # unsaved attempt: never risk one fetch per repaint
        try:
            usage, tier = _call_bounded(
                lambda tok=token: _fetch_codex_usage(tok, account, now),
                _PROVIDER_FETCH_BUDGET)
            token = None
            _end_net_attempt(net_key)
            if not usage:
                raise ValueError("no Codex windows in the payload")
            fresh = {"timestamp": time.time(), "usage": usage, "last_attempt": now,
                     "plan": tier or _codex_tier_label(jwt_plan) or cache.get("plan")}
            _write_provider_cache(pid, fresh)
            return fresh
        except urllib.error.HTTPError as exc:
            _end_net_attempt(net_key)
            code = getattr(exc, "code", None)
            if code == 429:
                try:
                    retry_after = float(exc.headers.get("retry-after") or 0)
                except (TypeError, ValueError, AttributeError):
                    retry_after = 0.0
                delay, fails = _rate_limit_backoff(cache.get("rate_limit_fails"))
                cache["rate_limited_until"] = now + max(delay, retry_after)
                cache["rate_limit_fails"] = fails
            elif code in (401, 403):
                # Expired or revoked: the Codex CLI renews it on its next use.
                cache["auth_failed_until"] = now + _PROVIDER_AUTH_BACKOFF
            else:
                cache["error_until"] = now + _PROVIDER_ERROR_BACKOFF
        except BaseException:
            _end_net_attempt(net_key)
            cache["error_until"] = now + _PROVIDER_ERROR_BACKOFF
        token = None
        _write_provider_cache(pid, cache)
        return cache
    finally:
        _release_provider_lock(lock)


# --- MiniMax (mmx CLI) ------------------------------------------------------

def _minimax_env_active():
    """True when this window itself runs on MiniMax (mxclaude): the main line
    already shows MiniMax, so no extra row is drawn."""
    return "minimax.io" in os.environ.get("ANTHROPIC_BASE_URL", "") and \
        os.environ.get("ANTHROPIC_MODEL", "").startswith("MiniMax-")


def _minimax_run_quota(exe):
    """Run ``mmx quota show``; return its windows in Pulse's shape, or None.

    npm installs mmx as a ``.cmd`` shim on Windows: a ``cmd.exe`` that starts
    ``node.exe``. On a timeout ``subprocess.run`` kills only the shim, and with
    pipes it then waits for end-of-file on pipes the still-running node holds,
    for as long as MiniMax's API hangs (see ``_git`` with ``network=True``). So
    the call gets no pipes at all: stdout goes to a temp file, stdin and stderr
    to the null device, and ``_MINIMAX_TIMEOUT`` is a real ceiling on every OS.
    """
    import tempfile  # only this path needs it
    try:
        with tempfile.TemporaryFile() as out:
            proc = subprocess.run(
                [exe, "quota", "show", "--output", "json", "--quiet"],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                timeout=_MINIMAX_TIMEOUT,
            )
            if proc.returncode != 0:
                return None
            out.seek(0)
            raw = out.read(_PROVIDER_FILE_MAX)
        return _parse_minimax_quota(json.loads(raw)) or None
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
        return None


def _minimax_cached_windows(cache, now=None):
    """The cached MiniMax windows as the main bar's ``_rate_limits``.

    Empty when there is nothing cached or it is older than
    ``_PROVIDER_STALE_AFTER``: the provider row draws old numbers with a "~",
    but the main bar has no such marker, so it shows no bars instead.
    """
    now = time.time() if now is None else now
    try:
        stamp = float((cache or {}).get("timestamp") or 0)
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(stamp) or stamp <= 0 or now - stamp > _PROVIDER_STALE_AFTER:
        return {}
    return _clean_provider_usage((cache or {}).get("usage"), now)


def _read_minimax_quota(pid="minimax", now=None, primary=False):
    """MiniMax windows via ``mmx quota show`` at most every 120 s (cached).

    One cache serves the provider row and the main bar of an mxclaude window,
    so ``mmx`` (a Node start-up plus a call to MiniMax) never runs on every
    repaint. The attempt is stamped before the call; when it cannot be stamped
    nothing is run. Like the other optional checks it takes turns with them
    (``_begin_net_attempt``), unless ``primary``: on an mxclaude window ``mmx``
    is the usage bar itself, the way the usage fetch is on a Claude one.
    """
    now = time.time() if now is None else now
    cache = _read_provider_cache(pid) or {}
    try:
        last = float(cache.get("last_attempt") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < _MINIMAX_PROVIDER_TTL:
        return cache
    exe = shutil.which("mmx")
    if not exe:
        return cache
    net_key = "provider:%s" % pid
    if not _begin_net_attempt(net_key, backoff=_MINIMAX_PROVIDER_TTL, primary=primary):
        return cache
    cache = dict(cache)
    cache["last_attempt"] = now
    if not _write_provider_cache(pid, cache):
        _end_net_attempt(net_key)
        return cache            # unsaved attempt: never risk one run per repaint
    parsed = _minimax_run_quota(exe)
    _end_net_attempt(net_key)
    if parsed:
        cache["usage"] = parsed
        cache["timestamp"] = now
        _write_provider_cache(pid, cache)
    return cache


# --- file: a JSON file somebody else writes ---------------------------------

def _file_window(w, now=None):
    if not isinstance(w, dict):
        return None
    pct = w.get("used_percentage")
    if pct is None:
        pct = w.get("utilization")
    return _provider_window(pct, w.get("resets_at"), now)


def _read_file_quota(path, now=None):
    """Read a provider row from a JSON file another tool keeps up to date.

    This is the hook for any provider usage-pulse cannot read itself (Kimi,
    GLM, a company gateway...): a tiny script writes the file, usage-pulse
    draws the row. Shape, matching Claude Code's own stdin ``rate_limits``::

        {
          "label": "Kimi",
          "five_hour": {"used_percentage": 42, "resets_at": 1792386351},
          "seven_day": {"used_percentage": 7,  "resets_at": "2026-10-26T10:00:00Z"}
        }

    * The two windows may sit at top level or under ``"rate_limits"``.
    * ``utilization`` is accepted in place of ``used_percentage`` (0-100).
    * ``resets_at`` is epoch seconds (or milliseconds) or ISO 8601; optional.
      A reset in the past reads as 0 % (the window rolled over).
    * Either window may be missing; ``label`` is optional (a label set with
      ``--providers label`` wins). Max 1 MB; UTF-8 (a BOM is fine).

    Returns ``{"usage", "data_ts", "label"}`` or None when unreadable.
    """
    try:
        data, st = _read_small_json(Path(os.path.expanduser(str(path))))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    src = data
    rl = data.get("rate_limits")
    if isinstance(rl, dict) and ("five_hour" in rl or "seven_day" in rl):
        src = rl
    usage = {}
    for key in ("five_hour", "seven_day"):
        win = _file_window(src.get(key), now)
        if win:
            usage[key] = win
    return {"usage": usage, "data_ts": st.st_mtime,
            "label": _clean_provider_label(data.get("label"))}


# --- collection -------------------------------------------------------------

def _provider_row_entries(config):
    """Enabled entries that get a row in this window, in config order (<= 6).

    Skips this window's own account (it is the main line) and MiniMax while
    the window itself runs on MiniMax.
    """
    if not isinstance(config, dict):
        return []
    entries = _normalize_providers(config.get("providers"))
    if not entries:
        return []
    active = _active_provider_id(config, entries)
    rows = []
    for entry in entries:
        if not entry.get("enabled") or entry["id"] == active:
            continue
        if entry["type"] == "minimax" and _minimax_env_active():
            continue
        rows.append(entry)
        if len(rows) >= _MAX_PROVIDER_ROWS:
            break
    return rows


def _provider_row_label(entry):
    """Display label: user label, else the file's own label, else a default."""
    if entry.get("label"):
        return entry["label"]
    if entry.get("type") == "file":
        try:
            loc = _provider_location(entry)
            data, _ = _read_small_json(loc) if loc else (None, None)
            label = _clean_provider_label(data.get("label")) if isinstance(data, dict) else None
            if label:
                return label
        except (OSError, ValueError, TypeError):
            pass
    return _provider_default_label(entry)


def _collect_provider_rows(config, now=None):
    """Data for every provider row: ``[{entry, label, usage, data_ts, stale, tier}]``.

    Each source is read in its own ``try``; a failure yields a row with no
    data (drawn as ``--``) rather than no status line. At most one network
    refresh happens per repaint across all Claude rows.
    """
    now = time.time() if now is None else now
    rows = []
    budget = [1]
    for entry in _provider_row_entries(config):
        ptype = entry["type"]
        usage, data_ts, tier = None, None, None
        try:
            loc = _provider_location(entry)
            if ptype == "claude":
                c = _refresh_claude_provider(entry, config, now, budget)
                usage, data_ts = c.get("usage"), c.get("timestamp")
                tier = c.get("plan_label") or c.get("plan")
            elif ptype == "codex":
                cached = _read_provider_cache(entry["id"])
                r = _read_codex_quota(loc, cached, now)
                if r:
                    usage, data_ts, tier = r["usage"], r["data_ts"], r["tier"]
                    if r.get("changed"):
                        _write_provider_cache(entry["id"], {
                            "timestamp": data_ts, "usage": usage,
                            "src": r["src"], "plan": tier})
                # The API sees the whole shared pool, so its numbers win while
                # fresh, or whenever they are newer than the rollout's. An old
                # rollout only says this machine has been quiet.
                try:
                    api = _refresh_codex_api(entry, config, now, budget)
                except Exception:
                    api = {}
                api_usage = _clean_provider_usage(api.get("usage"), now)
                try:
                    api_ts = float(api.get("timestamp") or 0)
                except (TypeError, ValueError):
                    api_ts = 0.0
                if api_usage and (now - api_ts < _PROVIDER_STALE_AFTER
                                  or api_ts >= float(data_ts or 0)):
                    usage, data_ts = api_usage, api_ts
                    tier = api.get("plan") or tier
            elif ptype == "gemini":
                r = _read_gemini_quota(loc, entry.get("bucket", "gemini"), now) if loc else None
                if r:
                    usage, data_ts, tier = r["usage"], r["data_ts"], r["tier"]
            elif ptype == "minimax":
                c = _read_minimax_quota(entry["id"], now)
                usage, data_ts = c.get("usage"), c.get("timestamp")
            elif ptype == "file":
                r = _read_file_quota(loc, now) if loc else None
                if r:
                    usage, data_ts = r["usage"], r["data_ts"]
        except Exception:
            usage, data_ts, tier = None, None, None
        usage = _clean_provider_usage(usage, now)
        try:
            data_ts = float(data_ts) if data_ts is not None else None
        except (TypeError, ValueError):
            data_ts = None
        # Polled sources go stale, Codex among them: its pool is shared with
        # the web app and other machines, so an old number here is not "no use
        # since". agy / custom files are rewritten on every use, so for them an
        # old file means exactly that.
        stale = bool(ptype in ("claude", "minimax", "codex") and usage and data_ts
                     and now - data_ts > _PROVIDER_STALE_AFTER)
        rows.append({"entry": entry, "label": _provider_row_label(entry),
                     "usage": usage, "data_ts": data_ts, "stale": stale,
                     "tier": _clean_provider_label(tier) if isinstance(tier, str) else None})
    return rows


# ---------------------------------------------------------------------------
# Status line rendering
# ---------------------------------------------------------------------------

def get_theme_colours(theme_name):
    """Return the colour dict for the given theme name."""
    return THEMES.get(theme_name, THEMES["default"])


def bar_colour(pct, theme):
    """Return ANSI colour based on usage percentage using theme colours."""
    if pct >= 80:
        return theme["high"]
    if pct >= 50:
        return theme["mid"]
    return theme["low"]


def _fmt_tokens(n):
    """Format token count: 200000 -> '200k', 1000000 -> '1M', 1B -> '1B'."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.0f}B" if n % 1_000_000_000 == 0 else f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M" if n % 1_000_000 == 0 else f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k" if n % 1_000 == 0 else f"{n / 1_000:.1f}k"
    return str(n)


def make_bar(pct, theme=None, plain=False, width=None, bar_style=None,
             anim_mode="off", flash_color=None, config=None):
    """Build a coloured bar. plain=True returns characters only (no ANSI).

    anim_mode   — animation mode: off/pulse/glow/shift (per-character effects).
    flash_color — override the entire bar with this ANSI colour.
    """
    if theme is None:
        theme = THEMES["default"]
    if width is None:
        width = BAR_SIZES[DEFAULT_BAR_SIZE]
    style = bar_style or DEFAULT_BAR_STYLE
    pct = pct or 0

    if flash_color:
        colour = flash_color
    elif plain:
        colour = None
    else:
        colour = bar_colour(pct, theme)

    # Gradient styles
    gradient_data = BAR_GRADIENT_STYLES.get(style)
    if gradient_data is not None:
        gradient, empty_char = gradient_data
        levels = len(gradient) - 1
        total_steps = width * levels
        filled_steps = round(pct / 100 * total_steps)
        filled_steps = max(0, min(total_steps, filled_steps))
        full = filled_steps // levels
        partial = filled_steps % levels
        empty = width - full - (1 if partial else 0)
        bar_fill = gradient[-1] * full
        bar_partial = gradient[partial] if partial else ""
        bar_empty = empty_char * empty
        if plain:
            return f"{bar_fill}{bar_partial}{DIM}{bar_empty}{RESET}"
        return f"{colour}{bar_fill}{bar_partial}{DIM}{bar_empty}{RESET}"

    # Standard binary fill
    fill_char, empty_char = BAR_STYLES.get(style, BAR_STYLES[DEFAULT_BAR_STYLE])
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    if plain:
        return f"{fill_char * filled}{DIM}{empty_char * (width - filled)}{RESET}"

    # Pulse mode: bars cycle through vivid truecolor hues
    if anim_mode == "pulse" and not plain:
        speed = _get_animation_speed(config) if config else 1.0
        phase = (time.time() * 0.8 * speed) % 1.0
        # 6 vivid colours that are obviously different on any terminal
        pulse_palette = [
            (0, 200, 200),    # cyan
            (80, 120, 255),   # blue
            (180, 80, 220),   # purple
            (220, 80, 120),   # pink
            (200, 160, 40),   # gold
            (40, 200, 120),   # green
        ]
        n = len(pulse_palette)
        scaled = phase * n
        idx = int(scaled) % n
        frac = scaled - int(scaled)
        nxt = (idx + 1) % n
        r = int(pulse_palette[idx][0] + (pulse_palette[nxt][0] - pulse_palette[idx][0]) * frac)
        g = int(pulse_palette[idx][1] + (pulse_palette[nxt][1] - pulse_palette[idx][1]) * frac)
        b = int(pulse_palette[idx][2] + (pulse_palette[nxt][2] - pulse_palette[idx][2]) * frac)
        pulse_col = f"\033[38;2;{r};{g};{b}m"
        return f"{pulse_col}{fill_char * filled}{DIM}{empty_char * (width - filled)}{RESET}"

    # Per-character animation (glow/shift)
    if anim_mode not in ("off", "rainbow", "pulse") and colour and not plain:
        chars = []
        for i in range(filled):
            anim_col = _apply_bar_animation(colour, i, width, anim_mode, config)
            c = anim_col or colour
            chars.append(f"{c}{fill_char}{RESET}")
        for i in range(width - filled):
            chars.append(f"{DIM}{empty_char}{RESET}")
        return "".join(chars)

    return f"{colour}{fill_char * filled}{DIM}{empty_char * (width - filled)}{RESET}"


# ---------------------------------------------------------------------------
# Animation state — threshold flash + celebration effects
# ---------------------------------------------------------------------------

def _get_anim_state_path():
    return get_account_state_dir() / "anim_state.json"


def _load_anim_state():
    """The animation state, or {} when the file is missing, damaged or not an object."""
    _, data = _read_state_json(_get_anim_state_path())
    return data if isinstance(data, dict) else {}


def _save_anim_state(state):
    try:
        _atomic_json_write(_get_anim_state_path(), state, indent=None)
    except OSError:
        pass


def _check_threshold_flash(section, pct, anim_state):
    prev_key = f"prev_pct_{section}"
    flash_key = f"flash_renders_{section}"
    prev_pct = anim_state.get(prev_key, 0)
    crossed = any(prev_pct < t <= pct for t in FLASH_THRESHOLDS)
    anim_state[prev_key] = pct
    if crossed:
        anim_state[flash_key] = FLASH_DURATION
        anim_state[f"flash_time_{section}"] = time.time()
    renders_left = anim_state.get(flash_key, 0)
    if renders_left > 0:
        flash_time = anim_state.get(f"flash_time_{section}", 0)
        if time.time() - flash_time > FLASH_RENDER_TTL:
            anim_state[flash_key] = 0


def _get_flash_color(section, theme, anim_state):
    flash_key = f"flash_renders_{section}"
    renders_left = anim_state.get(flash_key, 0)
    if renders_left > 0:
        flash_time = anim_state.get(f"flash_time_{section}", 0)
        if time.time() - flash_time > FLASH_RENDER_TTL:
            return None
        anim_state[flash_key] = renders_left - 1
        return theme["high"]
    return None


def _check_celebration(weekly_pct, anim_state):
    prev_weekly = anim_state.get("prev_weekly_pct", 0)
    celeb_key = "celebration_renders"
    drop = prev_weekly - weekly_pct
    if drop >= CELEBRATION_DROP_THRESHOLD and prev_weekly >= 30:
        anim_state[celeb_key] = CELEBRATION_DURATION
        anim_state["celebration_time"] = time.time()
    anim_state["prev_weekly_pct"] = weekly_pct
    renders_left = anim_state.get(celeb_key, 0)
    if renders_left > 0:
        celeb_time = anim_state.get("celebration_time", 0)
        if time.time() - celeb_time > FLASH_RENDER_TTL:
            anim_state[celeb_key] = 0
            return False
        anim_state[celeb_key] = renders_left - 1
        return True
    return False


def _render_celebration_label(config=None):
    chars = CELEBRATION_CHAR * 3
    return rainbow_colorize(f" {chars} Reset! {chars} ", color_all=True, shimmer=True, config=config)


def _calc_pace_pct(resets_at_str, window_seconds):
    """Return expected usage % based on elapsed time in the window, or None."""
    if not resets_at_str:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str)
        now = datetime.now(timezone.utc)
        remaining = (resets_at - now).total_seconds()
        elapsed = window_seconds - remaining
        if elapsed <= 0 or window_seconds <= 0:
            return None
        return min(100.0, max(0.0, (elapsed / window_seconds) * 100))
    except Exception:
        return None


# Pace display (--pace-style). "arrows" compares the window's usage with the
# share of its time that has passed: five points either way is noise on a
# 5-hour window; twenty points is a full hour of budget there (about 1.4 days
# on the weekly window), the point where it runs dry well before it resets.
PACE_STYLES = ("arrows", "percent")
DEFAULT_PACE_STYLE = "arrows"
PACE_ON_TRACK_PTS = 5
PACE_STRONG_PTS = 20


def _format_pace(pct, pace, style=DEFAULT_PACE_STYLE):
    """The pace widget's text with its leading space, or "" without a pace.

    percent → `` (43%)``, the share of the window's time that has passed.
    arrows  → `` ↑`` when usage is ahead of the clock, `` ↓`` behind it,
              doubled (``⇈`` red, ``⇊`` green) past PACE_STRONG_PTS, and a
              dim `` →`` when it is on track.
    """
    if pace is None:
        return ""
    if style not in PACE_STYLES:
        style = DEFAULT_PACE_STYLE
    if style == "percent":
        return f" ({pace:.0f}%)"
    diff = pct - pace
    if diff > PACE_STRONG_PTS:
        glyph, colour = "\u21c8", BRIGHT_RED      # ⇈ well over pace
    elif diff > PACE_ON_TRACK_PTS:
        glyph, colour = "\u2191", YELLOW          # ↑ over pace
    elif diff < -PACE_STRONG_PTS:
        glyph, colour = "\u21ca", BRIGHT_GREEN    # ⇊ well under pace
    elif diff < -PACE_ON_TRACK_PTS:
        glyph, colour = "\u2193", GREEN           # ↓ under pace
    else:
        glyph, colour = "\u2192", DIM             # → on track
    return f" {colour}{glyph}{RESET}"


def _roll_over_expired_windows(usage):
    """Copy of *usage* where every usage window whose reset has passed reads 0 %.

    Claude Code drops a window from ``rate_limits`` the moment its ``resets_at``
    passes, so a window that is still carried (persisted stdin context, an old
    cache.json) describes a period that is over. Drawn as it was, it showed the
    old percentage next to "now" (F01). The same rule as ``_provider_window``:
    0 % and no reset ("starts on next use"). Covers ``five_hour``, ``seven_day``
    and the model-scoped ``seven_day_*`` caps; anything else, and anything not
    a well-formed window, is returned untouched. *usage* is never mutated.
    """
    if not isinstance(usage, dict):
        return usage
    now = time.time()
    out = dict(usage)
    for key, win in usage.items():
        if not isinstance(key, str) or not isinstance(win, dict):
            continue
        if key not in ("five_hour", "seven_day") and not key.startswith("seven_day_"):
            continue
        reset = _parse_reset(win.get("resets_at"))
        if reset is None:
            continue
        try:
            expired = reset.timestamp() <= now
        except (OverflowError, OSError, ValueError):
            continue
        if expired:
            out[key] = dict(win, utilization=0.0, resets_at=None)
    return out


def _extra_usage_view(extra):
    """The numbers of an ``extra_usage`` block, or None when it is off or unusable.

    The block comes from the usage API or from cache.json, both untrusted, so
    a wrong type reads as 0 instead of raising (a raise is a blank bar).
    Amounts are minor units (cents, pence) in the API; the view holds major
    units. ``currency`` is the ISO-4217 code the API reports, "" when it does
    not (older payloads, always USD).
    """
    if not isinstance(extra, dict) or not extra.get("is_enabled"):
        return None

    def num(value):
        n = _stdin_number(value)
        return max(n, 0.0) if n is not None else 0.0

    code = extra.get("currency")
    code = code.strip().upper() if isinstance(code, str) else ""
    if not re.fullmatch(r"[A-Z]{3}", code):
        code = ""
    return {"pct": min(num(extra.get("utilization")), 100.0),
            "used": num(extra.get("used_credits")) / 100,
            "limit": num(extra.get("monthly_limit")) / 100,
            "currency": code}


def _extra_money(view, symbol):
    """``(symbol, used, limit)`` to draw for an extra-usage view (F17).

    A USD amount (or one with no currency) is converted into the user's
    currency exactly like the session cost beside it. An amount the API
    already states in another currency is never converted: it keeps the
    user's own symbol when that is the same currency, else its ISO code.
    """
    code = view["currency"]
    if code and code != "USD":
        if _CURRENCY_TO_CODE.get(symbol) == code:
            return symbol, view["used"], view["limit"]
        return code + " ", view["used"], view["limit"]
    rate, rate_code = _get_exchange_rate(symbol)
    return ("$" if rate_code == "USD" else symbol), view["used"] * rate, view["limit"] * rate


def _extra_age_tag(stdin_ctx, config):
    """A dim " (3d ago)" when the extra-usage figure is overdue for a refresh.

    The figure is refetched every ``model_caps_refresh_seconds``; it is old
    only when that did not happen (offline, rate limited). main() stamps the
    time of the last good fetch on ``stdin_ctx`` under ``_extra_fetched_at``
    (None when none was ever recorded). Without the key nothing is known, so
    nothing is claimed.
    """
    if not isinstance(stdin_ctx, dict) or "_extra_fetched_at" not in stdin_ctx:
        return ""
    fetched = _cache_seconds(stdin_ctx.get("_extra_fetched_at")) or None
    if fetched is None:
        return f" {YELLOW}(stale){RESET}"
    age = time.time() - fetched
    if age <= 2 * _model_caps_ttl(config):
        return ""
    if age < 3600:
        label = f"{int(age // 60)}m"
    elif age < 172800:
        label = f"{int(age // 3600)}h"
    else:
        label = f"{int(age // 86400)}d"
    colour = RED if age >= 86400 else YELLOW
    return f" {colour}({label} ago){RESET}"


def format_reset_time(resets_at_str):
    if not resets_at_str:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str)
        now = datetime.now(timezone.utc)
        total_seconds = int((resets_at - now).total_seconds())
        if total_seconds <= 0:
            return "now"
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return None


WEEKLY_TIMER_FORMATS = ("auto", "countdown", "date", "full")
DEFAULT_WEEKLY_TIMER_FORMAT = "auto"
CLOCK_FORMATS = ("12h", "24h")
DEFAULT_CLOCK_FORMAT = "12h"
DEFAULT_WEEKLY_TIMER_PREFIX = "R:"


def _weekly_countdown(total_seconds):
    """Format seconds as compact countdown: '2d 5h', '14h 22m', or '45m'."""
    if total_seconds >= 86400:
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        return f"{days}d {hours}h"
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _weekly_date(resets_at, clock="12h"):
    """Format reset time as local day+hour: 'Sat 5pm' or 'Sat 17:00'."""
    local_dt = resets_at.astimezone()
    hour = local_dt.hour
    if clock == "24h":
        time_str = f"{hour:02d}:{local_dt.minute:02d}"
    else:
        # Weekly resets fall on the hour in UTC, which is :30 or :45 local in
        # half-hour zones (India, Nepal, Newfoundland...). Keep the compact
        # "5pm" for an exact hour and spell the minutes out otherwise.
        minutes = f":{local_dt.minute:02d}" if local_dt.minute else ""
        time_str = f"{hour % 12 or 12}{minutes}{'am' if hour < 12 else 'pm'}"
    return f"{local_dt.strftime('%a')} {time_str}"


def format_weekly_reset(resets_at_str, fmt="auto", clock="12h"):
    """Format weekly reset time.

    Formats:
      auto      — date when >24h, countdown when <24h (default)
      countdown — always show countdown: '2d 5h' / '14h 22m' / '45m'
      date      — always show date: 'Sat 5pm' (or 'Sat 17:00' with clock='24h')
      full      — both: 'Sat 5pm · 2d 5h'

    clock: '12h' for am/pm display, '24h' for 24-hour display.
    """
    if not resets_at_str:
        return None
    try:
        safe = _sanitize(str(resets_at_str))
        resets_at = datetime.fromisoformat(safe)
        now = datetime.now(timezone.utc)
        total_seconds = int((resets_at - now).total_seconds())
        if total_seconds <= 0:
            return "now"
        if fmt == "countdown":
            return _weekly_countdown(total_seconds)
        if fmt == "date":
            return _weekly_date(resets_at, clock=clock)
        if fmt == "full":
            return f"{_weekly_date(resets_at, clock=clock)} \u00b7 {_weekly_countdown(total_seconds)}"
        # auto: date when >24h, countdown when <24h
        if total_seconds < 86400:
            return _weekly_countdown(total_seconds)
        return _weekly_date(resets_at, clock=clock)
    except (ValueError, TypeError, OverflowError, OSError):
        # OSError: on Windows a date past the year 3000 cannot be converted to
        # local time (astimezone raises "[Errno 22] Invalid argument").
        return None


def _get_history_path():
    """Return path to usage history file."""
    return get_account_state_dir() / "history.json"


def _read_history():
    """Read usage history samples: the ones the renderer can draw, [] on a damaged file."""
    return _load_samples(_get_history_path(), ("s", "w"))[1]


def _window_pct(usage, key):
    """Utilization of one usage window as a finite number; 0 when it is missing or unusable."""
    window = usage.get(key) if isinstance(usage, dict) else None
    if not isinstance(window, dict):
        return 0
    return _usage_number(window.get("utilization")) or 0


def _append_history(usage):
    """Append a usage sample to history and prune old entries."""
    session_pct = _window_pct(usage, "five_hour")
    weekly_pct = _window_pct(usage, "seven_day")

    # Not _read_history(): that returns [] for a half-written file, and this
    # function writes the result back. Another window's in-place write (an
    # older version) must not turn a day of samples into one.
    ok, samples = _load_samples(_get_history_path(), ("s", "w"))
    if not ok:
        return  # skip this sample; the next repaint tries again
    now = time.time()
    samples.append({"t": now, "s": session_pct, "w": weekly_pct})

    # Prune entries older than 24 hours and cap entry count
    cutoff = now - HISTORY_MAX_AGE
    samples = [s for s in samples if s.get("t", 0) > cutoff]
    samples = samples[-2000:]  # prevent unbounded growth

    try:
        _atomic_json_write(_get_history_path(), samples, indent=None)
    except OSError:
        pass


def _render_sparkline(samples, key="s", width=8):
    """Render a sparkline from usage samples."""
    if not samples:
        return ""
    # Take the last `width` samples
    recent = samples[-width:]
    chars = []
    for s in recent:
        val = s.get(key, 0)
        # Map 0-100 to index 0-6 (avoid █ at index 7 — it's in ALL_BAR_CHARS)
        idx = min(6, max(0, int(val / 100 * 6.99)))
        chars.append(SPARKLINE_CHARS[idx])
    return "".join(chars)


def _estimate_runway(samples, current_pct):
    """Estimate time until 100% usage via linear regression over recent samples.

    Returns a string like '~2h 15m' or '~45m', or None if insufficient data.
    """
    if len(samples) < 2 or current_pct >= 100:
        return None

    now = time.time()
    # Use samples from the last 10 minutes
    cutoff = now - 600
    recent = [s for s in samples if s.get("t", 0) > cutoff]

    if len(recent) < 2:
        return None

    # Simple linear regression: pct vs time
    n = len(recent)
    sum_t = sum(s["t"] for s in recent)
    sum_s = sum(s.get("s", 0) for s in recent)
    sum_ts = sum(s["t"] * s.get("s", 0) for s in recent)
    sum_tt = sum(s["t"] ** 2 for s in recent)

    denom = n * sum_tt - sum_t ** 2
    if abs(denom) < 1e-10:
        return None

    slope = (n * sum_ts - sum_t * sum_s) / denom  # pct per second

    if slope <= 0.001:
        return None  # Usage is flat or declining

    remaining = 100.0 - current_pct
    seconds_to_full = remaining / slope

    if seconds_to_full > 86400:  # More than 24 hours, not useful
        return None

    hours = int(seconds_to_full // 3600)
    minutes = int((seconds_to_full % 3600) // 60)

    if hours > 0:
        return f"~{hours}h {minutes:02d}m"
    return f"~{minutes}m"


def _compute_velocity(samples):
    """Compute usage velocity in pct/min from recent history samples."""
    if len(samples) < 2:
        return None
    now = time.time()
    recent = [s for s in samples if s.get("t", 0) > now - 300]  # last 5 min
    if len(recent) < 2:
        return None
    dt = recent[-1]["t"] - recent[0]["t"]
    if dt < 10:  # less than 10 seconds of data
        return None
    dp = recent[-1].get("s", 0) - recent[0].get("s", 0)
    return (dp / dt) * 60  # pct per minute


def _format_burn_rate(samples, current_pct, show_runway=False):
    """Format burn rate as an arrow indicator with optional runway."""
    velocity = _compute_velocity(samples)
    if velocity is None or velocity <= 0.5:
        return ""  # Only show when actively burning
    else:
        pct_per_hr = velocity * 60
        indicator = f"{VELOCITY_ARROW_UP}{pct_per_hr:.0f}%/hr" if pct_per_hr >= 10 else f"{VELOCITY_ARROW_UP}{pct_per_hr:.1f}%/hr"
    result = indicator
    if show_runway and velocity is not None and velocity > 0 and current_pct < 100:
        runway = _estimate_runway(samples, current_pct)
        if runway:
            result += f" {runway} left"
    return result


def _format_staleness(cache_age):
    """Format cache staleness as a colored age indicator."""
    if cache_age is None or cache_age <= STALENESS_WARN:
        return ""
    if cache_age >= STALENESS_RED:
        return f" {RED}(10m+ ago){RESET}"
    minutes = int(cache_age // 60)
    if cache_age >= STALENESS_YELLOW:
        return f" {YELLOW}({minutes}m ago){RESET}"
    return f" {DIM}({minutes}m ago){RESET}"


_EXCHANGE_RATE_TTL = 86400  # 24 hours
_FALLBACK_RATES = {
    "GBP": 0.79, "EUR": 0.92, "JPY": 149.0, "CAD": 1.36, "AUD": 1.53,
    "CHF": 0.88, "CNY": 7.24, "INR": 83.5, "BRL": 4.97, "KRW": 1330.0,
    "SEK": 10.4, "NOK": 10.6, "DKK": 6.85, "PLN": 3.98, "CZK": 23.2,
    "NZD": 1.63, "SGD": 1.34, "HKD": 7.82, "MXN": 17.1, "ZAR": 18.5,
    "TRY": 32.0, "THB": 34.8, "TWD": 31.5, "ILS": 3.62, "AED": 3.67,
}
_CURRENCY_TO_CODE = {
    "$": "USD", "\u00a3": "GBP", "\u20ac": "EUR", "\u00a5": "JPY",
    "C$": "CAD", "A$": "AUD", "NZ$": "NZD", "S$": "SGD", "HK$": "HKD",
    "R$": "BRL", "kr": "SEK", "Fr": "CHF", "\u20b9": "INR", "\u20a9": "KRW",
    "R": "ZAR", "z\u0142": "PLN", "K\u010d": "CZK", "\u20ba": "TRY",
    "\u20b1": "PHP", "RM": "MYR", "\u20aa": "ILS",
    # No hryvnia sign: the rate service (frankfurter) does not list UAH, so
    # there is no rate to convert with. Left unmapped, amounts stay in dollars.
}

# ISO codes name a currency without the ambiguity of a shared symbol ("kr" is
# the Swedish, Danish and Norwegian krone), so ``--currency DKK`` works. These
# are the codes frankfurter's /v1/currencies lists: all of them can be priced.
_ISO_CURRENCY_CODES = frozenset(
    "AUD BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF IDR ILS INR ISK JPY KRW MXN "
    "MYR NOK NZD PHP PLN RON SEK SGD THB TRY USD ZAR".split()
)
# Which krone a bare "kr" means when the machine's country says so. Configs
# saved before Denmark and Norway got their own codes still hold "kr".
_KRONE_BY_COUNTRY = {"DK": "DKK", "NO": "NOK"}


def _currency_code(currency_symbol):
    """ISO code for a configured currency symbol or code; "" when there is none."""
    if not isinstance(currency_symbol, str):
        return ""
    if currency_symbol == "kr":
        return _KRONE_BY_COUNTRY.get(_locale_country(), "SEK")
    code = _CURRENCY_TO_CODE.get(currency_symbol, "")
    if not code:
        iso = currency_symbol.strip().upper()
        if iso in _ISO_CURRENCY_CODES:
            code = iso
    return code


_exchange_rate_mem = {}  # in-memory cache: {code: (rate, timestamp)}


def _usable_rate(value):
    """An exchange rate as a positive finite float, or None when unusable.

    The cache file and the API reply are untrusted input. Besides text and
    null, valid JSON can hold an int too big for a float (float() raises
    OverflowError, which is not a ValueError), NaN, Infinity, zero or a
    negative number: none of them can price a cost.
    """
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def _get_exchange_rate(currency_symbol):
    """Get USD→target exchange rate. Cached for 24h. Returns (rate, code) or (1.0, 'USD')."""
    code = _currency_code(currency_symbol)
    if not code or code == "USD":
        return 1.0, "USD"

    # In-memory cache (avoids disk I/O on hot path)
    now = time.time()
    mem = _exchange_rate_mem.get(code)
    if mem and now - mem[1] < _EXCHANGE_RATE_TTL:
        return mem[0], code

    # Check disk cache
    cache_path = get_state_dir() / "exchange_rate.json"
    stale_rate = None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if code in cached.get("rates", {}):
            rate = _usable_rate(cached["rates"][code])
            if rate is not None:
                # A damaged or future timestamp is not fresh: refetch instead
                # of trusting the rate until a date that may never come.
                timestamp = _cache_seconds(cached.get("timestamp", 0))
                if timestamp is not None and 0 <= now - timestamp < _EXCHANGE_RATE_TTL:
                    _exchange_rate_mem[code] = (rate, now)
                    return rate, code
                stale_rate = rate
    except (FileNotFoundError, json.JSONDecodeError, OSError,
            AttributeError, TypeError, ValueError):
        pass

    # Fetch from frankfurter.app (free, no API key)
    if _begin_net_attempt(f"exchange_rate:{code}"):
        try:
            url = f"https://api.frankfurter.dev/v1/latest?from=USD&to={code}"
            req = urllib.request.Request(url, headers={"User-Agent": "usage-pulse"})
            body = _fetch_bounded(req, 10000)
            data = json.loads(body) if body is not None else {}
            rate = _usable_rate(data.get("rates", {}).get(code))
            if rate is not None:
                # Cache all fetched rates
                try:
                    _atomic_json_write(cache_path, {
                        "timestamp": time.time(),
                        "rates": data["rates"],
                    }, indent=None)
                except OSError:
                    pass
                _exchange_rate_mem[code] = (rate, now)
                return rate, code
        except Exception:
            pass

    # Fallback: the last real rate if there ever was one, else the hardcoded one
    fallback = stale_rate if stale_rate is not None else _FALLBACK_RATES.get(code)
    if fallback is None:
        # No rate of any kind: show the amount as the dollars it still is,
        # rather than under this currency's name at a rate of 1.0.
        return 1.0, "USD"
    _exchange_rate_mem[code] = (fallback, now)
    return fallback, code


def _format_cost(stdin_ctx, config):
    """Format session cost from stdin context, converted to user's currency."""
    cost_usd = stdin_ctx.get("cost_usd")
    if cost_usd is None:
        return ""
    try:
        cost_val = float(cost_usd)
    except (ValueError, TypeError):
        return ""
    currency = _sanitize(config.get("currency", "$"))[:5]
    rate, code = _get_exchange_rate(currency)
    converted = cost_val * rate
    if code == "USD":
        return f"${converted:.2f}"
    return f"{currency}{converted:.2f}"


def _get_context_history_path():
    return get_account_state_dir() / "context_history.json"


def _read_context_history():
    """Context samples the renderer can use; [] on a damaged file."""
    return _load_samples(_get_context_history_path(), ("c",))[1]


# context_history.json is per account, but context is per session: two windows
# at 75% and 10% sharing one series read as a jump of 65 points in seconds. So
# each sample names its session, and the velocity of a window is computed from
# that window's samples alone. main() announces the session once per repaint.
_context_session_id = None


def _set_context_session(session_id):
    """Name the session this process is painting (None when stdin gave none)."""
    global _context_session_id
    _context_session_id = session_id or None


def _context_session_key(session_id=None):
    sid = session_id if session_id is not None else _context_session_id
    return str(sid)[:64] if sid else ""


def _append_context_history(context_pct, session_id=None):
    pct = _usage_number(context_pct)
    if pct is None:
        return
    # Not _read_context_history(): see _append_history.
    ok, samples = _load_samples(_get_context_history_path(), ("c",))
    if not ok:
        return
    now = time.time()
    samples.append({"t": now, "c": pct, "s": _context_session_key(session_id)})
    cutoff = now - 3600
    samples = [s for s in samples if s.get("t", 0) > cutoff][-500:]
    try:
        _atomic_json_write(_get_context_history_path(), samples, indent=None)
    except OSError:
        pass


def _compute_context_velocity(session_id=None):
    _, samples = _load_samples(_get_context_history_path(), ("c",))
    # Samples from before sessions were recorded carry no "s": they belong to
    # no named session (an unnamed one, key "", still reads them).
    key = _context_session_key(session_id)
    now = time.time()
    recent = [s for s in samples if s.get("s", "") == key and s["t"] > now - 300]
    if len(recent) < 2:
        return None
    dt = recent[-1]["t"] - recent[0]["t"]
    if dt < 10:
        return None
    dp = recent[-1].get("c", 0) - recent[0].get("c", 0)
    return (dp / dt) * 60


def _format_context_warning(ctx_pct, theme):
    """Return (label, suffix) for context pressure warning, or (None, None)."""
    if ctx_pct is None:
        return None, None
    ctx_velocity = _compute_context_velocity()
    if ctx_pct >= CONTEXT_PRESSURE_CRITICAL:
        suffix = ""
        if ctx_velocity is not None and ctx_velocity > CONTEXT_PRESSURE_VELOCITY:
            suffix = f" {VELOCITY_ARROW_UP}fast"
        elif ctx_velocity is not None and ctx_velocity > 0.5:
            suffix = f" {VELOCITY_ARROW_UP}"
        return f"{theme['high']}\u26a0 Context{RESET}", suffix
    if ctx_pct >= CONTEXT_PRESSURE_PCT:
        if ctx_velocity is not None and ctx_velocity > CONTEXT_PRESSURE_VELOCITY:
            return f"{theme['high']}\u26a0 Context{RESET}", f" {VELOCITY_ARROW_UP}fast"
    return None, None


def _get_status_message(pct, velocity=None):
    """Return a (message, severity) tuple based on usage percentage and velocity.

    Severity: 'low', 'mid', 'high'
    """
    if pct >= 95:
        return ("At the limit", "high")
    if pct >= 80:
        return ("Pace yourself", "high")
    if pct >= 60:
        if velocity is not None and velocity > 2.0:
            return ("Running hot", "high")
        return ("Steady pace", "mid")
    if pct >= 30:
        if velocity is not None and velocity > 2.0:
            return ("In the flow", "mid")
        return ("Cruising", "mid")
    if pct >= 10:
        return ("Warming up", "low")
    return ("Fresh start", "low")


# ---------------------------------------------------------------------------
# Session stats & streaks
# ---------------------------------------------------------------------------

STREAK_MILESTONES = {
    7: "Week!",
    30: "Month!",
    50: "Fifty!",
    100: "Century!",
    200: "200 club!",
    365: "Year!",
    500: "500!",
    1000: "Legend!",
}


def _today_local():
    """Return today's date as YYYY-MM-DD in local timezone."""
    return datetime.now().strftime("%Y-%m-%d")


def _get_stats_path():
    """Return path to stats file."""
    return get_state_dir() / "stats.json"


def _load_stats():
    """Load stats from disk; the defaults when the file is missing, damaged or not an object."""
    _, data = _read_state_json(_get_stats_path())
    if isinstance(data, dict):
        return data
    return {
        "first_seen": _today_local(),
        "total_sessions": 0,
        "daily_dates": [],
        "current_streak": 0,
        "longest_streak": 0,
        "last_date": "",
    }


def _save_stats(stats):
    """Save stats to disk."""
    try:
        _atomic_json_write(_get_stats_path(), stats, indent=2)
    except OSError:
        pass


def _calculate_streak(daily_dates, today):
    """Calculate current and longest streak from date strings.

    Current streak counts consecutive days ending at today or yesterday.
    Returns (current_streak, longest_streak).
    """
    if not daily_dates:
        return (0, 0)

    # Deduplicate and sort
    unique = sorted(set(daily_dates))
    dates = []
    for d in unique:
        try:
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
        except ValueError:
            continue

    if not dates:
        return (0, 0)

    try:
        today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return (0, 0)

    # Calculate longest streak
    longest = 1
    run = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    longest = max(longest, run)

    # Calculate current streak using ordinal day arithmetic
    current_streak = 0
    check_ord = today_date.toordinal()
    for d in reversed(dates):
        d_ord = d.toordinal()
        if d_ord == check_ord:
            current_streak += 1
            check_ord -= 1
        elif d_ord == check_ord - 1 and current_streak == 0:
            # Today not logged yet, start from yesterday
            current_streak = 1
            check_ord = d_ord - 1
        elif d_ord < check_ord:
            break

    return (current_streak, longest)


def _check_milestone(total):
    """Check if total sessions hit a milestone. Returns message or None."""
    return STREAK_MILESTONES.get(total)


def _update_stats():
    """Update daily stats on fresh fetch. Returns (stats, milestone_or_None)."""
    stats = _load_stats()
    today = _today_local()

    if stats.get("last_date") == today:
        return (stats, None)  # Already updated today

    if not stats.get("first_seen"):
        stats["first_seen"] = today

    daily_dates = stats.get("daily_dates", [])
    if today not in daily_dates:
        daily_dates.append(today)
    stats["daily_dates"] = daily_dates

    stats["total_sessions"] = stats.get("total_sessions", 0) + 1

    current, longest = _calculate_streak(daily_dates, today)
    stats["current_streak"] = current
    stats["longest_streak"] = max(stats.get("longest_streak", 0), longest)
    stats["last_date"] = today

    milestone = _check_milestone(stats["total_sessions"])

    _save_stats(stats)
    return (stats, milestone)


def _get_streak_display(config, stats):
    """Return formatted streak string like '7d streak' or ''."""
    show = config.get("show", DEFAULT_SHOW)
    if not show.get("streak", True):
        return ""
    streak = stats.get("current_streak", 0)
    if streak < 2:
        return ""
    style = config.get("streak_style", "text")
    if style == "fire":
        return f"\U0001f525{streak}"
    return f"{streak}d streak"


_CUMULATIVE_COST_CACHE_TTL = 300  # 5 minutes
_cumulative_cost_mem = {"ts": 0, "data": {}}

_WEEKLY_COST_CACHE_TTL = 300  # 5 minutes
_WEEKLY_COST_WINDOW = 7 * 86400
_weekly_cost_mem = {"ts": 0, "data": {}}


# Reading every transcript takes seconds (tens of seconds on a big store), and
# Claude Code cancels a status line script the moment the next update fires,
# so a scan on the render path never finishes: the figure is only saved once
# the scan returns, nothing records that one began, and every repaint starts
# it over. The render path therefore never scans. It draws the last saved
# figure, however old, and when that is out of date it starts ONE detached
# process (_cost_refresh_worker) that scans and saves it. A claim file,
# created with O_EXCL before the process starts, keeps every other repaint and
# every other window from starting a second scan.
_COST_CACHE_VERSION = 2  # bumped when the scan's sums change; older files are not drawn
_COST_SCAN_LOCK_TTL = 300  # seconds a claim holds off a new scan (also the retry delay after a failure)
# A claim taken by another window a moment after this one read the clock is
# stamped slightly in this one's future. Up to this much counts as young; a
# stamp further ahead means the clock was set back, and the claim lapses.
_COST_SCAN_CLOCK_SLOP = 60
_CACHE_WRITE_1H_MULTIPLIER = 2.0  # 1-hour cache writes bill at 2x the input price

# Run by the detached process: load this file by path and call the worker.
_COST_WORKER_BOOT = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('usage_pulse_cost_worker', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "sys.modules[spec.name] = mod\n"
    "spec.loader.exec_module(mod)\n"
    "mod._cost_refresh_worker(sys.argv[2])\n"
)


def _cost_kind(kind):
    """(cache file name, in-memory cache, TTL, window in seconds) for *kind*."""
    if kind == "weekly":
        return ("weekly_cost_cache.json", _weekly_cost_mem,
                _WEEKLY_COST_CACHE_TTL, _WEEKLY_COST_WINDOW)
    if kind == "cumulative":
        return ("cumulative_cost_cache.json", _cumulative_cost_mem,
                _CUMULATIVE_COST_CACHE_TTL, None)
    return None


def _read_cost_cache(path):
    """A saved cost figure as (timestamp, data), or None.

    None covers a missing or damaged file and one written by an older scan
    (before responses were counted once, its totals were about double): better
    a blank for the seconds a refresh takes than a wrong figure.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(saved, dict) or saved.get("v") != _COST_CACHE_VERSION:
        return None
    stamp = _cache_seconds(saved.get("timestamp"))
    data = saved.get("data")
    if stamp is None or not isinstance(data, dict):
        return None
    return stamp, data


def _claim_cost_scan(kind, now):
    """Take the right to scan for *kind*. False means someone else holds it.

    The claim is a file created with O_EXCL, so only one process wins. It is
    taken BEFORE any work starts: a scan that is cut short, or that crashes,
    leaves the claim behind and nothing retries for _COST_SCAN_LOCK_TTL. A
    claim older than that is stale and is replaced; two processes racing to
    replace it can at worst both scan once, which is harmless.
    """
    path = get_account_state_dir() / ("cost_scan_%s.lock" % kind)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue  # released while we looked: try to take it
            if -_COST_SCAN_CLOCK_SLOP <= age < _COST_SCAN_LOCK_TTL:
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return False  # no claim, no scan: the figure just stays as it was
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass  # the file exists, which is all the claim needs
        return True
    return False


def _release_cost_scan(kind):
    try:
        (get_account_state_dir() / ("cost_scan_%s.lock" % kind)).unlink()
    except OSError:
        pass


def _spawn_cost_worker(kind):
    """Start the detached process that refreshes *kind*. True if it started.

    It must outlive this status line process, which exits as soon as it has
    printed (or is cancelled): its own session on POSIX, and on Windows no
    console, a new process group and, where the job allows it, outside the
    job object Claude Code may have put this process in.
    """
    if not sys.executable:
        return False
    # -I: no cwd or user site on sys.path, no PYTHON* variables.
    argv = [sys.executable, "-I", "-c", _COST_WORKER_BOOT,
            str(Path(__file__).resolve()), kind]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "close_fds": True}
    try:
        if sys.platform == "win32":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            try:
                subprocess.Popen(argv, creationflags=flags | breakaway, **options)
            except OSError:
                # The job forbids breakaway: start without it.
                subprocess.Popen(argv, creationflags=flags, **options)
        else:
            subprocess.Popen(argv, start_new_session=True, **options)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def _request_cost_refresh(kind, now):
    """Have *kind*'s figure refreshed in the background, at most one at a time."""
    try:
        if not _claim_cost_scan(kind, now):
            return False
        # A claim whose process never started is kept, not released: it is the
        # backoff that stops every repaint from trying again.
        return _spawn_cost_worker(kind)
    except Exception:
        return False


def _cost_refresh_worker(kind):
    """Entry point of the detached process: scan, save the figure, free the claim.

    A failed scan keeps its claim, so a broken store is retried after
    _COST_SCAN_LOCK_TTL instead of by a new process on every repaint.
    """
    spec = _cost_kind(kind)
    if spec is None:
        return
    name, _mem, _ttl, window = spec
    try:
        since = time.time() - window if window else None
        data = _scan_session_costs(since_ts=since)
        _atomic_json_write(
            get_account_state_dir() / name,
            {"v": _COST_CACHE_VERSION, "timestamp": time.time(), "data": data},
            indent=None,
        )
    except Exception:
        return
    _release_cost_scan(kind)


def _get_cached_cost(kind):
    """The saved figure for *kind*, never scanning transcripts itself.

    Fresh figures come from memory or disk. A stale one is still returned (an
    old total beats a missing one) while a background refresh is requested; with
    nothing saved yet the result is empty and the widget stays hidden until the
    first refresh lands.
    """
    name, mem, ttl, _window = _cost_kind(kind)
    now = time.time()

    # In-memory cache (avoids disk I/O on hot path)
    if now - mem["ts"] < ttl:
        return mem["data"]

    saved = _read_cost_cache(get_account_state_dir() / name)
    if saved is not None:
        stamp, data = saved
        if 0 <= now - stamp < ttl:
            mem["ts"] = now
            mem["data"] = data
            return data
    _request_cost_refresh(kind, now)
    return saved[1] if saved is not None else {}


def _get_cached_weekly_cost():
    """Rolling 7-day API-equivalent cost: the last saved figure (see above)."""
    return _get_cached_cost("weekly")


def _get_cached_cumulative_cost():
    """All-time API-equivalent cost: the last saved figure (see above)."""
    return _get_cached_cost("cumulative")


def _token_count(value):
    """A token count from untrusted JSON as a non-negative int (0 if unusable)."""
    try:
        count = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return count if count > 0 else 0


def _split_cache_writes(usage):
    """(5-minute, 1-hour) cache-write tokens of one response's usage.

    ``cache_creation_input_tokens`` is the total; ``cache_creation`` splits it
    by cache lifetime. Without the split (older transcripts, other providers)
    everything is a 5-minute write, the cheaper rate.
    """
    total = _token_count(usage.get("cache_creation_input_tokens"))
    detail = usage.get("cache_creation")
    one_hour = five = 0
    if isinstance(detail, dict):
        one_hour = _token_count(detail.get("ephemeral_1h_input_tokens"))
        five = _token_count(detail.get("ephemeral_5m_input_tokens"))
    if total and one_hour > total:
        one_hour = total  # a part cannot exceed the whole
    return max(total - one_hour, five), one_hour


def _parse_transcript_ts(value):
    """Parse a transcript entry's ISO-8601 timestamp to a UTC epoch float.

    Claude Code writes them Z-suffixed ("2026-07-18T15:01:07.532Z").
    ``datetime.fromisoformat`` only learned to accept "Z" in Python 3.11, and
    usage-pulse supports 3.8, so normalise it to an explicit offset first.
    Returns None for anything unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _scan_session_costs(since_ts=None):
    """Scan Claude Code session JSONL transcripts for API-equivalent costs.

    Returns a dict with per-model cost/token breakdown, totals, session count,
    and the earliest file mtime seen.

    When *since_ts* (a UTC epoch float) is given, only entries stamped at or
    after it are counted, which is what the rolling weekly-cost widget needs.
    Files whose mtime predates the cutoff are skipped outright — they cannot
    contain newer entries — but the per-entry timestamp is still checked,
    because a file touched today may hold months of history.
    """
    projects_dir = _claude_config_dir() / "projects"

    models: dict = {}
    total_cost_usd = 0.0
    total_tokens = 0
    session_count = 0
    first_seen_ts = None

    if not projects_dir.exists():
        return {
            "models": {},
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "session_count": 0,
            "first_seen": None,
        }

    seen: dict = {}  # (message id, request id) -> the record counted for it

    def tally(record, sign):
        """Add (sign=1) or take back (sign=-1) one response's figures."""
        nonlocal total_cost_usd, total_tokens
        model_id, cost, inp, out, cr, cw5, cw1 = record
        m = models.get(model_id)
        if m is None:
            m = models[model_id] = {
                "cost_usd": 0.0,
                "total_tokens": 0,
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        tokens = inp + out + cr + cw5 + cw1
        m["cost_usd"] += sign * cost
        m["input"] += sign * inp
        m["output"] += sign * out
        m["cache_read"] += sign * cr
        m["cache_write"] += sign * (cw5 + cw1)
        m["total_tokens"] += sign * tokens
        total_cost_usd += sign * cost
        total_tokens += sign * tokens

    try:
        jsonl_files = projects_dir.rglob("*.jsonl")
    except (OSError, PermissionError):
        jsonl_files = []

    for jsonl_path in jsonl_files:
        # Cheap prefilter: a file untouched since the cutoff has nothing new.
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            mtime = None
        if since_ts is not None and mtime is not None and mtime < since_ts:
            continue

        # Count sessions: top-level JSONL files only (not inside subagents/ dirs)
        path_parts = jsonl_path.parts
        is_subagent = any(p == "subagents" for p in path_parts)
        if not is_subagent:
            session_count += 1

        # Track earliest file mtime
        if mtime is not None and (first_seen_ts is None or mtime < first_seen_ts):
            first_seen_ts = mtime

        # Parse each line
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    try:
                        if entry.get("type") != "assistant":
                            continue
                        msg = entry.get("message", {})
                        usage = msg.get("usage", {})
                        if not usage or "input_tokens" not in usage:
                            continue
                        entry_ts = _parse_transcript_ts(entry.get("timestamp"))
                        if since_ts is not None:
                            # Drop undated entries too: counting them would
                            # inflate a "last 7 days" figure with history.
                            if entry_ts is None or entry_ts < since_ts:
                                continue
                        # Price at the rates in force when the entry was made,
                        # not on the scan day — otherwise a promo's expiry
                        # retroactively reprices every turn from the promo era.
                        # Undated entries fall back to today inside _pricing_for.
                        entry_date = None
                        if entry_ts is not None:
                            try:
                                entry_date = datetime.fromtimestamp(
                                    entry_ts, tz=timezone.utc
                                ).date()
                            except (OSError, OverflowError, ValueError):
                                pass
                        model_id = msg.get("model", "")
                        # Normalise: strip version suffix variants for matching
                        # e.g. "claude-sonnet-4-5-20251022" → "claude-sonnet-4-5".
                        # Longest prefix wins — a first-match scan would let the
                        # bare "claude-opus-4" key ($15/$75) swallow
                        # "claude-opus-4-8" ($5/$25) and treble the reported cost.
                        pricing = _pricing_for(model_id, entry_date)
                        if pricing is None:
                            best_key = None
                            for key in API_PRICING:
                                if model_id.startswith(key) and (
                                    best_key is None or len(key) > len(best_key)
                                ):
                                    best_key = key
                            if best_key is not None:
                                pricing = _pricing_for(best_key, entry_date)
                                model_id = best_key
                        if pricing is None:
                            continue

                        inp = int(usage.get("input_tokens") or 0)
                        out = int(usage.get("output_tokens") or 0)
                        cr = int(usage.get("cache_read_input_tokens") or 0)
                        # Cache writes bill at 1.25x input for the 5-minute
                        # cache and 2x for the 1-hour one (which Claude Code
                        # uses on subscriber plans).
                        cw5, cw1 = _split_cache_writes(usage)
                        # Haiku 5.5 steps up above 100K prompt tokens, priced
                        # per request (no-op for every other model).
                        pricing = _pricing_for(
                            model_id, entry_date, inp + cr + cw5 + cw1) or pricing

                        cost = (
                            inp * pricing["input"]
                            + out * pricing["output"]
                            + cr * pricing["cache_read"]
                            + cw5 * pricing["cache_write"]
                            + cw1 * pricing.get("cache_write_1h",
                                                pricing["input"] * _CACHE_WRITE_1H_MULTIPLIER)
                        ) / 1_000_000
                        record = (model_id, cost, inp, out, cr, cw5, cw1)

                        # Claude Code writes one line per content block
                        # (thinking, text, tool_use) of a response, each
                        # repeating the response's id and usage, and a resumed
                        # session copies earlier lines into a new file. Count
                        # each response once; if a repeat reports more output
                        # than the one kept, the fuller report replaces it.
                        message_id = msg.get("id")
                        if isinstance(message_id, str) and message_id:
                            request_id = entry.get("requestId")
                            key = (message_id, request_id if isinstance(request_id, str) else "")
                            kept = seen.get(key)
                            if kept is not None:
                                if out > kept[3]:
                                    tally(kept, -1)
                                    tally(record, 1)
                                    seen[key] = record
                                continue
                            seen[key] = record
                        tally(record, 1)
                    except (AttributeError, KeyError, TypeError, ValueError):
                        continue
        except (OSError, PermissionError):
            continue

    first_seen_str = None
    if first_seen_ts is not None:
        try:
            first_seen_str = date.fromtimestamp(first_seen_ts).isoformat()
        except (OSError, ValueError, OverflowError):
            pass

    return {
        "models": models,
        "total_cost_usd": total_cost_usd,
        "total_tokens": total_tokens,
        "session_count": session_count,
        "first_seen": first_seen_str,
    }


def cmd_stats():
    """Show full session stats summary."""
    stats = _load_stats()
    today = _today_local()
    current, longest = _calculate_streak(stats.get("daily_dates", []), today)

    utf8_print(f"\n{BOLD}usage-pulse stats{RESET}\n")
    utf8_print(f"  First seen:     {_sanitize(str(stats.get('first_seen', 'unknown')))}")
    utf8_print(f"  Total sessions: {stats.get('total_sessions', 0)}")
    utf8_print(f"  Days active:    {len(set(stats.get('daily_dates', [])))}")
    utf8_print(f"  Current streak: {current}d")
    utf8_print(f"  Longest streak: {max(stats.get('longest_streak', 0), longest)}d")

    milestone = _check_milestone(stats.get("total_sessions", 0))
    if milestone:
        utf8_print(f"  Milestone:      {BRIGHT_YELLOW}{milestone}{RESET}")
    utf8_print("")

    # --- API-equivalent cost section ---
    sys.stdout.buffer.write(f"{DIM}  Scanning sessions...{RESET}\r".encode("utf-8"))
    sys.stdout.buffer.flush()
    cost_data = _scan_session_costs()
    sys.stdout.buffer.write(b"                        \r")
    sys.stdout.buffer.flush()

    config = load_config()
    currency_sym = _sanitize(config.get("currency", "$"))[:5]
    rate, _code = _get_exchange_rate(currency_sym)

    model_rows = sorted(
        [(mid, mdata) for mid, mdata in cost_data["models"].items() if mdata["cost_usd"] > 0],
        key=lambda x: x[1]["cost_usd"],
        reverse=True,
    )

    if model_rows:
        utf8_print(f"  {BOLD}API-equivalent cost:{RESET}")
        # Find longest display name for alignment
        display_names = [API_PRICING_DISPLAY.get(mid, mid) for mid, _ in model_rows]
        max_name_len = max(len(n) for n in display_names)

        for (mid, mdata), dname in zip(model_rows, display_names):
            local_cost = mdata["cost_usd"] * rate
            cost_str = f"{currency_sym}{local_cost:,.2f}"
            tok_str = (
                f"{_fmt_tokens(mdata['input'])} in"
                f" \u00b7 {_fmt_tokens(mdata['output'])} out"
                f" \u00b7 {_fmt_tokens(mdata['cache_read'] + mdata['cache_write'])} cached"
            )
            pad = max_name_len - len(dname)
            utf8_print(
                f"    {BRIGHT_WHITE}{dname}:{RESET}{' ' * pad}  "
                f"{BRIGHT_YELLOW}{cost_str:<12}{RESET}{DIM}({tok_str}){RESET}"
            )

        # Built outside the f-string: a backslash escape inside an f-string
        # expression is a SyntaxError before Python 3.12 (PEP 701).
        _sep = "\u2500" * 33
        utf8_print(f"    {DIM}{_sep}{RESET}")
        total_local = cost_data["total_cost_usd"] * rate
        utf8_print(f"    {'Total:':<{max_name_len + 2}}  {BOLD}{BRIGHT_YELLOW}{currency_sym}{total_local:,.2f}{RESET}")
        utf8_print("")

        # Subscription value vs $200/month Max plan
        first_seen = cost_data.get("first_seen")
        months_active = 1.0
        if first_seen:
            try:
                fs = date.fromisoformat(first_seen)
                delta_days = (date.today() - fs).days
                months_active = max(delta_days / 30.44, 1.0)
            except (ValueError, TypeError):
                pass

        subscription_ref_usd = 200.0 * months_active
        ratio = cost_data["total_cost_usd"] / subscription_ref_usd
        utf8_print(
            f"  {DIM}Subscription value: ~{RESET}{BRIGHT_GREEN}{ratio:.1f}x{RESET}"
            f"{DIM} vs API pricing ({months_active:.1f} months \u00d7 $200/mo Max){RESET}"
        )
        utf8_print("")
    else:
        utf8_print(f"  {DIM}No session transcript data found for cost analysis.{RESET}\n")


def _stdin_number(value):
    """Return *value* as a finite float, or None for anything else.

    Booleans are rejected (``True`` is an int in Python) and so are NaN and
    infinity, which json.loads happily produces from ``NaN``/``Infinity``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _parse_stdin_context(raw_stdin):
    """Parse Claude Code's stdin JSON for session context.

    Extracts model name, context window usage, and cost.
    Returns dict with available keys, or empty dict on error.
    """
    if not raw_stdin or not raw_stdin.strip():
        return {}
    try:
        data = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return {}

    result = {}

    # Is this a full repaint payload rather than a fragment? Claude Code always
    # sends the session identity fields on a real status refresh, so their
    # presence means an omitted session-state field genuinely means "off", not
    # "unknown". Without this, a field that is simply dropped when inactive
    # (rather than sent as false) could never clear the badge it set, because
    # the persistence layer treats absent as unchanged.
    try:
        _payload = data.get("data", data)
        _full = isinstance(_payload, dict) and any(
            k in _payload for k in ("session_id", "model", "workspace", "transcript_path")
        )
    except (AttributeError, TypeError):
        _payload, _full = {}, False
    if _full:
        # The session's own identity, used to scope per-session widgets (the
        # subagent counter) to the window being painted. Deliberately absent
        # from _STDIN_CTX_KEYS: persisting it would hand this session's id to
        # every other session's fragment repaints.
        sid = _sanitize(str(_payload.get("session_id") or ""))[:64]
        if sid:
            result["session_id"] = sid
        # Defaults for session state; each block below overwrites when present.
        result["fast_mode"] = False
        result["effort"] = None
        result["thinking"] = None
        result["agent_name"] = None
        result["pr_number"] = None
        result["pr_url"] = None
        result["pr_review_state"] = None
        result["pr_kind"] = None
        result["cache_hit_pct"] = None
        result["cache_read_tokens"] = None
        result["prompt_cache_warm"] = None
        result["prompt_cache_hit_pct"] = None
        result["prompt_cache_expires_at"] = None
        result["spend_limit"] = None
        result["repo_slug"] = None
        result["session_name"] = None

    # Model name
    try:
        model = data.get("data", data).get("model", {})
        display_name = _sanitize(model.get("display_name", ""))
        if display_name:
            # Strip "Claude " prefix: "Claude Opus 4.6" → "Opus 4.6"
            short = display_name.replace("Claude ", "").strip()
            result["model_name"] = short if short else display_name
        else:
            model_id = model.get("id", "")
            if model_id:
                short = _model_short_name(model_id)
                result["model_name"] = short or _sanitize(model_id.split("-")[-1].title())
    except (AttributeError, KeyError):
        pass

    # Context window usage
    try:
        ctx = data.get("data", data).get("context_window", {})
        used_pct = ctx.get("used_percentage")
        # Raw token counts for tokens display mode
        input_tok = ctx.get("total_input_tokens")
        ctx_size = ctx.get("context_window_size")
        # Right after /compact Claude Code repaints in the same session with no
        # percentage, `current_usage: null` and zero totals: nothing of the
        # old context is left. The merge in main() keeps a field a payload
        # leaves out, so the old percentage would stay on the bar (and tokens
        # mode would read "0/1M" next to it). On a full payload that shows the
        # context emptied, the three fields are cleared until the next API
        # response sends real numbers (F12). A bare null percentage with no
        # such sign still means "unchanged".
        if (_full and used_pct is None and isinstance(ctx, dict)
                and (("current_usage" in ctx and ctx["current_usage"] is None)
                     or _stdin_number(input_tok) == 0)):
            result["context_pct"] = None
            result["context_used"] = None
            result["context_limit"] = None
        else:
            if used_pct is not None:
                result["context_pct"] = float(used_pct)
            # used_percentage counts input tokens only (input + cache reads and
            # writes), so the token total does too; output tokens are the last
            # reply, not context.
            if input_tok is not None and ctx_size is not None:
                result["context_used"] = int(input_tok)
                result["context_limit"] = int(ctx_size)
    except (AttributeError, KeyError, ValueError, TypeError):
        pass

    # Cost and lines changed
    try:
        cost = data.get("data", data).get("cost", {})
        total = cost.get("total_cost_usd")
        if total is not None:
            result["cost_usd"] = float(total)
        lines_added = cost.get("total_lines_added")
        lines_removed = cost.get("total_lines_removed")
        if lines_added is not None or lines_removed is not None:
            result["lines_added"] = int(lines_added or 0)
            result["lines_removed"] = int(lines_removed or 0)
    except (AttributeError, KeyError, ValueError, TypeError):
        pass

    # Running Claude Code version. Used by the update check so it doesn't have
    # to shell out to `claude --version` on every repaint.
    try:
        version = data.get("data", data).get("version")
        if version:
            result["cc_version"] = _sanitize(str(version))
    except (AttributeError, KeyError, TypeError):
        pass

    # Reasoning effort, fast mode and thinking state.
    #
    # Claude Code reports the session's effort on stdin as `effort.level`.
    # Earlier versions of usage-pulse read a CLAUDE_CODE_EFFORT_LEVEL
    # environment variable instead, which Claude Code does not actually export
    # — so the effort widget only ever rendered for users who happened to set
    # that variable by hand. stdin is the real source; the env var is kept
    # below as a fallback for anyone relying on the old behaviour.
    # These are *session state*, not accumulating facts: each can be switched
    # off again mid-session. Record the value whenever the field is present —
    # including the off value — because the persistence layer below treats an
    # absent key as "unchanged". Recording only the on value meant a badge
    # could never be cleared: toggle fast mode on once and the ⚡fast marker
    # stuck forever, even as Claude Code reported fast_mode: false.
    try:
        payload = data.get("data", data)
        if "effort" in payload:
            effort = payload.get("effort")
            level = ""
            if isinstance(effort, dict):
                level = _sanitize(str(effort.get("level") or ""))
            result["effort"] = level if level and level != "unset" else None
        if "fast_mode" in payload:
            result["fast_mode"] = payload.get("fast_mode") is True
        if "thinking" in payload:
            thinking = payload.get("thinking")
            if isinstance(thinking, dict) and thinking.get("enabled") is not None:
                result["thinking"] = bool(thinking.get("enabled"))
            else:
                result["thinking"] = None
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Active subagent (agent.name) — present while a subagent drives the turn.
    try:
        agent = data.get("data", data).get("agent")
        if isinstance(agent, dict):
            name = _sanitize(agent.get("name", ""))
            if name:
                result["agent_name"] = name
    except (AttributeError, KeyError, TypeError):
        pass

    # Pull request context (number / url / review_state). Also session state:
    # switching off a PR branch must clear the badge, so an explicitly absent
    # or empty `pr` clears rather than leaving the previous one pinned.
    try:
        payload = data.get("data", data)
        pr = payload.get("pr")
        if "pr" in payload and not (isinstance(pr, dict) and pr.get("number") is not None):
            result["pr_number"] = None
            result["pr_url"] = None
            result["pr_review_state"] = None
            result["pr_kind"] = None
        if isinstance(pr, dict) and pr.get("number") is not None:
            result["pr_number"] = int(pr["number"])
            url = _sanitize(pr.get("url", ""))
            # Only accept http(s) — the URL is embedded in an OSC 8 hyperlink,
            # so a javascript:/file: scheme must never reach the terminal.
            if url.startswith(("https://", "http://")):
                result["pr_url"] = url
            # review_state and kind must be written even when absent: the
            # persistence layer treats a missing key as "unchanged", so a
            # GitHub PR arriving after a GitLab MR (kind omitted ⇒ GitHub)
            # would otherwise keep rendering the stale !N marker, and a PR
            # whose review disappeared would keep its old glyph.
            state = _sanitize(pr.get("review_state", ""))
            result["pr_review_state"] = state if state else None
            # v2.1.234+: "mr" marks a GitLab merge request, so the badge can
            # use GitLab's !N notation instead of #N.
            kind = _sanitize(pr.get("kind", ""))
            result["pr_kind"] = kind if kind else None
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Cache efficiency from the current_usage breakdown. cache_read is billed
    # at ~0.1x input, so the share of input served from cache is the single
    # most useful cost signal available on stdin.
    try:
        cur = data.get("data", data).get("context_window", {}).get("current_usage", {})
        if isinstance(cur, dict):
            cache_read = int(cur.get("cache_read_input_tokens") or 0)
            cache_creation = int(cur.get("cache_creation_input_tokens") or 0)
            fresh_input = int(cur.get("input_tokens") or 0)
            billable = cache_read + cache_creation + fresh_input
            if billable > 0:
                result["cache_read_tokens"] = cache_read
                result["cache_hit_pct"] = (cache_read / billable) * 100.0
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Prompt-cache state (v2.1.251+): `prompt_cache{warm, hit_ratio,
    # expires_at}` where hit_ratio is 0..1 (null while no input has been
    # counted) and expires_at is epoch seconds (null once the cache is cold).
    # Session state, so a present-but-malformed block clears the badge.
    try:
        payload = data.get("data", data)
        if "prompt_cache" in payload:
            pc = payload.get("prompt_cache")
            warm = hit_pct = expires_at = None
            if isinstance(pc, dict):
                if isinstance(pc.get("warm"), bool):
                    warm = pc["warm"]
                ratio = _stdin_number(pc.get("hit_ratio"))
                if ratio is not None:
                    hit_pct = max(0.0, min(ratio, 1.0)) * 100.0
                exp = _stdin_number(pc.get("expires_at"))
                if exp is not None and exp > 0:
                    expires_at = exp
            result["prompt_cache_warm"] = warm
            result["prompt_cache_hit_pct"] = hit_pct
            result["prompt_cache_expires_at"] = expires_at
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Spend limit (claude.ai gateway): `rate_limits.spend_limit{used_percentage,
    # resets_at, used_usd?, limit_usd?, period?}`. Kept out of `_rate_limits`,
    # which the usage bars enumerate. Claude Code drops a window it no longer
    # reports, so a `rate_limits` block without a usable spend_limit clears it.
    try:
        payload = data.get("data", data)
        if "rate_limits" in payload:
            rl_all = payload.get("rate_limits")
            sl = rl_all.get("spend_limit") if isinstance(rl_all, dict) else None
            spend = None
            if isinstance(sl, dict):
                used_pct = _stdin_number(sl.get("used_percentage"))
                if used_pct is not None:
                    resets_iso = None
                    resets_raw = _stdin_number(sl.get("resets_at"))
                    if resets_raw is not None:
                        try:
                            resets_iso = datetime.fromtimestamp(
                                resets_raw, tz=timezone.utc
                            ).isoformat()
                        except (ValueError, OSError, OverflowError):
                            pass
                    used_usd = _stdin_number(sl.get("used_usd"))
                    limit_usd = _stdin_number(sl.get("limit_usd"))
                    period = sl.get("period")
                    period = _sanitize(period).strip().lower()[:16] if isinstance(period, str) else ""
                    spend = {
                        "used_percentage": max(0.0, used_pct),
                        "resets_at": resets_iso,
                        # usd fields are optional; a limit of 0 is not a limit.
                        "used_usd": used_usd if used_usd is not None and used_usd >= 0 else None,
                        "limit_usd": limit_usd if limit_usd is not None and limit_usd > 0 else None,
                        "period": period or None,
                    }
            result["spend_limit"] = spend
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Repository identity parsed from the origin remote: workspace.repo
    # {host, owner, name}. Absent outside a git repo with an origin, so a
    # workspace without a usable repo clears the badge.
    try:
        ws = data.get("data", data).get("workspace")
        if isinstance(ws, dict):
            repo = ws.get("repo")
            slug = None
            if isinstance(repo, dict):
                owner = _sanitize(repo.get("owner") or "").strip() if isinstance(repo.get("owner"), str) else ""
                rname = _sanitize(repo.get("name") or "").strip() if isinstance(repo.get("name"), str) else ""
                if owner and rname:
                    slug = owner + "/" + rname
                    if len(slug) > 60:
                        slug = slug[:59] + "…"
            result["repo_slug"] = slug
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Session name: custom --name / /rename, else the AI-generated title.
    # Absent when the session has neither, which on a full payload clears it
    # (see the defaults block above).
    try:
        payload = data.get("data", data)
        if "session_name" in payload:
            sname = payload.get("session_name")
            sname = _sanitize(sname).strip() if isinstance(sname, str) else ""
            if len(sname) > 40:
                sname = sname[:39] + "…"
            result["session_name"] = sname or None
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Worktree (v2.1.69+)
    try:
        wt = data.get("data", data).get("worktree", {})
        if wt:
            branch = _sanitize(wt.get("branch", ""))
            name = _sanitize(wt.get("name", ""))
            if branch:
                result["worktree_branch"] = branch
            elif name:
                result["worktree_branch"] = name
    except (AttributeError, KeyError):
        pass

    # Rate limits from stdin (v2.1.80+) — eliminates need for OAuth API call
    try:
        rl = data.get("data", data).get("rate_limits", {})
        if rl:
            result["_rate_limits"] = {}
            # Take the two fixed windows plus every model-scoped weekly cap
            # Claude Code reports (seven_day_opus, seven_day_sonnet,
            # seven_day_fable, ...). Enumerating rather than hardcoding means a
            # new model shows up without another release — the previous fixed
            # tuple silently dropped Fable.
            windows = ["five_hour", "seven_day"]
            windows += sorted(
                k for k in rl
                if isinstance(k, str) and k.startswith("seven_day_")
            )
            for window in windows:
                # Guard each window separately. Sharing one try/except across
                # the loop meant a single unparseable `used_percentage` aborted
                # it and silently dropped every window after the bad one — the
                # weekly and per-model bars would vanish because of one field.
                try:
                    w = rl.get(window)
                    if not isinstance(w, dict) or w.get("used_percentage") is None:
                        continue
                    utilization = float(w["used_percentage"])
                    # Convert Unix epoch seconds to ISO string for compatibility
                    # with existing format_reset_time / format_weekly_reset
                    resets_at = w.get("resets_at")
                    resets_iso = None
                    if resets_at is not None:
                        try:
                            resets_iso = datetime.fromtimestamp(
                                float(resets_at), tz=timezone.utc
                            ).isoformat()
                        except (ValueError, OSError, OverflowError, TypeError):
                            pass
                    result["_rate_limits"][window] = {
                        "utilization": utilization,
                        "resets_at": resets_iso,
                    }
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
    except (AttributeError, KeyError, ValueError, TypeError):
        pass

    # MiniMax provider: when running under mxclaude, fix the four known
    # stdin mismatches described at the top of this provider block. Wrapped
    # like every sibling block above so malformed stdin can never crash the
    # status line — on any error we simply leave the Anthropic-shaped values.
    try:
        if "minimax.io" in os.environ.get("ANTHROPIC_BASE_URL", "") and \
           os.environ.get("ANTHROPIC_MODEL", "").startswith("MiniMax-"):
            ctx = data.get("data", data).get("context_window", {}) or {}
            in_tok  = int(ctx.get("total_input_tokens")  or 0)
            out_tok = int(ctx.get("total_output_tokens") or 0)
            total   = in_tok + out_tok

            # 1 + 2: Recompute Context %% from (input + output) against the
            # real per-model window (don't trust the shim's stdin value), and
            # keep tokens-display mode consistent with the corrected window.
            ctx_size = MINIMAX_CONTEXT_SIZES.get(
                os.environ.get("ANTHROPIC_MODEL", ""), 1_000_000,
            )
            if ctx_size > 0:
                result["context_pct"] = (total / ctx_size) * 100.0
                result["context_used"] = total
                result["context_limit"] = ctx_size

            # 3: Recompute cost from token counts using tiered pricing.
            # Anthropic-priced `cost.total_cost_usd` is wrong under mxclaude.
            tier = MINIMAX_PRICING["tier1"]
            if total > MINIMAX_PRICING["tier1"]["max_tokens"]:
                tier = MINIMAX_PRICING["tier2"]
            result["cost_usd"] = (
                in_tok  * tier["in_per_m"] / 1_000_000.0
                + out_tok * tier["out_per_m"] / 1_000_000.0
            )

            # 4: Fetch rate_limits from `mmx quota show` and reshape to Pulse's
            # schema. Only override if stdin didn't already provide rate_limits.
            if not result.get("_rate_limits"):
                try:
                    # Cached for _MINIMAX_PROVIDER_TTL like the provider row:
                    # this runs on every repaint, and `mmx` is a Node start-up
                    # plus a call to MiniMax. _read_minimax_quota resolves the
                    # npm .cmd shim via shutil.which and runs it with no pipes.
                    quota = _minimax_cached_windows(
                        _read_minimax_quota("minimax", primary=True))
                    if quota:
                        result["_rate_limits"] = quota
                except Exception:
                    pass  # renderer will simply not show rate bars
    except (AttributeError, KeyError, ValueError, TypeError, OSError):
        pass

    return result


def _get_heatmap_path():
    """Return path to heatmap data file."""
    return get_account_state_dir() / "heatmap.json"


def _update_heatmap(usage):
    """Update the activity heatmap with current usage data."""
    session_pct = _window_pct(usage, "five_hour")
    weekly_pct = _window_pct(usage, "seven_day")

    # Load existing heatmap. A file that cannot be read yet (another window may
    # be mid-write) skips this update rather than restarting 28 days of data.
    ok, heatmap = _read_state_json(_get_heatmap_path())
    if not ok:
        return
    if not isinstance(heatmap, dict):
        heatmap = {}

    # Keep only entries the renderer can draw: a dict per hour, numbers in it.
    hours = {}
    raw_hours = heatmap.get("hours")
    if isinstance(raw_hours, dict):
        for key, value in raw_hours.items():
            if isinstance(value, dict):
                hours[key] = {
                    "session_pct": _usage_number(value.get("session_pct")) or 0,
                    "weekly_pct": _usage_number(value.get("weekly_pct")) or 0,
                    "samples": int(_usage_number(value.get("samples")) or 0),
                }

    # Current hour key in UTC: YYYY-MM-DDTHH
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%dT%H")

    # Update entry for current hour — track peak session_pct
    entry = hours.get(hour_key, {"session_pct": 0, "weekly_pct": 0, "samples": 0})
    entry["session_pct"] = max(entry.get("session_pct", 0), session_pct)
    entry["weekly_pct"] = max(entry.get("weekly_pct", 0), weekly_pct)
    entry["samples"] = entry.get("samples", 0) + 1
    hours[hour_key] = entry

    # Prune entries older than 28 days (672 hours)
    cutoff = now - timedelta(days=28)
    cutoff_key = cutoff.strftime("%Y-%m-%dT%H")
    hours = {k: v for k, v in hours.items() if k >= cutoff_key}

    heatmap["hours"] = hours

    try:
        _atomic_json_write(_get_heatmap_path(), heatmap, indent=None)
    except OSError:
        pass


def _heatmap_intensity(pct):
    """Return intensity level 0-4 from usage percentage."""
    if pct <= 0:
        return 0
    if pct <= 25:
        return 1
    if pct <= 50:
        return 2
    if pct <= 75:
        return 3
    return 4


def _render_heatmap(config=None):
    """Render a 7-row x 24-col activity heatmap from stored data.

    Rows = days of week (Mon-Sun), cols = hours (0-23).
    Returns a multi-line string.
    """
    if config is None:
        config = load_config()

    theme_name = config.get("theme", "default")
    theme = get_theme_colours(theme_name)

    intensity_chars = ["\u00b7", "\u2591", "\u2592", "\u2593", "\u2588"]  # ·, ░, ▒, ▓, █
    intensity_colors = ["", theme["low"], theme["low"], theme["mid"], theme["high"]]

    # Load heatmap data. The file is untrusted: a damaged or wrong-shaped one
    # draws an empty grid rather than raising.
    _, heatmap = _read_state_json(_get_heatmap_path())
    hours_data = heatmap.get("hours") if isinstance(heatmap, dict) else None
    if not isinstance(hours_data, dict):
        hours_data = {}

    # Build a 7x24 grid (Mon=0 .. Sun=6, hours 0-23)
    # Use the last 7 days from today
    now = datetime.now(timezone.utc)
    grid = [[0] * 24 for _ in range(7)]

    for day_offset in range(7):
        day = now - timedelta(days=(6 - day_offset))
        weekday = day.weekday()  # Mon=0, Sun=6
        for hour in range(24):
            key = day.strftime("%Y-%m-%dT") + f"{hour:02d}"
            entry = hours_data.get(key)
            pct = _usage_number(entry.get("session_pct")) if isinstance(entry, dict) else None
            grid[weekday][hour] = _heatmap_intensity(pct or 0)

    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    lines = []

    # Hour labels header
    header = "     "
    for h in range(24):
        if h % 6 == 0:
            header += f"{h:<3}"
        else:
            header += "   "
    lines.append(header.rstrip())

    # Grid rows
    for weekday in range(7):
        row = f" {day_labels[weekday]} "
        for hour in range(24):
            level = grid[weekday][hour]
            ch = intensity_chars[level]
            color = intensity_colors[level]
            if color:
                row += f"{color}{ch}{RESET}  "
            else:
                row += f"{DIM}{ch}{RESET}  "
        lines.append(row.rstrip())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-session awareness
# ---------------------------------------------------------------------------

def _get_sessions_dir():
    sessions_dir = get_account_state_dir() / SESSION_DIR_NAME
    _secure_mkdir(sessions_dir)
    return sessions_dir


# Stale markers are cleared a bounded batch at a time (see _prune_old_json).
SESSION_PRUNE_BATCH = 100      # stale markers removed per pass, so cleanup never stalls a repaint
SESSION_PRUNE_INTERVAL = 60    # seconds between cleanup passes
SESSION_PRUNE_STAMP = ".pruned"


def _get_session_id(stdin_ctx=None):
    """The id of the window being painted, or None when it has no stable one.

    Claude Code sends a ``session_id`` that is stable for a session and unique
    per session. The parent pid is not: on Windows the status line runs through
    a new Git Bash on every repaint, so it named a new marker each time. A
    repaint without an id of its own (a fragment) takes the one main() stored
    beside the stdin context; failing that the parent pid is used where it is
    stable (not Windows).
    """
    sid = _safe_path_id((stdin_ctx or {}).get("session_id"))
    if not sid:
        owner = _read_json_dict(get_account_state_dir() / "stdin_ctx.json")
        sid = _safe_path_id(owner.get("session_id"))
    if sid:
        return sid
    return os.getppid() if sys.platform != "win32" else None


def _sessions_widget_on(config=None):
    try:
        if config is None:
            config = load_config()
        return bool(config.get("show", {}).get("sessions", False))
    except Exception:
        return False


def _session_prune_due(sessions_dir, now):
    """True (and the stamp renewed) when a cleanup pass is due: once a minute."""
    stamp = sessions_dir / SESSION_PRUNE_STAMP
    try:
        if 0 <= now - stamp.stat().st_mtime < SESSION_PRUNE_INTERVAL:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        stamp.touch()
    except OSError:
        return False
    return True


def _update_session_state(usage, stdin_ctx, config=None):
    try:
        now = time.time()
        sessions_dir = get_account_state_dir() / SESSION_DIR_NAME
        # Stale markers (windows that closed, or a backlog left by older
        # versions) are cleared whether or not the widget is on, a bounded
        # batch per minute.
        if _session_prune_due(sessions_dir, now):
            _prune_old_json(sessions_dir, SESSION_STALE_SECONDS, SESSION_PRUNE_BATCH, now)
        # Nothing reads the markers unless the sessions widget is on.
        if not _sessions_widget_on(config):
            return
        session_id = _get_session_id(stdin_ctx)
        if session_id is None:
            return
        sessions_dir = _get_sessions_dir()
        five = (usage.get("five_hour") if usage else None) or {}
        seven = (usage.get("seven_day") if usage else None) or {}
        state = {
            "pid": session_id,
            "session_pct": five.get("utilization") or 0,
            "weekly_pct": seven.get("utilization") or 0,
            "timestamp": now,
            "model": (stdin_ctx or {}).get("model_name", ""),
        }
        _atomic_json_write(sessions_dir / f"{session_id}.json", state, indent=None)
    except Exception:
        pass


def _get_active_sessions(session_id=None):
    """The other windows' markers still fresh. *session_id* is this window's."""
    sessions_dir = _get_sessions_dir()
    if session_id is None:
        session_id = _get_session_id()
    mine = {str(os.getppid())}
    if session_id is not None:
        mine.add(str(session_id))
    now = time.time()
    active = []
    removed = 0

    def remove(entry):
        nonlocal removed
        try:
            os.unlink(entry.path)
        except OSError:
            pass
        removed += 1

    try:
        with os.scandir(sessions_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    # File age first: a stale marker need not be opened to be
                    # skipped, and a backlog is cleared a batch at a time.
                    if now - entry.stat().st_mtime > SESSION_STALE_SECONDS:
                        if removed < SESSION_PRUNE_BATCH:
                            remove(entry)
                        continue
                    with open(entry.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError, RecursionError):
                    remove(entry)
                    continue
                stamp = _cache_seconds(data.get("timestamp")) if isinstance(data, dict) else None
                if stamp is None or now - stamp > SESSION_STALE_SECONDS:
                    remove(entry)
                    continue
                if str(data.get("pid")) in mine:
                    continue
                active.append(data)
    except OSError:
        pass
    return active


# ---------------------------------------------------------------------------
# Focus timer
# ---------------------------------------------------------------------------

def _get_pomodoro_path():
    return get_state_dir() / POMODORO_FILE


def _read_pomodoro():
    try:
        with open(_get_pomodoro_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("active"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def _write_pomodoro(data):
    _atomic_json_write(_get_pomodoro_path(), data, indent=2)


def _pomodoro_remaining(pomo):
    if not pomo or not pomo.get("active"):
        return (0, False)
    start = pomo.get("start", 0)
    duration = pomo.get("duration_minutes", POMODORO_DEFAULT_MINUTES)
    elapsed = time.time() - start
    focus_seconds = duration * 60
    break_seconds = POMODORO_BREAK_MINUTES * 60
    if elapsed < focus_seconds:
        return (focus_seconds - elapsed, False)
    elif elapsed < focus_seconds + break_seconds:
        return (focus_seconds + break_seconds - elapsed, True)
    return (0, False)


def _render_pomodoro(pomo, theme, bar_width=8):
    if not pomo or not pomo.get("active"):
        return ""
    remaining, is_break = _pomodoro_remaining(pomo)
    if remaining <= 0:
        try:
            pomo["active"] = False
            _write_pomodoro(pomo)
            # The countdown just left the screen; without this the 15s
            # repaint timer it asked for would stay armed until the next
            # tool call or config save.
            sync_status_line_refresh()
        except Exception:
            pass
        return ""
    remaining_min = int(remaining / 60) + (1 if remaining % 60 > 0 else 0)
    if is_break:
        return f"\u2615 Break! {remaining_min}m"
    duration = pomo.get("duration_minutes", POMODORO_DEFAULT_MINUTES)
    elapsed = time.time() - pomo.get("start", 0)
    pct = min(100.0, max(0.0, (elapsed / (duration * 60)) * 100))
    filled = round(pct / 100 * bar_width)
    filled = max(0, min(bar_width, filled))
    colour = bar_colour(pct, theme)
    bar = f"{colour}{FILL * filled}{DIM}{EMPTY * (bar_width - filled)}{RESET}"
    return f"Focus {remaining_min}m {bar}"


def cmd_pomodoro(action, minutes=None):
    """Run the focus-timer command, then re-evaluate the repaint timer.

    Starting or stopping a timer flips whether a countdown is on screen, which
    is one of the two things `refreshInterval` exists for.
    """
    try:
        return _cmd_pomodoro_inner(action, minutes)
    finally:
        try:
            sync_status_line_refresh()
        except Exception:
            pass


def _cmd_pomodoro_inner(action, minutes=None):
    if action == "start":
        duration = POMODORO_DEFAULT_MINUTES
        if minutes is not None:
            try:
                duration = int(minutes)
                if duration < 1 or duration > 240:
                    utf8_print("Duration must be between 1 and 240 minutes.")
                    return
            except ValueError:
                utf8_print(f"Invalid duration: {_sanitize(str(minutes))}")
                return
        _write_pomodoro({"start": time.time(), "duration_minutes": duration, "active": True})
        utf8_print(f"{BRIGHT_GREEN}Focus started: {duration} minutes{RESET}")
        utf8_print(f"  Focus timer will appear in your status line.")
        utf8_print(f"  Stop with: --focus stop")
    elif action == "stop":
        pomo = _read_pomodoro()
        if pomo and pomo.get("active"):
            pomo["active"] = False
            _write_pomodoro(pomo)
            utf8_print("Focus stopped.")
        else:
            utf8_print("No active pomodoro timer.")
    elif action == "status":
        pomo = _read_pomodoro()
        if not pomo or not pomo.get("active"):
            utf8_print("No active pomodoro timer.")
            utf8_print("  Start with: --focus start [minutes]")
            return
        remaining, is_break = _pomodoro_remaining(pomo)
        if remaining <= 0:
            utf8_print("Focus timer has expired.")
            return
        remaining_min = int(remaining / 60) + (1 if remaining % 60 > 0 else 0)
        if is_break:
            utf8_print(f"\u2615 Break time! {remaining_min}m remaining")
        else:
            elapsed_min = int((time.time() - pomo.get("start", 0)) / 60)
            utf8_print(f"{BOLD}Focus:{RESET} {elapsed_min}m / {pomo.get('duration_minutes', 25)}m elapsed, {remaining_min}m remaining")
    else:
        utf8_print(f"Usage: --focus start [minutes] | stop | status")


# ---------------------------------------------------------------------------
# Git drift detector
# ---------------------------------------------------------------------------

def _check_git_drift():
    key = _git_repo_key()
    entry = _read_git_cache(GIT_DRIFT_FILE, key)
    behind, ahead = _cache_count(entry.get("behind")), _cache_count(entry.get("ahead"))
    if _git_cache_fresh(entry, GIT_DRIFT_CACHE_TTL):
        return (behind, ahead)
    # Record the attempt BEFORE git runs. A repaint cancelled mid-git leaves
    # this stamp, so the next one shows the last answer instead of running the
    # same slow git again (and being cancelled again).
    _write_git_cache(GIT_DRIFT_FILE, key, {
        "timestamp": _cache_stamp(entry, "timestamp"), "attempted": time.time(),
        "behind": behind, "ahead": ahead})
    new_behind = new_ahead = 0
    try:
        # One call: "<commits only on HEAD> <commits only on upstream>".
        result = subprocess.run(
            [_GIT_PATH, "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            capture_output=True, text=True, timeout=GIT_RENDER_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return (behind, ahead)  # no answer in time: keep the last, retry after the TTL
    if result.returncode == 0:  # a non-zero exit means no upstream: nothing to drift from
        try:
            left, right = result.stdout.split()
            new_ahead, new_behind = int(left), int(right)
        except ValueError:
            pass
    _write_git_cache(GIT_DRIFT_FILE, key, {
        "timestamp": time.time(), "behind": new_behind, "ahead": new_ahead})
    return (new_behind, new_ahead)


def _render_git_drift():
    try:
        behind, ahead = _check_git_drift()
        if behind == 0 and ahead == 0:
            return ""
        parts = []
        if behind > 0:
            parts.append(f"\u2193{behind}")
        if ahead > 0:
            parts.append(f"\u2191{ahead}")
        return " ".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Files changed counter
# ---------------------------------------------------------------------------

def _count_changed_files():
    key = _git_repo_key()
    entry = _read_git_cache(FILES_CHANGED_FILE, key)
    last = _cache_count(entry.get("count"))
    if _git_cache_fresh(entry, FILES_CHANGED_CACHE_TTL):
        return last
    # Record the attempt before git runs (see _check_git_drift).
    _write_git_cache(FILES_CHANGED_FILE, key, {
        "timestamp": _cache_stamp(entry, "timestamp"), "attempted": time.time(),
        "count": last})
    count = 0
    try:
        result = subprocess.run([_GIT_PATH, "diff", "--name-only"], capture_output=True,
                                text=True, timeout=GIT_RENDER_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return last  # no answer in time: keep the last count, retry after the TTL
    if result.returncode == 0:
        count = len([l for l in result.stdout.strip().split("\n") if l.strip()])
    _write_git_cache(FILES_CHANGED_FILE, key, {"timestamp": time.time(), "count": count})
    return count


def _render_files_changed():
    try:
        count = _count_changed_files()
        if count > 0:
            return f"{count} file{'s' if count != 1 else ''}"
        return ""
    except Exception:
        return ""


def cmd_heatmap():
    """Display the activity heatmap."""
    config = load_config()
    utf8_print(f"\n{BOLD}Activity Heatmap (last 7 days){RESET}\n")
    heatmap = _render_heatmap(config)
    utf8_print(heatmap)

    # Legend
    theme_name = config.get("theme", "default")
    theme = get_theme_colours(theme_name)
    utf8_print(f"\n  Legend: {DIM}\u00b7{RESET} none  {theme['low']}\u2591{RESET} low  {theme['low']}\u2592{RESET} med  {theme['mid']}\u2593{RESET} high  {theme['high']}\u2588{RESET} peak")
    utf8_print("")


_SPEND_PERIOD_SHORT = {"daily": "day", "weekly": "wk", "monthly": "mo"}


def _format_cache_ttl(seconds):
    """Compact time-to-cold for the prompt cache: ``45s``, ``3m``, ``1h``."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    minutes = (seconds + 59) // 60          # round up: 179.2s is "3m", not "2m"
    if minutes < 60:
        return f"{minutes}m"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h" if rem == 0 else f"{hours}h{rem:02d}m"


def _format_spend_amount(usd, currency, rate, code):
    """Format a USD amount in the user's currency; whole amounts drop the cents
    (``$50``) so a spend limit reads ``$12.40/$50``."""
    value = usd * rate
    symbol = "$" if code == "USD" else currency
    if abs(value - round(value)) < 0.005:
        return f"{symbol}{value:.0f}"
    return f"{symbol}{value:.2f}"


# (terminal width, row budget, bar width) of the last --bar-size auto fit. The
# provider rows are drawn after the main line in the same run and follow its
# bars; it is only trusted for the same width and budget.
_SHRINK_MEMO = None


def _shrink_bar_width(bw, config, show, usage, stdin_ctx, plan="", cache_age=None):
    """Narrow *bw* so every row of the line fits the width budget (--bar-size auto).

    The non-bar width is measured, not estimated: the line is drawn at two
    named bar sizes and the difference gives the number of bars on each row, so
    whatever else is on it (cost, lines, model, effort, branch, the stale label,
    a plan name) is counted at its real width. Bars never go below two
    characters; if the line is still too wide then, the fit drops whole widgets
    with a marker.

    _detect_terminal_width tries the COLUMNS Claude Code passes, real TTYs,
    /dev/tty, then /proc on Linux. When all fail the width is unknown and the
    bars keep their size: the host clips at its own edge.
    """
    global _SHRINK_MEMO
    try:
        max_visible = _width_budget(config)
        if not max_visible:
            return bw
        small, medium = BAR_SIZES["small"], BAR_SIZES["small-medium"]
        probes = []
        for size in ("small", "small-medium"):
            # A named size never shrinks, so the probe cannot recurse. Animation
            # is off so the probe does not touch the saved animation state.
            probe = dict(config, bar_size=size, animate="off")
            probes.append(build_status_line(usage, plan, probe, stdin_ctx,
                                            cache_age).split("\n"))
        best = bw
        if len(probes[0]) == len(probes[1]):
            for row_small, row_medium in zip(*probes):
                width_small = _visible_len(row_small)
                bars = (_visible_len(row_medium) - width_small) // (medium - small)
                if bars <= 0:
                    continue
                fixed = width_small - bars * small
                best = min(best, (max_visible - fixed) // bars)
        result = max(2, min(bw, best))
        _SHRINK_MEMO = (_detect_terminal_width(), max_visible, result)
        return result
    except Exception:
        return bw  # if width detection fails, keep the configured size


def build_status_line(usage, plan, config=None, stdin_ctx=None, cache_age=None):
    if config is None:
        config = load_config()

    theme_name = config.get("theme", "default")
    is_rainbow_theme = theme_name == "rainbow"
    animate_raw = config.get("animate", "off")
    # Normalize: True→"rainbow", False→"off", string→itself
    if animate_raw is True:
        animate_raw = "rainbow"
    elif animate_raw is False:
        animate_raw = "off"
    animate = animate_raw != "off"

    use_rainbow = is_rainbow_theme or animate_raw == "rainbow"

    if use_rainbow:
        bar_plain = True
        theme = get_theme_colours(theme_name) if not is_rainbow_theme else THEMES["default"]
    else:
        bar_plain = False
        theme = get_theme_colours(theme_name)

    # Animation state for threshold flash and celebration
    anim_state = _load_anim_state() if animate else {}
    # Animation mode
    anim_mode = animate_raw if animate_raw in ANIMATE_MODES else "off"

    show = config.get("show", DEFAULT_SHOW)
    bw, fit_bars = _bar_width_setting(config)
    bstyle = config.get("bar_style", DEFAULT_BAR_STYLE)
    layout = config.get("layout", DEFAULT_LAYOUT)

    # Shrink-to-fit, only for --bar-size auto. A named size is never narrowed:
    # on a window too narrow for the line, the line is truncated (--max-width)
    # or wrapped (--wrap auto) instead, so "large" always means large.
    if fit_bars:
        bw = _shrink_bar_width(bw, config, show, usage, stdin_ctx, plan, cache_age)

    # list of ((priority, widget_id), text) tuples — sorted before joining.
    # The key carries the widget id so the two-line splitter can tell which
    # segment is which without every append site having to pass it along;
    # tuple ordering still sorts by priority first, with the id as a stable
    # tie-break.
    parts = []
    _wpri = dict(WIDGET_PRIORITY)
    _wpri.update(config.get("widget_priority", {}))
    def _pri(widget_id):
        return (_wpri.get(widget_id, 999), widget_id)

    pace_style = config.get("pace_style", DEFAULT_PACE_STYLE)

    def _bar_fields(widget_id, bar):
        """``(bar + space, space + bar)`` for the layouts that put the bar
        before or after the number; both empty when that bar is switched off
        (--hide session_bar / weekly_bar / context_bar)."""
        if not show.get(f"{widget_id}_bar", True):
            return "", ""
        return f"{bar} ", f" {bar}"

    # Current Session (5-hour block)
    if show.get("session", True):
        five = usage.get("five_hour")
        if five:
            pct = five.get("utilization") or 0
            session_flash = None
            if animate:
                _check_threshold_flash("session", pct, anim_state)
                session_flash = _get_flash_color("session", theme, anim_state)
            bar = make_bar(pct, theme, plain=bar_plain, width=bw, bar_style=bstyle,
                           anim_mode=anim_mode, flash_color=session_flash, config=config)
            bar_before, bar_after = _bar_fields("session", bar)
            reset = format_reset_time(five.get("resets_at")) if show.get("timer", True) else None
            reset_str = f" {reset}" if reset else ""
            pace_str = ""
            if show.get("pace"):
                pace_str = _format_pace(pct, _calc_pace_pct(five.get("resets_at"), 18000), pace_style)
            _s = _pri("session")
            if layout == "compact":
                parts.append((_s, f"S {bar_before}{pct:.0f}%{pace_str}{reset_str}"))
            elif layout == "minimal":
                parts.append((_s, f"{bar_before}{pct:.0f}%{pace_str}{reset_str}"))
            elif layout == "percent-first":
                parts.append((_s, f"{pct:.0f}%{pace_str}{bar_after}{reset_str}"))
            else:  # standard
                history = _read_history() if show.get("sparkline", True) or show.get("runway", True) or show.get("status_message", True) or show.get("burn_rate", True) else []
                label = "Session"
                # providers (3.4.0): name this window's account when rows follow
                try:
                    label = _provider_session_label(config) or label
                except Exception:
                    pass
                if show.get("status_message", True):
                    velocity = _compute_velocity(history)
                    msg, _ = _get_status_message(pct, velocity)
                    label = msg
                spark_str = ""
                if show.get("sparkline", True):
                    spark = _render_sparkline(history)
                    if spark:
                        spark_str = f" {spark}"
                runway_str = ""
                if show.get("runway", True):
                    runway = _estimate_runway(history, pct)
                    if runway:
                        runway_str = f" {runway}"
                burn_str = ""
                if show.get("burn_rate", True) and history:
                    br = _format_burn_rate(history, pct, show_runway=show.get("runway", False))
                    burn_str = f" {br}" if br else ""
                if reset_str and (runway_str or spark_str or burn_str):
                    reset_str = f" \u00b7{reset}"
                parts.append((_s, f"{label} {bar_before}{pct:.0f}%{burn_str}{pace_str}{spark_str}{runway_str}{reset_str}"))
        elif not usage.get("seven_day"):
            # No data at all yet: draw the empty placeholder. A weekly window
            # without a 5-hour one means the account has no 5-hour limit (the
            # API sends ``five_hour: null``), and a 0% bar would claim it has.
            _s = _pri("session")
            bar = make_bar(0, theme, plain=bar_plain, width=bw, bar_style=bstyle)
            bar_before, bar_after = _bar_fields("session", bar)
            if layout == "compact":
                parts.append((_s, f"S {bar_before}0%"))
            elif layout == "minimal":
                parts.append((_s, f"{bar_before}0%"))
            elif layout == "percent-first":
                parts.append((_s, f"0%{bar_after}"))
            else:
                parts.append((_s, f"Session {bar_before}0%"))

    # Weekly Limit (7-day all models)
    if show.get("weekly", True):
        seven = usage.get("seven_day")
        if seven:
            pct = seven.get("utilization") or 0
            weekly_flash = None
            celebrating = False
            if animate:
                _check_threshold_flash("weekly", pct, anim_state)
                weekly_flash = _get_flash_color("weekly", theme, anim_state)
                celebrating = _check_celebration(pct, anim_state)
            bar = make_bar(pct, theme, plain=bar_plain, width=bw, bar_style=bstyle,
                           anim_mode=anim_mode, flash_color=weekly_flash, config=config)
            weekly_reset_str = ""
            if show.get("weekly_timer", True):
                wt_fmt = config.get("weekly_timer_format", DEFAULT_WEEKLY_TIMER_FORMAT)
                if wt_fmt not in WEEKLY_TIMER_FORMATS:
                    wt_fmt = DEFAULT_WEEKLY_TIMER_FORMAT
                wt_prefix = _sanitize(str(config.get("weekly_timer_prefix", DEFAULT_WEEKLY_TIMER_PREFIX)))[:10]
                wt_clock = config.get("clock_format", DEFAULT_CLOCK_FORMAT)
                if wt_clock not in CLOCK_FORMATS:
                    wt_clock = DEFAULT_CLOCK_FORMAT
                wr = format_weekly_reset(seven.get("resets_at"), fmt=wt_fmt, clock=wt_clock)
                if wr:
                    weekly_reset_str = f" {wt_prefix}{wr}"
            pace_str = ""
            if show.get("pace"):
                pace_str = _format_pace(pct, _calc_pace_pct(seven.get("resets_at"), 604800), pace_style)
            bar_before, bar_after = _bar_fields("weekly", bar)
            _w = _pri("weekly")
            if celebrating:
                celeb_label = _render_celebration_label(config)
                parts.append((_w, f"{celeb_label} {bar_before}{pct:.0f}%{pace_str}{weekly_reset_str}"))
            elif layout == "compact":
                parts.append((_w, f"W {bar_before}{pct:.0f}%{pace_str}{weekly_reset_str}"))
            elif layout == "minimal":
                parts.append((_w, f"{bar_before}{pct:.0f}%{pace_str}{weekly_reset_str}"))
            elif layout == "percent-first":
                parts.append((_w, f"{pct:.0f}%{pace_str}{bar_after}{weekly_reset_str}"))
            else:
                parts.append((_w, f"Weekly {bar_before}{pct:.0f}%{pace_str}{weekly_reset_str}"))

    # Per-model weekly caps (Opus / Sonnet / Fable).
    #
    # These are rendered only when the API actually reports a utilization for
    # that model. Claude Pro returns null for the model-scoped windows, so on
    # Pro these bars are simply absent — v3.1.0 drew a hardcoded "Sonnet ━ 0%"
    # in that case, which looked like a real reading of an untouched budget
    # rather than "no data" (issue #46).
    for _widget_id, _usage_key, _label, _short in (
        ("opus", "seven_day_opus", "Opus", "O"),
        ("sonnet", "seven_day_sonnet", "Sonnet", "S"),
        ("fable", "seven_day_fable", "Fable", "F"),
    ):
        if not show.get(_widget_id, True):
            continue
        _entry = usage.get(_usage_key)
        if not _entry or _entry.get("utilization") is None:
            continue
        pct = _entry.get("utilization") or 0
        bar = make_bar(pct, theme, plain=bar_plain, width=bw, bar_style=bstyle)
        pace_str = ""
        if show.get("pace"):
            pace_str = _format_pace(pct, _calc_pace_pct(_entry.get("resets_at"), 604800), pace_style)
        _p = _pri(_widget_id)
        if layout == "compact":
            parts.append((_p, f"{_short} {bar} {pct:.0f}%{pace_str}"))
        elif layout == "minimal":
            parts.append((_p, f"{bar} {pct:.0f}%{pace_str}"))
        elif layout == "percent-first":
            parts.append((_p, f"{pct:.0f}%{pace_str} {bar}"))
        else:
            parts.append((_p, f"{_label} {bar} {pct:.0f}%{pace_str}"))

    # Extra usage (bonus/gifted credits)
    extra = _extra_usage_view(usage.get("extra_usage"))
    extra_enabled_by_user = show.get("extra", False)
    extra_explicitly_hidden = config.get("extra_hidden", False)
    extra_has_credits = extra is not None and extra["limit"] > 0
    if extra_enabled_by_user or (extra_has_credits and not extra_explicitly_hidden):
        _e = _pri("extra")
        currency = _sanitize(config.get("currency", "$"))[:5]
        if extra is not None:
            pct = extra["pct"]
            # Converted like the session cost beside it, and marked when the
            # figure is overdue for a refresh (F17).
            currency, used, limit = _extra_money(extra, currency)
            age_tag = _extra_age_tag(stdin_ctx, config)
            extra_display = config.get("extra_display", "auto")
            if extra_display == "auto":
                extra_display = "amount" if limit == 0 else "full"
            if extra_display == "amount":
                if layout == "compact":
                    parts.append((_e, f"E {currency}{used:.2f}{age_tag}"))
                elif layout == "minimal":
                    parts.append((_e, f"{currency}{used:.2f}{age_tag}"))
                else:
                    parts.append((_e, f"Extra {currency}{used:.2f}{age_tag}"))
            else:
                bar = make_bar(pct, theme, plain=bar_plain, width=bw, bar_style=bstyle)
                if layout == "compact":
                    parts.append((_e, f"E {bar} {currency}{used:.2f}/{currency}{limit:.2f}{age_tag}"))
                elif layout == "minimal":
                    parts.append((_e, f"{bar} {currency}{used:.2f}{age_tag}"))
                elif layout == "percent-first":
                    parts.append((_e, f"{currency}{used:.2f} {bar}{age_tag}"))
                else:
                    parts.append((_e, f"Extra {bar} {currency}{used:.2f}/{currency}{limit:.2f}{age_tag}"))
        elif extra_enabled_by_user:
            if layout == "minimal":
                parts.append((_e, "n/a"))
            elif layout == "compact":
                parts.append((_e, "E: n/a"))
            else:
                parts.append((_e, "Extra: n/a"))

    # Context window usage from stdin context (with pressure warning)
    if stdin_ctx and show.get("context", True):
        ctx_pct = stdin_ctx.get("context_pct")
        if ctx_pct is not None:
            ctx_bar = make_bar(ctx_pct, theme, plain=bar_plain, width=bw, bar_style=bstyle,
                               anim_mode=anim_mode, config=config)
            ctx_fmt = config.get("context_format", "percent")
            ctx_used = stdin_ctx.get("context_used")
            ctx_limit = stdin_ctx.get("context_limit")
            if ctx_used is None or ctx_limit is None:
                model_name = stdin_ctx.get("model_name", "")
                window = MODEL_CONTEXT_WINDOWS.get(model_name, DEFAULT_CONTEXT_WINDOW)
                ctx_limit = window
                ctx_used = int(ctx_pct / 100 * window)
            if ctx_fmt == "tokens":
                pct_label = f"{_fmt_tokens(ctx_used)}/{_fmt_tokens(ctx_limit)}"
            else:
                pct_label = f"{ctx_pct:.0f}%"
            ctx_warning_label = None
            ctx_warning_suffix = ""
            if show.get("context_warning", True):
                ctx_warning_label, ctx_warning_suffix = _format_context_warning(ctx_pct, theme)
                if ctx_warning_suffix is None:
                    ctx_warning_suffix = ""
            _cx = _pri("context")
            cbar_before, cbar_after = _bar_fields("context", ctx_bar)
            if layout == "compact":
                prefix = f"\u26a0 C" if ctx_warning_label else "C"
                parts.append((_cx, f"{prefix} {cbar_before}{pct_label}{ctx_warning_suffix}"))
            elif layout == "minimal":
                parts.append((_cx, f"{cbar_before}{pct_label}{ctx_warning_suffix}"))
            elif layout == "percent-first":
                parts.append((_cx, f"{pct_label}{ctx_warning_suffix}{cbar_after}"))
            else:
                if ctx_warning_label:
                    parts.append((_cx, f"{ctx_warning_label} {cbar_before}{pct_label}{ctx_warning_suffix}"))
                else:
                    parts.append((_cx, f"Context {cbar_before}{pct_label}"))

    # Cost ticker
    if stdin_ctx and show.get("cost", True):
        cost_str = _format_cost(stdin_ctx, config)
        if cost_str:
            parts.append((_pri("cost"), cost_str))

    # Cumulative API-equivalent cost (opt-in, off by default)
    if show.get("cumulative_cost", False):
        try:
            cost_data = _get_cached_cumulative_cost()
            total_usd = cost_data.get("total_cost_usd", 0.0)
            if total_usd > 0:
                currency = _sanitize(config.get("currency", "$"))[:5]
                rate, code = _get_exchange_rate(currency)
                total_local = total_usd * rate
                sym = "$" if code == "USD" else currency
                parts.append((_pri("cumulative_cost"), f"{DIM}All:{RESET} {sym}{total_local:,.2f}"))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    # Rolling 7-day API-equivalent cost (opt-in, off by default)
    if show.get("weekly_cost", False):
        try:
            wdata = _get_cached_weekly_cost()
            wtotal = wdata.get("total_cost_usd", 0.0)
            if wtotal > 0:
                currency = _sanitize(config.get("currency", "$"))[:5]
                rate, code = _get_exchange_rate(currency)
                sym = "$" if code == "USD" else currency
                parts.append((_pri("weekly_cost"), f"{DIM}7d:{RESET} {sym}{wtotal * rate:,.2f}"))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    # Lines changed (from stdin cost data)
    if stdin_ctx and show.get("lines", True):
        la = stdin_ctx.get("lines_added")
        lr = stdin_ctx.get("lines_removed")
        if la is not None or lr is not None:
            a = int(la or 0)
            r = int(lr or 0)
            if a > 0 or r > 0:
                parts.append((_pri("lines"), f"{BRIGHT_GREEN}+{a}{RESET} {BRIGHT_RED}-{r}{RESET}"))

    # Plan name (hidden in minimal layout)
    if layout != "minimal" and show.get("plan", True) and plan:
        parts.append((_pri("plan"), _sanitize(plan)))

    # Streak display
    if show.get("streak", True):
        try:
            stats = _load_stats()
            sd = _get_streak_display(config, stats)
            if sd:
                parts.append((_pri("streak"), sd))
        except Exception:
            pass

    # Model name from stdin context
    if stdin_ctx and show.get("model", True):
        model = stdin_ctx.get("model_name")
        if model:
            model = re.sub(r'\s*\([^)]*context[^)]*\)', '', model).strip()
            if model:
                parts.append((_pri("model"), model))

    # Effort level — from stdin, falling back to the legacy env var.
    if show.get("effort", True):
        effort = (stdin_ctx or {}).get("effort") or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL", "")
        if effort and effort != "unset":
            effort = _sanitize(effort)
            effort_short = _format_effort(
                effort, config.get("effort_format", DEFAULT_EFFORT_FORMAT)
            )
            colour = EFFORT_COLOURS.get(effort, "")
            parts.append((_pri("effort"), f"{colour}{effort_short}{RESET}" if colour else effort_short))

    # Fast mode — Opus running at up to 2.5x output speed, premium pricing.
    # Worth flagging precisely because it costs more than standard Opus.
    if stdin_ctx and show.get("fast_mode", True) and stdin_ctx.get("fast_mode"):
        parts.append((_pri("fast_mode"), f"{BRIGHT_YELLOW}⚡fast{RESET}"))

    # Thinking indicator (opt-in — on by default on current models, so it is
    # only interesting to users who toggle it).
    if stdin_ctx and show.get("thinking", False):
        thinking = stdin_ctx.get("thinking")
        if thinking is not None:
            if thinking:
                parts.append((_pri("thinking"), f"{DIM}think{RESET}"))
            else:
                parts.append((_pri("thinking"), f"{DIM}no-think{RESET}"))

    # Active subagent
    if stdin_ctx and show.get("agent", True):
        agent_name = stdin_ctx.get("agent_name")
        if agent_name:
            parts.append((_pri("agent"), f"{MAGENTA}▸{agent_name}{RESET}"))

    # Cache hit rate (opt-in) — share of billable input served from cache.
    if stdin_ctx and show.get("cache", False):
        hit = stdin_ctx.get("cache_hit_pct")
        if hit is not None:
            # High cache hit is good, so colour it inverted relative to the
            # usage bars: green when most input is cached, red when little is.
            if hit >= 70:
                colour = GREEN
            elif hit >= 40:
                colour = YELLOW
            else:
                colour = RED
            if layout == "minimal":
                parts.append((_pri("cache"), f"{colour}{hit:.0f}%{RESET}"))
            else:
                parts.append((_pri("cache"), f"{DIM}cache{RESET} {colour}{hit:.0f}%{RESET}"))

    # Prompt-cache warmth (opt-in): hit share and time until the cached prefix
    # goes cold, straight from stdin `prompt_cache`. Unlike `cache` above, which
    # derives a ratio from the last turn, this is Claude Code's own session figure.
    if stdin_ctx and show.get("cache_warm", False):
        try:
            warm = stdin_ctx.get("prompt_cache_warm")
            hit = stdin_ctx.get("prompt_cache_hit_pct")
            expires = stdin_ctx.get("prompt_cache_expires_at")
            remaining = None
            if isinstance(expires, (int, float)) and not isinstance(expires, bool):
                remaining = float(expires) - time.time()
            # A persisted "warm" is only as fresh as its expiry: once that has
            # passed the cache really is cold, whatever the last payload said.
            if warm and remaining is not None and remaining <= 0:
                warm = False
            if warm is False:
                parts.append((_pri("cache_warm"), f"{DIM}cache cold{RESET}"))
            elif warm:
                if hit is not None:
                    hit_colour = GREEN if hit >= 70 else (YELLOW if hit >= 40 else RED)
                else:
                    hit_colour = GREEN
                ttl_txt = _format_cache_ttl(remaining) if remaining is not None else ""
                ttl_colour = YELLOW if remaining is not None and remaining < 60 else DIM
                if layout == "minimal":
                    body = f"{hit:.0f}%" if hit is not None else ttl_txt
                    parts.append((_pri("cache_warm"), f"{hit_colour}♨{body}{RESET}"))
                else:
                    head = f"{hit_colour}♨ {hit:.0f}%{RESET}" if hit is not None else f"{hit_colour}♨{RESET}"
                    if hit is not None and ttl_txt:
                        parts.append((_pri("cache_warm"), f"{head} {ttl_colour}· {ttl_txt}{RESET}"))
                    elif ttl_txt:
                        parts.append((_pri("cache_warm"), f"{head} {ttl_colour}{ttl_txt}{RESET}"))
                    else:
                        parts.append((_pri("cache_warm"), head))
        except (TypeError, ValueError, OverflowError):
            pass

    # Spend limit (shown whenever the gateway reports one): money used against
    # the cap, or just the percentage when the gateway omits the usd fields.
    if stdin_ctx and show.get("spend_limit", True):
        try:
            spend = stdin_ctx.get("spend_limit")
            if isinstance(spend, dict) and spend.get("used_percentage") is not None:
                pct = float(spend["used_percentage"])
                # Claude Code stops reporting a window once it resets; a value
                # persisted from before that would be stale, so hide it too.
                stale = False
                if spend.get("resets_at"):
                    try:
                        stale = datetime.fromisoformat(_sanitize(str(spend["resets_at"]))) <= datetime.now(timezone.utc)
                    except (ValueError, TypeError):
                        stale = False
                if not stale:
                    colour = bar_colour(pct, theme)
                    used_usd, limit_usd = spend.get("used_usd"), spend.get("limit_usd")
                    if used_usd is not None and limit_usd:
                        currency = _sanitize(config.get("currency", "$"))[:5]
                        rate, code = _get_exchange_rate(currency)
                        amount = (_format_spend_amount(float(used_usd), currency, rate, code)
                                  + "/" + _format_spend_amount(float(limit_usd), currency, rate, code))
                    else:
                        amount = f"{pct:.0f}%"
                    period = spend.get("period")
                    period_txt = _SPEND_PERIOD_SHORT.get(period, _sanitize(str(period))[:8]) if period else ""
                    period_tail = f" {DIM}{period_txt}{RESET}" if period_txt else ""
                    if layout == "minimal":
                        parts.append((_pri("spend_limit"), f"{colour}{amount}{RESET}"))
                    elif layout == "compact":
                        parts.append((_pri("spend_limit"), f"{colour}{amount}{RESET}{period_tail}"))
                    else:
                        parts.append((_pri("spend_limit"), f"{DIM}spend{RESET} {colour}{amount}{RESET}{period_tail}"))
        except (TypeError, ValueError, OverflowError):
            pass

    # Repository badge (opt-in): owner/name from the origin remote.
    if stdin_ctx and show.get("repo", False):
        slug = stdin_ctx.get("repo_slug")
        if isinstance(slug, str) and slug:
            parts.append((_pri("repo"), f"{DIM}{_sanitize(slug)}{RESET}"))

    # Session name (opt-in): the /rename or --name title, else the AI title.
    if stdin_ctx and show.get("session_name", False):
        sname = stdin_ctx.get("session_name")
        if isinstance(sname, str) and sname:
            parts.append((_pri("session_name"), f"{DIM}{_sanitize(sname)}{RESET}"))

    # Pull request badge (opt-in), clickable via OSC 8 where supported.
    if stdin_ctx and show.get("pr", False):
        pr_number = stdin_ctx.get("pr_number")
        if pr_number is not None:
            state = stdin_ctx.get("pr_review_state", "")
            colour = PR_STATE_COLOURS.get(state, CYAN)
            # GitLab merge requests are conventionally written !N, not #N.
            marker = "!" if stdin_ctx.get("pr_kind") == "mr" else "#"
            label = f"{marker}{pr_number}"
            if state:
                label += f" {PR_STATE_GLYPHS.get(state, state)}"
            parts.append((_pri("pr"), _osc8(stdin_ctx.get("pr_url"), f"{colour}{label}{RESET}")))

    # Worktree branch
    if stdin_ctx and show.get("worktree", True):
        wt_branch = stdin_ctx.get("worktree_branch")
        if wt_branch:
            parts.append((_pri("worktree"), wt_branch))

    # --- Hook-based live features ---
    hook_state = _read_hook_state()
    # The heartbeat and the last tool describe one session. hook_state.json is a
    # snapshot of whichever window's hook fired last, so a window reads its own
    # counters (F40). Before its first tool call it has none, and the snapshot
    # of another session is not shown in its place.
    window_id = (stdin_ctx or {}).get("session_id")
    if window_id:
        own_beat = _read_session_heartbeat(window_id)
        if own_beat:
            hook_state = own_beat
        elif isinstance(hook_state, dict) and hook_state.get("session_id") != _safe_path_id(window_id):
            hook_state = None
    hook_fresh = _is_hook_state_fresh(hook_state)

    if show.get("heartbeat", False) and hook_fresh:
        # hook_state.json is untrusted: a count or start time that is text, a
        # list or NaN must not blank the bar. Unusable values read as 0 tools
        # and 0 s elapsed.
        tool_count = _usage_number(hook_state.get("tool_count", 0))
        tool_count = int(tool_count) if tool_count is not None else 0
        session_start = _cache_seconds(hook_state.get("session_start")) or time.time()
        elapsed = max(0.0, time.time() - session_start)
        frame_idx = int(time.time() * 4) % len(HEARTBEAT_SPINNER)
        spinner = HEARTBEAT_SPINNER[frame_idx]
        parts.append((_pri("heartbeat"), f"[{spinner}] {tool_count} tools {_format_elapsed(elapsed)}"))

    if show.get("last_tool", False) and hook_fresh:
        last_tool = hook_state.get("last_tool", "")
        if last_tool and isinstance(last_tool, str):
            parts.append((_pri("last_tool"), f"Last: {last_tool[:12]}"))

    if show.get("branch", True):
        worktree_shown = stdin_ctx and show.get("worktree", True) and stdin_ctx.get("worktree_branch")
        if not worktree_shown:
            git_branch = _get_git_branch()
            if git_branch:
                parts.append((_pri("branch"), git_branch))

    if show.get("sessions", False):
        try:
            other_sessions = _get_active_sessions()
            if other_sessions:
                count = len(other_sessions)
                parts.append((_pri("sessions"), f"+{count} session{'s' if count != 1 else ''}"))
        except Exception:
            pass

    if show.get("pomodoro", True):
        try:
            pomo = _read_pomodoro()
            if pomo and pomo.get("active"):
                pomo_str = _render_pomodoro(pomo, theme, bar_width=min(bw, 8))
                if pomo_str:
                    parts.append((_pri("pomodoro"), pomo_str))
        except Exception:
            pass

    if show.get("subagents", True):
        try:
            sub_str = _render_subagents(
                config, (stdin_ctx or {}).get("session_id"))
            if sub_str:
                parts.append((_pri("subagents"), sub_str))
        except Exception:
            pass

    if show.get("team", True):
        try:
            team_str = _render_team(config, (stdin_ctx or {}).get("session_id"))
            if team_str:
                parts.append((_pri("team"), team_str))
        except Exception:
            pass

    if show.get("budget", True):
        try:
            budget_str = _render_budget(config, stdin_ctx)
            if budget_str:
                parts.append((_pri("budget"), budget_str))
        except Exception:
            pass

    if show.get("git_drift", False):
        try:
            drift_str = _render_git_drift()
            if drift_str:
                parts.append((_pri("git_drift"), drift_str))
        except Exception:
            pass

    if show.get("files_changed", False):
        try:
            files_str = _render_files_changed()
            if files_str:
                parts.append((_pri("files_changed"), files_str))
        except Exception:
            pass

    # Sort widgets by priority, then join
    parts.sort(key=lambda x: x[0])

    # Staleness indicator. It qualifies the numbers, so it sits right after the
    # first (highest-priority) widget rather than at the end of the line, where
    # a line that is too wide cuts it off first and old figures look current.
    staleness_str = ""
    if show.get("staleness", True) and cache_age is not None:
        staleness_str = _format_staleness(cache_age)
    if staleness_str and parts:
        parts[0] = (parts[0][0], parts[0][1] + staleness_str)
        staleness_str = ""
    line = _join_parts(parts, config) + staleness_str

    # Animation: on = rainbow always moving, off = static theme colours
    if use_rainbow:
        line = rainbow_colorize(line, color_all=False, shimmer=animate, config=config)
    else:
        text_color_code = resolve_text_color(config)
        if text_color_code:
            line = apply_text_color(line, text_color_code)

    # Persist animation state
    if animate:
        _save_anim_state(anim_state)

    return line


def _join_parts(parts, config):
    """Join sorted widget segments into one or two rows.

    Claude Code renders each line of stdout as its own status row, so a
    deliberate two-row layout is just a newline. Two opt-in config keys drive
    it, both lists of widget ids:

    * ``line1_widgets`` — allowlist for row 1; everything else flows to row 2.
    * ``line2_widgets`` — explicit push to row 2; everything else stays on row 1.

    ``line1_widgets`` wins if both are set. With neither, everything stays on a
    single row and the existing ``--wrap`` handling applies as before. A row
    that ends up empty is dropped rather than emitted blank.
    """
    if not isinstance(config, dict):
        return " | ".join(p[1] for p in parts)
    line1_ids = config.get("line1_widgets") or []
    line2_ids = config.get("line2_widgets") or []
    # Only str ids are usable; a nested list/dict would make set() raise
    # TypeError: unhashable type and blank the bar.
    line1_ids = [w for w in line1_ids if isinstance(w, str)] if isinstance(line1_ids, list) else []
    line2_ids = [w for w in line2_ids if isinstance(w, str)] if isinstance(line2_ids, list) else []
    if not line1_ids and not line2_ids:
        return " | ".join(p[1] for p in parts)

    if line1_ids:
        allow = set(line1_ids)
        on_row1 = [p for p in parts if p[0][1] in allow]
        on_row2 = [p for p in parts if p[0][1] not in allow]
    else:
        demote = set(line2_ids)
        on_row1 = [p for p in parts if p[0][1] not in demote]
        on_row2 = [p for p in parts if p[0][1] in demote]

    rows = [" | ".join(p[1] for p in row) for row in (on_row1, on_row2)]
    return "\n".join(r for r in rows if r)


OSC8_START = "\033]8;;"
OSC8_END = "\033]8;;\a"

# Final bytes that terminate a CSI (colour) sequence.
_CSI_TERMINATORS = "ABCDEFGHJKSTfmnsulh"


def _skip_escape(text, i):
    """Return the index just past the escape sequence starting at ``text[i]``.

    Handles the two forms the status line emits:

    * CSI colour codes — ``ESC [ ... <letter>``, short and self-terminating.
    * OSC 8 hyperlinks — ``ESC ] 8 ; ; <url> BEL``. These matter because a URL
      contains letters from the CSI terminator set (the ``h`` of ``https``), so
      scanning for a CSI terminator would stop inside the URL and count the
      rest of it as visible width.

    ``i`` must point at an ESC. Callers use this for width maths, so an
    unterminated sequence consumes the remainder rather than looping.
    """
    n = len(text)
    if i + 1 < n and text[i + 1] == "]":
        # OSC — runs until BEL or the ST terminator (ESC backslash).
        j = i + 2
        while j < n:
            if text[j] == "\a":
                return j + 1
            if text[j] == "\033" and j + 1 < n and text[j + 1] == "\\":
                return j + 2
            j += 1
        return n
    if i + 1 < n and text[i + 1] == "[":
        # CSI, per ECMA-48: parameter bytes 0x30-0x3F, then intermediate bytes
        # 0x20-0x2F, then one final byte 0x40-0x7E. Matching the grammar rather
        # than a hand-listed set of finals means a long true-colour SGR such as
        # ESC[38;2;255;128;64;48;2;0;0;0m is consumed whole; the previous
        # scanner gave up after 25 bytes and counted the tail as visible width.
        j = i + 2
        while j < n and 0x30 <= ord(text[j]) <= 0x3F:
            j += 1
        while j < n and 0x20 <= ord(text[j]) <= 0x2F:
            j += 1
        return j + 1 if j < n else n
    # Bare ESC followed by a single final byte (e.g. ESC c), or a trailing ESC.
    return min(i + 2, n)


def _osc8(url, label):
    """Wrap *label* in an OSC 8 hyperlink when *url* is usable.

    Terminals without hyperlink support ignore the escape and show the label
    unchanged, so this is safe to emit unconditionally. Returns the bare label
    when there is no URL, or when it contains control characters that would
    let it break out of the escape sequence.
    """
    if not url:
        return label
    if any(ch in url for ch in ("\a", "\033", "\n", "\r", ";")):
        return label
    return f"{OSC8_START}{url}\a{label}{OSC8_END}"


try:
    import unicodedata as _unicodedata
except ImportError:  # pragma: no cover - a stripped-down Python
    _unicodedata = None


def _cell_width(ch):
    """Terminal columns one character occupies: 0, 1 or 2.

    Combining marks, format characters and the emoji selector / joiner take no
    column of their own; East Asian wide and fullwidth characters (CJK, most
    emoji such as the lightning bolt, flame and cup this script prints) take
    two. Counting code points instead made a CJK name overflow its row by half
    and over-counted every zero-width character.
    """
    cp = ord(ch)
    if cp < 0xAD or _unicodedata is None:
        return 1
    if cp in (0xFE0F, 0x200D):
        return 0
    if _unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if _unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _scan(text):
    """Yield ``(start, end, width, is_escape)`` for each escape sequence and
    each visible character of *text*. Escape sequences have width 0."""
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\033":
            j = _skip_escape(text, i)
            yield i, j, 0, True
            i = j
        else:
            yield i, i + 1, _cell_width(text[i]), False
            i += 1


def _visible_len(text):
    """Return the width in terminal columns of *text*, ignoring ANSI escapes."""
    return sum(w for _s, _e, w, esc in _scan(text) if not esc)


_ELLIPSIS = "\u2026"  # horizontal ellipsis
_SEPARATOR = " | "
# --wrap auto packs as many rows as the line needs, up to this many. The last
# allowed row drops whole widgets with a trailing marker if it still overflows.
WRAP_MAX_ROWS = 4


def _width_budget(config):
    """Columns one row may use: the terminal width times ``max_width``.

    Returns None when the width is unknown; Claude Code always passes COLUMNS
    and any other host clips at its own edge, so the line is then left alone.
    """
    term_width = _detect_terminal_width()
    if not term_width:
        return None
    max_width_pct = config.get("max_width", DEFAULT_MAX_WIDTH_PCT)
    if not (isinstance(max_width_pct, int) and 20 <= max_width_pct <= 100):
        max_width_pct = DEFAULT_MAX_WIDTH_PCT
    return (term_width * max_width_pct) // 100


def _clip_cells(text, width, force=False):
    """Cut *text* to *width* columns, ending in a dim ellipsis when it cut.

    Never splits a wide character, closes a hyperlink it cut through (or the
    terminal treats every following line as part of the link target) and
    resets colour before the marker. Text that already fits comes back
    unchanged unless *force* asks for the marker regardless.
    """
    if width <= 0:
        return ""
    if not force and _visible_len(text) <= width:
        return text
    keep, used, cut, in_link = width - 1, 0, len(text), False
    for start, end, w, esc in _scan(text):
        if esc:
            if text.startswith(OSC8_START, start):
                in_link = text[start:end] != OSC8_END
            continue
        if used + w > keep:
            cut = start
            break
        used += w
    return (text[:cut] + (OSC8_END if in_link else "") + RESET
            + DIM + _ELLIPSIS + RESET)


def _segment_spans(line, sep=_SEPARATOR):
    """Split *line* into widgets at its visible ``" | "`` separators.

    Returns ``(start, end, width, prefix)`` per widget: raw offsets into
    *line*, its width in columns, and the colour codes in force where it starts
    (so a widget moved to a new row keeps its colour). Splitting on the
    visible text rather than the raw string is what lets rainbow colouring,
    which wraps every character (separators too) in its own escape codes, wrap
    and truncate like any other theme.
    """
    chars, states, active = [], [], ""
    for start, end, width, esc in _scan(line):
        if esc:
            seq = line[start:end]
            if seq.startswith("\033[") and seq.endswith("m"):
                if seq in ("\033[0m", "\033[m"):
                    active = ""
                elif len(active) > 512:
                    active = seq
                else:
                    active += seq
            continue
        chars.append((start, end, line[start], width))
        states.append(active)
    text = "".join(c[2] for c in chars)
    spans, first, seg_start, prefix = [], 0, 0, ""
    pos = text.find(sep)
    while pos != -1:
        spans.append((seg_start, chars[pos][0],
                      sum(c[3] for c in chars[first:pos]), prefix))
        last = pos + len(sep) - 1
        seg_start, prefix, first = chars[last][1], states[last], pos + len(sep)
        pos = text.find(sep, first)
    spans.append((seg_start, len(line), sum(c[3] for c in chars[first:]), prefix))
    return spans


def _spans_width(spans):
    return sum(s[2] for s in spans) + len(_SEPARATOR) * (len(spans) - 1)


def _fit_row(row, max_visible, suffix=""):
    """Fit one row, plus a reserved *suffix* (the update badges), into
    *max_visible* columns.

    The suffix keeps its width: widgets are what give way. Whole widgets are
    dropped from the tail (the lowest priority, since widgets are sorted by it)
    and a dim ellipsis marks the cut; a slice of a bar or word is shown only
    when even the first widget is wider than the row.
    """
    sfx = _visible_len(suffix)
    budget = max_visible - sfx
    if budget < 1:
        return _clip_cells(row + suffix, max_visible)
    if _visible_len(row) <= budget:
        return row + suffix
    spans = _segment_spans(row)
    marker = 1 + len(_ELLIPSIS)                       # a space and the ellipsis
    for keep in range(len(spans) - 1, 0, -1):
        if _spans_width(spans[:keep]) + marker <= budget:
            return (row[:spans[keep - 1][1]] + RESET + " " + DIM + _ELLIPSIS
                    + RESET + suffix)
    return _clip_cells(row[:spans[0][1]], budget, force=len(spans) > 1) + suffix


def _truncate_line(line, config, suffix=""):
    """Fit one row to the effective terminal width, marking what was dropped.

    Applied as the very last step before output so the line never spills
    into Claude Code's side notification area or wraps to the next line.
    *suffix* is appended after fitting and keeps its width (update badges).
    When the width is unknown the line is left alone.
    """
    try:
        max_visible = _width_budget(config)
        if max_visible is None:
            return line + suffix
        return _fit_row(line, max_visible, suffix)
    except Exception:
        return line + suffix


def _wrap_line(line, config, suffix=""):
    """Wrap at ' | ' separators when the line would overflow the terminal.

    If the line fits, it is returned unchanged. Otherwise whole widgets are
    packed greedily into as many rows as needed, up to WRAP_MAX_ROWS; only the
    last allowed row can still overflow, and it drops widgets with a trailing
    marker. *suffix* (update badges) stays on the last row with its width
    reserved. An unknown width leaves the line alone.
    """
    try:
        max_visible = _width_budget(config)
        if max_visible is None:
            return line + suffix
        sfx = _visible_len(suffix)
        if _visible_len(line) + sfx <= max_visible:
            return line + suffix

        spans = _segment_spans(line)
        if len(spans) < 2:
            return _truncate_line(line, config, suffix)

        rows, cur, cur_w = [], [], 0
        for n, span in enumerate(spans):
            width = span[2] + (sfx if n == len(spans) - 1 else 0)
            if not cur:
                cur, cur_w = [span], width
            elif (cur_w + len(_SEPARATOR) + width > max_visible
                    and len(rows) < WRAP_MAX_ROWS - 1):
                rows.append(cur)
                cur, cur_w = [span], width
            else:
                cur.append(span)
                cur_w += len(_SEPARATOR) + width
        rows.append(cur)

        def text(group):
            return group[0][3] + line[group[0][0]:group[-1][1]]

        out = [_fit_row(text(g), max_visible) + RESET for g in rows[:-1]]
        out.append(_fit_row(text(rows[-1]), max_visible, suffix))
        return "\n".join(out)
    except Exception:
        return _truncate_line(line, config, suffix)


def _fit_line(line, config, suffix=""):
    """Apply wrap or truncate depending on the user's --wrap setting.

    The line may already be two rows when ``line1_widgets`` / ``line2_widgets``
    are configured. Each row is fitted independently — measuring the joined
    string would count the newline as a visible column and truncate the rows
    against a shared budget they don't actually share. *suffix* goes after the
    last row.
    """
    if "\n" in line:
        rows = line.split("\n")
        last = len(rows) - 1
        return "\n".join(_fit_line(row, config, suffix if n == last else "")
                         for n, row in enumerate(rows))
    if config.get("wrap") == "auto":
        return _wrap_line(line, config, suffix)
    return _truncate_line(line, config, suffix)


def _finish_line(line, config, stdin_ctx=None):
    """Add the update badges and provider rows to *line*, then fit every row.

    The badges used to be appended before fitting, so a line that was too wide
    lost them first and an update went unseen. They are now reserved width on
    the last main row, and widgets give way instead. Provider rows follow the
    main rows and are fitted on their own.
    """
    badges = append_claude_update_indicator(
        append_update_indicator("", config), config, stdin_ctx)
    main_rows = line.count("\n") + 1
    rows = _append_provider_rows(line, config).split("\n")
    fitted = _fit_line("\n".join(rows[:main_rows]), config, suffix=badges)
    return "\n".join([fitted] + [_fit_line(row, config) for row in rows[main_rows:]])


# ---------------------------------------------------------------------------
# Subagent tracking
# ---------------------------------------------------------------------------
# Claude Code v2.1.198+ runs subagents in the background by default and lets
# them nest (depth 3 by default), so a session can quietly accumulate a lot of
# them. SubagentStart / SubagentStop carry `agent_id` and `agent_type`, which is
# everything needed to keep a live count without polling anything.
#
# State is one directory per session holding one marker file per agent:
# ``subagents/<session_id>/<agent_id>.live``, renamed to ``.done`` on stop.
# Every write is an atomic create or rename of a distinct path. The obvious
# single-JSON design failed two ways in practice: concurrent Start hooks lost
# increments to the read-modify-write race (a parallel fan-out of ten agents
# recorded two), and with several Claude Code windows open — or a session id
# rotation after compaction — whichever session wrote last clobbered the
# counters that every other session then displayed.

SUBAGENT_STATE_TTL = 6 * 3600  # forget stragglers whose Stop hook never fired


def _subagent_root():
    return get_state_dir() / "subagents"


def _safe_path_id(text, limit=64):
    """Reduce an untrusted hook field to a filesystem-safe path component."""
    return re.sub(r"[^A-Za-z0-9._-]", "", str(text or ""))[:limit].strip(".")


def hook_subagent(event):
    """Handle --hook-subagent-start / --hook-subagent-stop. Writes no stdout.

    Hooks must stay silent: anything on stdout for these events lands in the
    transcript as context. Errors are swallowed for the same reason — a
    bookkeeping failure must never surface as a hook error to the user.
    """
    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = _read_stdin_text(65536)
    except (OSError, ValueError):
        pass
    data = {}
    try:
        if raw.strip():
            data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        _record_subagent_event(event, data)
    except Exception:
        pass


def _record_subagent_event(event, data):
    """Record one Start/Stop event as a marker file. Concurrency-safe.

    Ordering is not guaranteed between the two hook processes of a fast agent:
    its Stop can land before its Start. A ``.done`` marker therefore blocks a
    late ``.live`` from being created, and a Start that loses the race anyway
    heals itself by demoting its own marker.
    """
    agent_id = _safe_path_id(data.get("agent_id"))
    session_id = _safe_path_id(data.get("session_id"))
    if not session_id:
        return
    if event == "start" and not agent_id:
        # The spawn still counts toward the cap; give it a unique name.
        agent_id = "noid-%d-%d" % (os.getpid(), time.monotonic_ns())
    elif event == "stop" and not agent_id:
        return

    session_dir = _subagent_root() / session_id
    live = session_dir / (agent_id + ".live")
    done = session_dir / (agent_id + ".done")
    # A pruner in another process can sweep this dir between the mkdir and
    # the marker write (TOCTOU on the staleness check); the second attempt
    # restores the event rather than silently losing it.
    for _attempt in (0, 1):
        _secure_mkdir(session_dir)
        if event == "start":
            if not done.exists():
                try:
                    with open(live, "x", encoding="utf-8") as f:
                        json.dump({"type": _sanitize(str(data.get("agent_type") or ""))[:40],
                                   "started": time.time()}, f)
                except (FileExistsError, OSError):
                    pass
                # Stop may have slipped in between the check and the create.
                if done.exists():
                    try:
                        os.replace(live, done)
                    except OSError:
                        pass
        elif event == "stop":
            try:
                os.replace(live, done)
                # os.replace preserves mtime: an agent that ran past the TTL
                # would otherwise leave a marker that looks ancient, and the
                # next prune would erase the session's whole history.
                os.utime(done, None)
            except OSError:
                # Stop arrived before Start (or Start never made it): leave a
                # tombstone so the late Start can't mark the agent live.
                try:
                    with open(done, "w", encoding="utf-8") as f:
                        json.dump({"stopped": time.time()}, f)
                except OSError:
                    pass
        if live.exists() or done.exists():
            break

    _prune_subagent_dirs(_subagent_root())
    # One-time migration: the pre-3.2.1 single-file state is dead weight now.
    try:
        os.unlink(get_state_dir() / "subagents.json")
    except OSError:
        pass


def _prune_subagent_dirs(root, now=None):
    """Remove session dirs with no marker newer than the TTL.

    Sessions end without a goodbye event, so their dirs would otherwise
    accumulate forever. Anything recent enough to still be rendered is kept.
    Shared by the subagent and agent-team roots; the latter keep their
    markers one level down, in ``tasks/`` and ``teammates/``.
    """
    now = now or time.time()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            # The newest marker decides; the dir's own mtime only matters for
            # an empty dir (a concurrent hook mid-mkdir must not be swept).
            newest = max((_newest_marker_mtime(c) for c in os.scandir(entry.path)),
                         default=entry.stat().st_mtime)
            if now - newest > SUBAGENT_STATE_TTL:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


def _newest_marker_mtime(entry):
    """Return a marker's mtime, or for a marker subdir its newest marker's.

    A subdir's own mtime is no proxy for activity: rewriting a file inside
    it (a teammate going idle again) leaves the directory's mtime untouched,
    so a busy team would look abandoned and be swept mid-session.
    """
    if entry.is_dir(follow_symlinks=False):
        return max((c.stat().st_mtime for c in os.scandir(entry.path)),
                   default=entry.stat().st_mtime)
    return entry.stat().st_mtime


def _count_subagents(session_id, now=None):
    """Return (live, spawned) for one session from its marker files."""
    session_id = _safe_path_id(session_id)
    if not session_id:
        return 0, 0
    now = now or time.time()
    try:
        entries = list(os.scandir(_subagent_root() / session_id))
    except OSError:
        return 0, 0
    # An agent can transiently own both markers (Stop's rename fails against
    # Start's open handle, then tombstones after Start's heal check). The
    # .done twin wins: one spawn, not live.
    done_ids = {e.name[:-5] for e in entries if e.name.endswith(".done")}
    live = 0
    live_ids = set()
    for entry in entries:
        if not entry.name.endswith(".live"):
            continue
        agent = entry.name[:-5]
        live_ids.add(agent)
        if agent in done_ids:
            continue
        try:
            # A marker past the TTL is a crashed agent whose Stop never
            # fired, not a live one.
            if now - entry.stat().st_mtime < SUBAGENT_STATE_TTL:
                live += 1
        except OSError:
            pass
    return live, len(live_ids | done_ids)


def _render_subagents(config, session_id=None):
    """Return the subagent segment, or None when there is nothing to say.

    Counts are scoped to *session_id* — the session this repaint belongs to —
    so one window never displays another window's agents. Claude Code enforces
    its own caps (spawns and concurrency) but does not expose them on stdin or
    in settings, so the denominators come from ``limits`` in usage-pulse's
    own config and default to Claude Code's documented values.
    """
    if not session_id:
        return None
    live, spawned = _count_subagents(session_id)
    if not live and not spawned:
        return None

    limits = config.get("limits", {})
    if not isinstance(limits, dict):
        limits = {}
    spawn_cap = limits.get("subagent_spawns", DEFAULT_LIMITS["subagent_spawns"])
    conc_cap = limits.get("subagent_concurrent", DEFAULT_LIMITS["subagent_concurrent"])

    def _tint(value, cap):
        """Green under half, yellow past half, red past 80% of the cap."""
        if not isinstance(cap, int) or cap <= 0:
            return ""
        ratio = value / cap
        if ratio >= 0.8:
            return RED
        if ratio >= 0.5:
            return YELLOW
        return GREEN

    layout = config.get("layout", DEFAULT_LAYOUT)
    live_part = f"{_tint(live, conc_cap)}{live}{RESET}"
    if isinstance(spawn_cap, int) and spawn_cap > 0:
        total_part = f"{_tint(spawned, spawn_cap)}{spawned}{RESET}{DIM}/{spawn_cap}{RESET}"
    else:
        total_part = f"{spawned}"
    if layout == "minimal":
        return f"⚇{live_part}"
    if layout == "compact":
        return f"⚇{live_part} {total_part}"
    return f"{DIM}agents{RESET} {live_part} live {DIM}·{RESET} {total_part}"


# ---------------------------------------------------------------------------
# Agent-team tracking
# ---------------------------------------------------------------------------
# With agent teams enabled Claude Code fires TeammateIdle, TaskCreated and
# TaskCompleted, which is enough to show how big the team is and how far the
# shared task list has got without polling anything. A teammate becomes known
# the first time its name appears in any of the three.
#
# Whether a teammate is working *right now* is not observable from them: it
# only surfaces when a Task* event happens to carry its name, and claiming or
# doing work fires nothing, so "0 working" would be the normal reading for a
# busy team. The idle/working markers are still kept (they are cheap) but not
# displayed. SubagentStart does fire each time an in-process teammate handles
# a message — but with agent_id/agent_type, no teammate_name — so it is the
# likely future link, once a real capture confirms how those ids map to names.
#
# State follows the subagent marker design above, for the same two reasons
# (parallel hooks lose read-modify-write increments; windows must not clobber
# each other). ``teams/<session_id>/`` holds:
#
# * ``tasks/<task_id>.open``, renamed to ``.done`` on completion, with the
#   same tombstone handling as a subagent's Stop-before-Start;
# * ``teammates/<name>.idle`` or ``teammates/<name>.working``. The state is
#   the file name, not the file's content, so the status line only ever lists
#   the directory and never holds a marker open — on Windows an open handle
#   makes a hook's rename or replace fail (the overlap the subagent tombstone
#   logic has to heal).
#
# In-process teammates (the default backend) run inside the lead's process
# and have no session id of their own, so their events carry the lead's
# session id and land in the lead's directory.

TEAM_LEAD_NAME = "team-lead"  # Claude Code's roster name for the lead itself
_WIN_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL",
                       *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))}


def _team_root():
    return get_state_dir() / "teams"


def _team_key(value):
    """Reduce an untrusted task id or teammate name to a distinct file stem.

    Returns "" for anything unusable: not a string or int, or without a
    single letter or digit (``..``, ``///``). A plain lower-case id passes
    through readable. One that ``_safe_path_id`` had to alter (spaces,
    non-ASCII, path tricks, too long), that has capitals, or that Windows
    reserves as a device name gets a digest suffix, so "Zoë" and "Zo" — or
    "Impl" and "impl" on a case-insensitive disk, or a name written entirely
    in CJK — stay distinct teammates instead of colliding or vanishing.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    raw = _sanitize(str(value)).strip()
    if not any(ch.isalnum() for ch in raw):
        return ""
    key = _safe_path_id(raw)
    if (key != raw or raw != raw.lower()
            or key.split(".")[0].upper() in _WIN_RESERVED_STEMS):
        digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:10]
        key = f"{key[:40]}-{digest}" if key else f"x-{digest}"
    return key


def hook_team(event):
    """Handle --hook-teammate-idle / --hook-task-created / --hook-task-completed.

    Unlike the subagent hooks, these events can steer the team rather than
    just report on it: exit code 2 keeps a teammate from going idle, deletes
    a new task, or refuses a completion, and ``{"continue": false}`` on stdout
    stops a teammate. So this prints nothing and lets nothing escape — a
    failure costs the status line one event, never the team its work.

    ``BaseException`` on purpose, KeyboardInterrupt included: a stray
    ``sys.exit(2)`` on this path would block the team, and exit 0 is the only
    status that is harmless for all three events. An interrupt has already
    reached Claude Code itself, and there is nothing here to clean up.
    """
    try:
        _record_team_event(event, _read_hook_payload())
    except BaseException:
        pass


def _read_hook_payload(limit=1 << 20):
    """Return the hook's stdin JSON object, or {} for anything else.

    Reads bytes and decodes UTF-8 itself: that is what Claude Code sends, but
    a text-mode stdin on Windows decodes with the ANSI code page and garbles a
    non-ASCII task subject. The cap is roomier than the subagent hooks'
    because ``task_description`` is free text, and a payload cut off mid-JSON
    would lose the whole event.
    """
    raw = ""
    try:
        if not sys.stdin.isatty():
            stream = getattr(sys.stdin, "buffer", None)
            if stream is not None:
                raw = stream.read(limit).decode("utf-8-sig", "replace")
            else:
                raw = sys.stdin.read(limit)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError, RecursionError):
        data = {}
    return data if isinstance(data, dict) else {}


def _record_team_event(event, data):
    """Record one team event as marker files. Concurrency-safe.

    A missing or unusable task id drops the event: unlike a subagent spawn
    there is no cap to protect, so a made-up id could only ever surface as a
    phantom open task. The lead is not a teammate, so its own task actions
    update the list but never the teammate count.
    """
    session_id = _safe_path_id(data.get("session_id"))
    if not session_id or event not in ("idle", "created", "completed"):
        return
    session_dir = _team_root() / session_id
    name = _team_key(data.get("teammate_name"))
    member = "" if name == TEAM_LEAD_NAME else name

    if event == "idle":
        if not member:
            return
        _set_teammate_state(session_dir / "teammates", member, "idle")
    else:
        task_id = _team_key(data.get("task_id"))
        if not task_id:
            return
        subject = " ".join(_sanitize(data.get("task_subject") or "").split())[:60]
        _record_task(session_dir / "tasks", task_id, event, subject, name)
        # Creating or completing a task is work: the teammate is mid-turn.
        # A turn-end completion is still followed by its TeammateIdle (the
        # hook lifecycle runs TaskCompleted, then Stop, then TeammateIdle).
        if member:
            _set_teammate_state(session_dir / "teammates", member, "working")

    _prune_subagent_dirs(_team_root())


def _record_task(tasks_dir, task_id, event, subject, owner):
    """Create or complete one task marker; mirrors ``_record_subagent_event``.

    A completion that overtakes its creation leaves a tombstone ``.done``
    (no ``created`` stamp) that keeps the late creation from opening it. A
    ``.done`` that *does* carry one saw a whole create→complete cycle, so a
    fresh creation for that id is a new task reusing it (ids are small
    counters), not a race — it reopens instead of being swallowed.
    """
    pending = tasks_dir / (task_id + ".open")
    done = tasks_dir / (task_id + ".done")
    # Same TOCTOU retry as the subagent writer: a pruner in another process
    # can sweep the dir between the mkdir and the marker write.
    for _attempt in (0, 1):
        _secure_mkdir(tasks_dir)
        if event == "created":
            if done.exists() and not _is_task_tombstone(done):
                try:
                    os.unlink(done)
                except OSError:
                    pass
            if not done.exists():
                try:
                    with open(pending, "x", encoding="utf-8") as f:
                        json.dump({"subject": subject, "owner": owner,
                                   "created": time.time()}, f)
                except OSError:  # FileExistsError: a repeat — first one stands
                    pass
                # A completion may have slipped in between check and create.
                if done.exists():
                    try:
                        os.replace(pending, done)
                    except OSError:
                        pass
        else:
            try:
                os.replace(pending, done)
                # os.replace keeps the creation mtime; without a touch a task
                # that took longer than the TTL would look ancient to the pruner.
                os.utime(done, None)
            except OSError:
                # No .open to rename: the completion came first, the creation
                # was never recorded, or this is a repeat. "x" keeps a repeat
                # idempotent — the first record stands.
                try:
                    with open(done, "x", encoding="utf-8") as f:
                        json.dump({"subject": subject, "owner": owner,
                                   "completed": time.time()}, f)
                except OSError:
                    pass
        if pending.exists() or done.exists():
            break


def _is_task_tombstone(path):
    """True unless *path* records a task whose creation was also seen."""
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        return not (isinstance(record, dict) and "created" in record)
    except (OSError, ValueError):
        return True


def _set_teammate_state(mates_dir, member, state):
    """Mark *member* idle or working without ever leaving it markerless.

    A transition renames the old state's marker onto the new name, then
    rewrites it to refresh the mtime (a rename keeps the old one). Nothing is
    ever deleted: a rename always leaves its target behind, so however two
    writers for the same teammate interleave, a marker survives. Deleting the
    other state's marker — even only when it looks older — cannot promise
    that: between one writer's check and its unlink the other can rewrite
    that marker and delete this one, and both are gone. A race can instead
    leave both names in place; ``_count_team`` then takes the newer mtime,
    and the next transition renames them back into one.
    """
    other = "working" if state == "idle" else "idle"
    marker = mates_dir / f"{member}.{state}"
    for _attempt in (0, 1):
        _secure_mkdir(mates_dir)
        try:
            os.replace(mates_dir / f"{member}.{other}", marker)
        except OSError:
            pass  # nothing to move: a new teammate, or already in this state
        try:
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({"state": state, "ts": time.time()}, f)
        except OSError:
            pass
        if marker.exists():
            break


def _count_team(session_id, now=None):
    """Return (teammates, working, tasks_open, tasks_done) for one session.

    Only lists directories; never opens a marker. A teammate whose last word
    was "working" but has been silent past the TTL was stopped or crashed
    without an idle event, so it stops counting as working — the straggler
    rule a subagent's ``.live`` marker follows. An open task is never aged
    out on its own (a pending task has no missed goodbye event, and expiring
    it would paint an unfinished list as done), but once nothing in the
    session has changed for the TTL the pruner removes the whole session dir,
    open tasks included.
    """
    session_id = _safe_path_id(session_id)
    if not session_id:
        return 0, 0, 0, 0
    now = now or time.time()
    session_dir = _team_root() / session_id

    try:
        task_names = [e.name for e in os.scandir(session_dir / "tasks")]
    except OSError:
        task_names = []
    # As with subagents, a task caught owning both markers is done.
    done_ids = {n[:-5] for n in task_names if n.endswith(".done")}
    open_ids = {n[:-5] for n in task_names if n.endswith(".open")} - done_ids

    marks = {}
    try:
        mate_entries = list(os.scandir(session_dir / "teammates"))
    except OSError:
        mate_entries = []
    for entry in mate_entries:
        member, _, state = entry.name.rpartition(".")
        if not member or state not in ("idle", "working"):
            continue
        try:
            marks.setdefault(member, {})[state] = entry.stat().st_mtime
        except OSError:
            continue
    working = 0
    for states in marks.values():
        busy = states.get("working")
        if (busy is not None and busy > states.get("idle", float("-inf"))
                and now - busy < SUBAGENT_STATE_TTL):
            working += 1
    return len(marks), working, len(open_ids), len(done_ids)


def _render_team(config, session_id=None):
    """Return the agent-team segment, or None when there is nothing to say.

    Scoped to the painting session like the subagent counter. The team part
    is the roster size — teammates seen so far — not a working count, which
    these hooks cannot observe (see the section comment). A session that
    uses the shared task list without teammates shows only the tasks; a team
    whose tasks never reached the list shows only the roster. Only the task
    tally changes colour: green once every task is done.
    """
    if not session_id:
        return None
    mates, _working, tasks_open, tasks_done = _count_team(session_id)
    tasks = tasks_open + tasks_done
    if not mates and not tasks:
        return None

    layout = config.get("layout", DEFAULT_LAYOUT)
    mate_part = f"{mates}" if mates else ""
    task_colour = GREEN if tasks and not tasks_open else ""
    task_part = f"{task_colour}{tasks_done}{RESET}{DIM}/{tasks}{RESET}" if tasks else ""
    if layout == "minimal":
        return f"⚑{mate_part or task_part}"
    if layout == "compact":
        return "⚑" + " ".join(p for p in (mate_part, task_part) if p)
    pieces = []
    if mate_part:
        pieces.append(f"{DIM}team{RESET} {mate_part}")
    if task_part:
        pieces.append(f"{DIM}tasks{RESET} {task_part}")
    return f" {DIM}·{RESET} ".join(pieces)


def _render_budget(config, stdin_ctx):
    """Return a spend-against-budget segment, or None.

    ``--max-budget-usd`` is a Claude Code CLI flag with no settings key and no
    environment variable, so its value cannot be discovered from here. The
    ceiling is therefore usage-pulse's own ``budget_usd`` setting, which the
    user sets to match whatever they pass on the command line.
    """
    try:
        budget = float(config.get("budget_usd") or 0)
    except (TypeError, ValueError):
        return None
    if budget <= 0:
        return None
    spent = (stdin_ctx or {}).get("cost_usd")
    if spent is None:
        return None
    try:
        spent = float(spent)
    except (TypeError, ValueError):
        return None

    pct = min(100.0, (spent / budget) * 100.0) if budget else 0.0
    theme = get_theme_colours(config.get("theme", "default"))
    bw = _bar_width_setting(config)[0]
    bar = make_bar(pct, theme, width=max(2, bw // 2),
                   bar_style=config.get("bar_style", DEFAULT_BAR_STYLE))
    currency = _sanitize(config.get("currency", "$"))[:5]
    rate, code = _get_exchange_rate(currency)
    sym = "$" if code == "USD" else currency
    layout = config.get("layout", DEFAULT_LAYOUT)
    if layout == "minimal":
        return f"{bar} {pct:.0f}%"
    return f"{DIM}budget{RESET} {bar} {sym}{spent * rate:,.2f}{DIM}/{sym}{budget * rate:,.2f}{RESET}"


# ---------------------------------------------------------------------------
# Provider rows (3.4.0) — one extra status row per other subscription
# ---------------------------------------------------------------------------
# Claude Code renders each stdout line as its own row, and _fit_line fits each
# row independently, so the rows are simply appended after the main line.
# Labels share one column, and every row's " | Weekly" separator sits at the
# same column as the main line's first separator when the main line starts
# with this account's label (providers_label_main).

def _provider_main_label(config):
    """Label of this window's own account (the main line)."""
    entries = _normalize_providers((config or {}).get("providers"))
    active = _active_provider_id(config, entries)
    for entry in entries:
        if entry["id"] == active:
            return entry.get("label") or _provider_default_label(entry)
    return _default_claude_label(_claude_config_dir())


def _provider_label_width(config, entries=None):
    """Shared label column: the widest row label and this account's label."""
    if entries is None:
        entries = _provider_row_entries(config)
    labels = [_provider_row_label(e) for e in entries]
    labels.append(_provider_main_label(config))
    return max(len(label) for label in labels)


def _provider_session_label(config):
    """``Claude (Work) 5h`` for the main line's session bar, or None.

    Only when provider rows are actually drawn, so a user without providers
    keeps the familiar ``Session`` label.
    """
    if not isinstance(config, dict) or not config.get("providers_label_main", True):
        return None
    if not (config.get("show") or {}).get("providers", True):
        return None
    entries = _provider_row_entries(config)
    if not entries:
        return None
    width = _provider_label_width(config, entries)
    return f"{_provider_main_label(config).ljust(width)} 5h"


def _provider_bar_width(config, label_width):
    """Row bar width: the configured size. With --bar-size auto it is never
    wider than the main line's shrunk bars, and shrinks further before the
    row would need truncating."""
    bw, fit = _bar_width_setting(config)
    if not fit:
        return bw
    try:
        term_width = _detect_terminal_width()
        if not term_width:
            return bw
        max_width_pct = config.get("max_width", DEFAULT_MAX_WIDTH_PCT)
        if not (isinstance(max_width_pct, int) and 20 <= max_width_pct <= 100):
            max_width_pct = DEFAULT_MAX_WIDTH_PCT
        effective = (term_width * max_width_pct) // 100
        memo = _SHRINK_MEMO
        if memo and memo[0] == term_width and memo[1] == effective:
            # The main line was just fitted at this width: follow its bars.
            main_max = memo[2]
        else:
            show = config.get("show", DEFAULT_SHOW)
            # No measured main line in this run (a preview, say): estimate.
            main_bars = sum(1 for k in ("session", "weekly", "context") if show.get(k, True)) or 1
            main_overhead = main_bars * 18 + max(main_bars - 1, 0) * 3 + 15
            main_max = max(2, (effective - main_overhead) // main_bars)
        row_max = max(2, (effective - (label_width + 2 * 18 + 3)) // 2)
        bw = max(2, min(bw, main_max, row_max))
    except Exception:
        pass
    return bw


def _render_provider_row(label, usage, config, label_width, bar_width, stale=False, tier=None):
    """Return ``(head, tail)`` for one provider row.

    head = label + 5-hour window, tail = weekly window, styled like the main
    line's bars for the configured layout, theme and timer settings. A missing
    window draws an empty bar and ``--``; stale data draws dim bars and ``~29%``.
    """
    theme_name = config.get("theme", "default")
    animate_raw = config.get("animate", "off")
    use_rainbow = theme_name == "rainbow" or animate_raw in (True, "rainbow")
    theme = THEMES["default"] if theme_name == "rainbow" else get_theme_colours(theme_name)
    bstyle = config.get("bar_style", DEFAULT_BAR_STYLE)
    layout = config.get("layout", DEFAULT_LAYOUT)
    show = config.get("show", DEFAULT_SHOW)
    pace_style = config.get("pace_style", DEFAULT_PACE_STYLE)
    usage = usage or {}

    def _window(key, window_seconds):
        w = usage.get(key)
        if not w or w.get("utilization") is None:
            bar = make_bar(0, theme, plain=True, width=bar_width, bar_style=bstyle)
            return bar, f"{DIM}--{RESET}", None
        pct = w.get("utilization") or 0
        bar = make_bar(pct, theme, plain=use_rainbow and not stale, width=bar_width,
                       bar_style=bstyle, flash_color=DIM if stale else None, config=config)
        pct_txt = f"{DIM}~{pct:.0f}%{RESET}" if stale else f"{pct:.0f}%"
        # The main line's pace arrow, on live data only: a stale row's clock
        # has moved on while its number has not.
        if show.get("pace") and not stale:
            pct_txt += _format_pace(pct, _calc_pace_pct(w.get("resets_at"), window_seconds), pace_style)
        return bar, pct_txt, w.get("resets_at")

    head = label.ljust(label_width)
    if show.get("session", True):
        bar, pct_txt, resets = _window("five_hour", 18000)
        reset = format_reset_time(resets) if resets and show.get("timer", True) else None
        reset_str = f" {reset}" if reset else ""
        if layout == "compact":
            head += f" S {bar} {pct_txt}{reset_str}"
        elif layout == "minimal":
            head += f" {bar} {pct_txt}{reset_str}"
        elif layout == "percent-first":
            head += f" {pct_txt} {bar}{reset_str}"
        else:
            head += f" 5h {bar} {pct_txt}{reset_str}"

    tail = ""
    if show.get("weekly", True):
        bar, pct_txt, resets = _window("seven_day", 604800)
        wr_str = ""
        if resets and show.get("weekly_timer", True):
            wt_fmt = config.get("weekly_timer_format", DEFAULT_WEEKLY_TIMER_FORMAT)
            if wt_fmt not in WEEKLY_TIMER_FORMATS:
                wt_fmt = DEFAULT_WEEKLY_TIMER_FORMAT
            wt_prefix = _sanitize(str(config.get("weekly_timer_prefix", DEFAULT_WEEKLY_TIMER_PREFIX)))[:10]
            wt_clock = config.get("clock_format", DEFAULT_CLOCK_FORMAT)
            if wt_clock not in CLOCK_FORMATS:
                wt_clock = DEFAULT_CLOCK_FORMAT
            wr = format_weekly_reset(resets, fmt=wt_fmt, clock=wt_clock)
            if wr:
                wr_str = f" {wt_prefix}{wr}"
        if layout == "compact":
            tail = f"W {bar} {pct_txt}{wr_str}"
        elif layout == "minimal":
            tail = f"{bar} {pct_txt}{wr_str}"
        elif layout == "percent-first":
            tail = f"{pct_txt} {bar}{wr_str}"
        else:
            tail = f"Weekly {bar} {pct_txt}{wr_str}"
    if tier and show.get("plan", False):
        tail = f"{tail} {DIM}{tier}{RESET}" if tail else f"{DIM}{tier}{RESET}"
    return head, tail


def _render_provider_rows(config, now=None, main_col=None):
    """Render every provider row. Returns ``(rows, separator_column)``.

    Heads are padded to the widest head (or ``main_col``, the main line's
    separator column, when that is wider) so every ``|`` lines up.
    """
    entries = _provider_row_entries(config)
    if not entries:
        return [], None
    rows = _collect_provider_rows(config, now)
    if not rows:
        return [], None
    label_width = _provider_label_width(config, entries)
    bar_width = _provider_bar_width(config, label_width)
    built = [_render_provider_row(r["label"], r["usage"], config, label_width, bar_width,
                                  stale=r["stale"], tier=r.get("tier"))
             for r in rows]
    col = max(_visible_len(head) for head, _ in built)
    if main_col and main_col > col:
        col = main_col
    theme_name = config.get("theme", "default")
    animate_raw = config.get("animate", "off")
    use_rainbow = theme_name == "rainbow" or animate_raw in (True, "rainbow")
    animate = animate_raw not in ("off", False, None)
    text_color = None if use_rainbow else resolve_text_color(config)
    out = []
    for head, tail in built:
        pad = " " * max(0, col - _visible_len(head))
        row = f"{head}{pad} | {tail}" if tail else head
        if use_rainbow:
            row = rainbow_colorize(row, color_all=False, shimmer=animate, config=config)
        elif text_color:
            row = apply_text_color(row, text_color)
        out.append(row)
    return out, col


def _strip_ansi(text):
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            i = _skip_escape(text, i)
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _raw_offset(text, visible_count):
    """Raw index just after ``visible_count`` visible characters of ``text``."""
    seen, i = 0, 0
    while i < len(text) and seen < visible_count:
        if text[i] == "\033":
            i = _skip_escape(text, i)
            continue
        seen += 1
        i += 1
    return i


def _append_provider_rows(line, config):
    """Append the provider rows to a rendered status line (never raises).

    Called at every output site after the update indicators (so they stay on
    the main row) and before ``_fit_line`` (which fits each row on its own).
    With ``providers_label_main`` the main line starts with this account's
    label; its first `` | `` is then padded out to the rows' separator column.
    """
    try:
        if not isinstance(config, dict) or not isinstance(line, str):
            return line
        if not (config.get("show") or {}).get("providers", True):
            return line
        if not _provider_row_entries(config):
            return line
        first, nl, rest = line.partition("\n")
        main_col = None
        plain = _strip_ansi(first)
        if config.get("providers_label_main", True) and \
                plain.startswith(_provider_main_label(config) + " "):
            idx = plain.find(" | ")
            if idx > 0:
                main_col = idx
        rows, col = _render_provider_rows(config, main_col=main_col)
        if not rows:
            return line
        if main_col is not None and col and col > main_col:
            cut = _raw_offset(first, main_col)
            first = first[:cut] + " " * (col - main_col) + first[cut:]
        return first + nl + rest + "\n" + "\n".join(rows)
    except Exception:
        return line


# ---------------------------------------------------------------------------
# Subagent status line (the `subagentStatusLine` setting)
# ---------------------------------------------------------------------------

SUBAGENT_STATUS_GLYPHS = {
    "running": "▸",    # ▸
    "active": "▸",
    "pending": "·",    # ·
    "queued": "·",
    "done": "✓",       # ✓
    "completed": "✓",
    "success": "✓",
    "failed": "✗",     # ✗
    "error": "✗",
    "cancelled": "✗",
}
SUBAGENT_STATUS_COLOURS = {
    "running": CYAN, "active": CYAN,
    "pending": DIM, "queued": DIM,
    "done": GREEN, "completed": GREEN, "success": GREEN,
    "failed": RED, "error": RED, "cancelled": YELLOW,
}


def _elapsed_from_start(start_time):
    """Seconds since *start_time*, which may be epoch ms, epoch s, or ISO."""
    if start_time is None:
        return None
    try:
        value = float(start_time)
        # Anything past ~2001 in milliseconds is far beyond a plausible epoch
        # second, so treat large values as ms.
        if value > 1e11:
            value /= 1000.0
        delta = time.time() - value
        return delta if delta >= 0 else None
    except (TypeError, ValueError):
        pass
    ts = _parse_transcript_ts(start_time)
    if ts is None:
        return None
    delta = time.time() - ts
    return delta if delta >= 0 else None


def _render_subagent_row(task, config, columns=None):
    """Build one subagent row body, or "" to hide it.

    Mirrors the main bar's vocabulary — same theme, same bar style — so the
    agent panel reads as part of the same tool rather than a separate widget.
    """
    if not isinstance(task, dict):
        return ""
    # `name` is optional; fall back to the subagent type (agentType, v2.1.293+:
    # "Explore", "code-reviewer", ...) before the generic task kind.
    name = _sanitize(str(task.get("name") or task.get("agentType") or task.get("type") or "agent"))[:28]
    status = _sanitize(str(task.get("status") or "")).lower()
    glyph = SUBAGENT_STATUS_GLYPHS.get(status, "·")
    colour = SUBAGENT_STATUS_COLOURS.get(status, "")

    parts = [f"{colour}{glyph}{RESET} {name}" if colour else f"{glyph} {name}"]

    model_short = _model_short_name(str(task.get("model") or ""))
    if model_short:
        parts.append(f"{DIM}{model_short}{RESET}")

    effort = task.get("effort")
    if effort not in (None, "", "unset"):
        # Either a level string or a numeric token budget.
        if isinstance(effort, (int, float)) and not isinstance(effort, bool):
            parts.append(f"{DIM}{_fmt_tokens(int(effort))}{RESET}")
        else:
            rendered = _format_effort(
                _sanitize(str(effort)).lower(),
                config.get("effort_format", DEFAULT_EFFORT_FORMAT),
            )
            if rendered:
                parts.append(f"{DIM}{rendered}{RESET}")

    # Context bar, when Claude Code resolved the model's window (v2.1.205+).
    try:
        tokens = int(task.get("tokenCount") or 0)
        window = int(task.get("contextWindowSize") or 0)
    except (TypeError, ValueError):
        tokens, window = 0, 0
    if tokens and window > 0:
        pct = max(0.0, min(100.0, (tokens / window) * 100.0))
        theme = get_theme_colours(config.get("theme", "default"))
        bw = _bar_width_setting(config)[0]
        bar = make_bar(pct, theme, width=max(2, bw // 2),
                       bar_style=config.get("bar_style", DEFAULT_BAR_STYLE))
        parts.append(f"{bar} {pct:.0f}%")
    elif tokens:
        parts.append(f"{DIM}{_fmt_tokens(tokens)}{RESET}")

    elapsed = _elapsed_from_start(task.get("startTime"))
    if elapsed is not None:
        parts.append(f"{DIM}{_format_elapsed(elapsed)}{RESET}")

    row = f" {DIM}·{RESET} ".join(parts)

    # `columns` is the usable row width Claude Code offers; respect it.
    try:
        width = int(columns or 0)
    except (TypeError, ValueError):
        width = 0
    if width > 0 and _visible_len(row) > width:
        row = _clip_visible(row, width)
    return row


def _clip_visible(text, width):
    """Clip to *width* terminal columns, ending in a dim ellipsis when it cut.

    Keeps escapes intact, resets colour and never splits a wide character.
    """
    return _clip_cells(text, width)


def cmd_subagent_status_line():
    """Handle --subagent-status-line: render the agent panel's rows.

    Claude Code sends every visible subagent row as one JSON object on stdin
    and expects one JSON line of ``{"id", "content"}`` per row we want to
    override. Rows we say nothing about keep their default rendering, so any
    failure here degrades to Claude Code's own display rather than a blank
    panel — which is why the whole body is defensive.
    """
    try:
        raw = _read_stdin_text(1_000_000) if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        return
    if not raw or not raw.strip():
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(data, dict):
        return

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return
    columns = data.get("columns")

    try:
        config = load_config()
    except Exception:
        config = {}

    out = []
    for task in tasks[:64]:  # a sane ceiling; the panel never shows more
        if not isinstance(task, dict):
            continue
        task_id = task.get("id")
        if not task_id:
            continue
        try:
            content = _render_subagent_row(task, config, columns)
        except Exception:
            continue  # leave this row's default rendering alone
        if not content:
            continue
        out.append(json.dumps({"id": str(task_id), "content": content}))
    if out:
        sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def _win_portable_path(path_str):
    """Return a Windows absolute path spelled with forward slashes.

    Claude Code v2.1.47+ invokes statusLine commands via Git Bash, which
    treats an unquoted backslash as an escape and drops it from the path.
    Forward slashes survive every shell. See:
    https://github.com/anthropics/claude-code/issues/27057

    The path stays literal. It used to be rewritten to ``$HOME/...``, but
    ``$HOME`` is whatever the *shell* says: Git Bash reads the HOME variable
    (a corporate home drive, or one set for Emacs or ssh), and Python's
    ``Path.home()`` ignores it, so the command pointed at a folder the
    installer never wrote to. PowerShell needs ``$env:HOME`` spelled
    differently anyway.
    """
    if sys.platform != "win32":
        return path_str
    path_str = str(path_str).replace("\\", "/")
    # Extended-length prefix (``\\?\C:\x`` / ``\\?\UNC\srv\share``) that
    # Path.resolve() can return: no shell understands it.
    if path_str.startswith("//?/UNC/"):
        path_str = "//" + path_str[len("//?/UNC/"):]
    elif path_str.startswith("//?/") and len(path_str) > 5 and path_str[5] == ":":
        path_str = path_str[len("//?/"):]
    return path_str


def _windows_py_launcher():
    """``py -3`` when the Python launcher is installed and really runs Python 3.8+.

    The launcher is the one Python entry point that survives upgrades: a
    python.org install lives in a versioned folder (``Python313``), and the
    Python install manager in ``pythoncore-3.13-64``, so a command that names
    one of those blanks the status bar the day the old version is removed.
    ``py`` also exists as a Microsoft Store alias that does nothing, so it is
    only used once it has actually run Python.
    """
    if sys.platform != "win32":
        return None
    launcher = shutil.which("py")
    if not launcher:
        return None
    try:
        result = subprocess.run(
            [launcher, "-3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return "py -3" if result.returncode == 0 else None


def _windows_short_path(path):
    """The 8.3 short form of an existing Windows path (``C:/PROGRA~1/...``), or None.

    A short path has no space, so it needs no quotes and runs unchanged in
    Git Bash and PowerShell. Volumes with 8.3 names switched off hand the long
    name back, and the caller then falls back to quoting.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
    except (AttributeError, ImportError, OSError, ValueError):
        return None
    if not length or length >= len(buf):
        return None
    return buf.value.replace("\\", "/")


def _git_bash_available(settings=None):
    """True when Claude Code can run shell commands through Git Bash on Windows.

    Claude Code runs a status line or hook command through Git Bash when it
    finds it and through PowerShell when it does not, and the ``statusLine``
    setting has no field to choose. It looks at ``CLAUDE_CODE_GIT_BASH_PATH``
    (environment or the ``env`` block of settings.json), then the usual Git
    for Windows locations. WSL's ``System32\\bash.exe`` is not Git Bash.
    """
    candidates = []
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if not configured and isinstance(settings, dict) and isinstance(settings.get("env"), dict):
        configured = settings["env"].get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured and isinstance(configured, str):
        candidates.append(Path(configured))
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Git" / "bin" / "bash.exe")
    git = shutil.which("git")
    if git:
        try:
            git_dir = Path(git).resolve().parent
        except (OSError, RuntimeError):
            git_dir = Path(git).parent
        # Git\cmd\git.exe, Git\bin\git.exe and Git\mingw64\bin\git.exe
        candidates.append(git_dir.parent / "bin" / "bash.exe")
        candidates.append(git_dir.parent.parent / "bin" / "bash.exe")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return True
        except OSError:
            continue
    return False


def _get_python_cmd():
    """Return the interpreter part of the command written to settings.json.

    On Windows the result is, in order of preference: ``py -3`` (the launcher
    survives Python upgrades and venv removal), the interpreter's own
    forward-slash path (a venv is swapped for the Python underneath it), its
    8.3 short path when the long one has a space, and as a last resort the
    quoted path. Everything but the quoted form is one plain word that Git Bash
    and both PowerShells read the same way; ``_pulse_command`` deals with the
    quoted one. Elsewhere it is ``sys.executable``, quoted if it has a space.
    """
    if sys.platform == "win32":
        launcher = _windows_py_launcher()
        if launcher:
            return launcher
        exe = sys.executable
        if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
            exe = getattr(sys, "_base_executable", None) or exe
        portable = _win_portable_path(exe)
        if _SHELL_NEUTRAL_WORD.fullmatch(portable):
            return portable
        short = _windows_short_path(exe)
        if short and _SHELL_NEUTRAL_WORD.fullmatch(short):
            return short
        return f'"{portable}"'
    exe = sys.executable
    if " " in exe:
        return f'"{exe}"'
    return exe


def _pulse_command(python_cmd, script_path, flag=None, settings=None):
    """Build the full status-line or hook command from ``_get_python_cmd()``.

    Claude Code runs it through Git Bash, or through PowerShell when Git Bash
    is absent, so on Windows the text must parse in both: a plain interpreter
    word first, then the script path in single quotes. A quoted interpreter
    cannot do that. Bash reads ``& "..."`` as "run in the background" and
    PowerShell reads a line that starts with a quoted string as text to print,
    so that one case gets the form of whichever shell will really run it.
    """
    tail = f" {flag}" if flag else ""
    if sys.platform != "win32":
        return f'{python_cmd} "{script_path}"{tail}'
    script = str(script_path)
    quoted_script = f'"{script}"' if "'" in script else f"'{script}'"
    return f"{_shell_python(python_cmd, settings)} {quoted_script}{tail}"


def _shell_python(python_cmd, settings=None):
    """``python_cmd`` as the shell that will run it needs it written."""
    if sys.platform == "win32" and python_cmd.startswith('"') and not _git_bash_available(settings):
        return "& " + python_cmd    # PowerShell's call operator
    return python_cmd


# Refresh cadences (seconds) for `statusLine.refreshInterval`, chosen by what
# is actually on screen. Claude Code enforces a minimum of 1.
REFRESH_ANIMATED = 2      # animations repaint every tick
REFRESH_TIMED = 15        # focus countdown / heartbeat elapsed time
REFRESH_STATIC = None     # nothing time-based — stay purely event-driven


def _desired_refresh_interval(config):
    """Return the `refreshInterval` this config needs, or None for event-driven.

    Claude Code redraws the status line on its own events (prompt submit, tool
    use, and so on), which is enough for anything derived from stdin. It is not
    enough for content that changes with the wall clock: animation frames, the
    focus-timer countdown, and the heartbeat's elapsed time all freeze while
    the session sits idle. `refreshInterval` re-runs the command on a timer to
    cover exactly those cases.

    We only ask for a timer when something needs it, so a static bar costs
    nothing extra.
    """
    if not isinstance(config, dict):
        return REFRESH_STATIC
    if config.get("animate", "off") != "off":
        return REFRESH_ANIMATED
    show = config.get("show", {})
    if not isinstance(show, dict):
        show = {}
    # `heartbeat` and `pomodoro` are enabled by default but render nothing
    # unless a hook is currently firing or a focus timer is actually running.
    # Asking for a timer on the strength of the *setting* alone meant a stock
    # config relaunched Python every 15s — 240 times an hour — to redraw a bar
    # that never changed. Only ask once one of them is genuinely on screen.
    try:
        if show.get("heartbeat", False) and _is_hook_state_fresh(_read_hook_state()):
            return REFRESH_TIMED
    except Exception:
        pass
    try:
        if show.get("pomodoro", True):
            pomo = _read_pomodoro()
            if pomo and pomo.get("active"):
                return REFRESH_TIMED
    except Exception:
        pass
    return REFRESH_STATIC


def sync_status_line_refresh(config=None):
    """Re-point `statusLine.refreshInterval` at what the current config needs.

    Called after any setting that changes the answer (animation mode, focus
    timer, widget visibility). Rewrites only that one key, leaving the rest of
    settings.json untouched, and is a no-op when usage-pulse is not the
    installed status line — we must never edit another tool's block.

    Returns the interval that was written (None when the key was removed), or
    False when nothing was done.
    """
    settings_path = _claude_config_dir() / "settings.json"
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if not isinstance(settings, dict):
        return False  # valid JSON, wrong shape — leave it alone
    status_line = settings.get("statusLine")
    if not isinstance(status_line, dict):
        return False
    # Only touch our own status line.
    if "claude_status.py" not in str(status_line.get("command", "")):
        return False

    if config is None:
        config = load_config()
    interval = _desired_refresh_interval(config)

    current = status_line.get("refreshInterval")
    had_legacy = "refresh" in status_line
    if current == interval and not had_legacy:
        return interval  # already correct

    status_line.pop("refresh", None)  # never a real Claude Code setting
    if interval is None:
        status_line.pop("refreshInterval", None)
    else:
        status_line["refreshInterval"] = interval
    try:
        _backup_settings(settings_path)
        _atomic_json_write(settings_path, settings)
    except OSError:
        return False
    return interval


def install_status_line():
    settings_path = _claude_config_dir() / "settings.json"
    script_path = _win_portable_path(Path(__file__).resolve())
    python_cmd = _get_python_cmd()

    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except json.JSONDecodeError:
            print(f"Error: {settings_path} contains invalid JSON. Fix or remove it before installing.", file=sys.stderr)
            return
        except OSError as e:
            print(f"Error: Cannot read {settings_path}: {e}", file=sys.stderr)
            return

    # Status line command: forward-slash literal paths on Windows, written so
    # that both Git Bash and PowerShell read it the same way.
    status_command = _pulse_command(python_cmd, script_path, settings=settings)
    existing = settings.get("statusLine")
    if isinstance(existing, dict) and "claude_status.py" in str(existing.get("command", "")):
        # Ours already: update what we own and keep the user's other keys
        # (such as `padding`) through every reinstall and plugin re-point.
        status_line = existing
        status_line["type"] = "command"
        status_line["command"] = status_command
    else:
        # Absent, or another tool's: ours replaces it whole, as it always did.
        status_line = {
            "type": "command",
            "command": status_command,
        }
    # Versions up to v3.1.0 wrote `"refresh": 150` here. Claude Code has no
    # such setting, so it was silently ignored and the timed repaint never
    # happened. The real key is `refreshInterval`, in seconds.
    status_line.pop("refresh", None)
    interval = _desired_refresh_interval(load_config())
    if interval is not None:
        status_line["refreshInterval"] = interval
    else:
        status_line.pop("refreshInterval", None)
    settings["statusLine"] = status_line

    # Render the agent-panel rows too. Claude Code falls back to its own row
    # rendering for anything we don't emit, so this can only add information.
    settings["subagentStatusLine"] = {
        "type": "command",
        "command": _pulse_command(python_cmd, script_path, "--subagent-status-line", settings),
    }

    # No hooks installed here — static status bar by default.
    # Use --animate on for always-on animation (installs hooks automatically)
    # Use --install-hooks for animate-while-working mode

    _secure_mkdir(settings_path.parent)
    backup = _backup_settings(settings_path)
    _atomic_json_write(settings_path, settings)

    utf8_print(f"Installed status line to {settings_path}")
    if backup:
        utf8_print(f"Previous settings.json kept at {backup}")
    utf8_print("Installed subagent status line (per-agent rows in the agent panel)")
    utf8_print(f"Command: {status_command}")
    if status_command.startswith("& "):
        utf8_print("Note: Git for Windows (Git Bash) was not found, so the command is written for PowerShell.")
        utf8_print("      If you install Git for Windows later, run --setup again to rewrite it.")
    if interval is not None:
        utf8_print(f"Refresh: every {interval}s (plus Claude Code's own events)")
    else:
        utf8_print("Refresh: event-driven (no timer needed for a static bar)")
    utf8_print("Restart Claude Code to see the status line.")
    utf8_print("Tip: use --animate on for always-on rainbow animation.")


def install_pulse_command():
    """Copy the /pulse slash command into the user's Claude commands dir.

    The shell installers already do this, but running ``--install`` directly
    used to set up the status line while leaving ``/pulse`` uninstalled -- the
    meter worked yet the command silently did nothing.  Making --install
    self-sufficient closes that gap no matter how the user installed.
    """
    here = Path(__file__).resolve().parent
    # commands/pulse.md is the source; a raw download from before 3.5.0 kept
    # a copy next to the script.
    source = next((p for p in (here / "commands" / "pulse.md", here / "pulse.md") if p.exists()), None)
    if source is None:
        utf8_print("Note: commands/pulse.md not found next to claude_status.py; skipped installing the /pulse command.")
        utf8_print("      Re-run the full installer (install.sh / install.ps1) to add it.")
        return

    commands_dir = _claude_config_dir() / "commands"
    dest = commands_dir / "pulse.md"
    try:
        _secure_mkdir(commands_dir)
        shutil.copyfile(source, dest)
    except OSError as e:
        utf8_print(f"Note: could not install the /pulse command: {e}")
        return

    utf8_print(f"Installed /pulse command to {dest}")


def _without_pulse_entries(entries):
    """A settings hook-event list minus every entry that runs claude_status.py.

    Covers today's hooks and the flat ``{"command": ...}`` shape of old
    versions. Other tools' entries, and their commands inside a shared entry,
    stay.
    """
    kept = []
    for entry in entries:
        if isinstance(entry, dict) and "claude_status.py" in str(entry.get("command", "")):
            continue
        entry = _without_pulse_command(entry, "")   # "" is in every command: any of ours
        if entry is not None:
            kept.append(entry)
    return kept


def cmd_uninstall():
    """``--uninstall``: take usage-pulse out of Claude Code's configuration.

    Removes, from the active config dir (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``),
    our ``statusLine`` and ``subagentStatusLine``, every hook that runs this
    script, and the ``/pulse`` command. Another tool's status line, hooks and
    command are left alone. A copy of settings.json is kept first. Without
    this, deleting the install folder leaves hooks that fail after every tool
    call. The install folder itself, and usage-pulse's own config and cache,
    are not deleted. Returns a process exit code.
    """
    config_dir = _claude_config_dir()
    settings_path = config_dir / "settings.json"
    settings, error = _load_settings_for_write(settings_path)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    removed = []
    for key, label in (("statusLine", "status line"),
                       ("subagentStatusLine", "agent-panel rows")):
        block = settings.get(key)
        if isinstance(block, dict) and "claude_status.py" in str(block.get("command", "")):
            del settings[key]
            removed.append(label)
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        hook_events = []
        for event, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            kept = _without_pulse_entries(entries)
            if len(kept) == len(entries) and kept == entries:
                continue
            hook_events.append(event)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if hook_events:
            if not hooks:
                del settings["hooks"]
            removed.append("hooks (" + ", ".join(sorted(hook_events)) + ")")

    if removed:
        try:
            _backup_settings(settings_path)
            _atomic_json_write(settings_path, settings)
        except OSError as e:
            print(f"Error: could not write {settings_path}: {e}", file=sys.stderr)
            return 1

    command_file = config_dir / "commands" / "pulse.md"
    try:
        if command_file.is_file() and "claude_status.py" in command_file.read_text(encoding="utf-8", errors="replace"):
            command_file.unlink()
            removed.append("/pulse command")
    except OSError as e:
        utf8_print(f"Note: could not remove {command_file}: {e}")

    if not removed:
        utf8_print(f"Nothing to remove: usage-pulse is not installed in {config_dir}.")
        return 0
    utf8_print(f"Removed from {config_dir}: " + "; ".join(removed))
    utf8_print("usage-pulse's own settings and cache folders (\"claude-status\") were left in place.")
    here = Path(__file__).resolve()
    if _is_plugin_cache_path(here):
        utf8_print("This copy is a plugin install: run /plugin uninstall usage-pulse to remove the files.")
    else:
        utf8_print(f"The files are still in {here.parent}; delete that folder to finish.")
    utf8_print("Restart Claude Code for this to take effect.")
    return 0


def _pulse_script_in_command(command):
    """The claude_status.py path a settings.json command runs, or None."""
    m = re.search(r'"([^"]*claude_status\.py)"|\'([^\']*claude_status\.py)\'|(\S*claude_status\.py)',
                  str(command or ""))
    if not m:
        return None
    return next(g for g in m.groups() if g)


def _is_plugin_cache_path(path):
    """True for ``<cache>/<marketplace>/usage-pulse/<version>/claude_status.py``.

    Marketplace installs live in a per-version folder named after the plugin
    (``claude-pulse`` before 3.5.0); a git clone (``~/.usage-pulse/claude_status.py``)
    or a checkout of the repo (``.../usage-pulse/claude_status.py``) has no
    version folder.
    """
    # Split on both separators: Path() only knows the host's own, so on
    # Linux or macOS a Windows path (C:\Users\...\claude_status.py) would be one part.
    parts = [p.lower() for p in re.split(r"[\\/]+", str(path)) if p]
    return (len(parts) >= 3 and parts[-1] == "claude_status.py"
            and parts[-3] in ("usage-pulse", "claude-pulse"))


def cmd_sync_install_path():
    """``--sync-install-path``: re-point settings.json at this copy.

    Marketplace plugins live in a per-version folder that Claude Code removes
    a fortnight after an update, so a status line registered from the old
    folder dies with it (issue #54). The plugin's SessionStart hook runs this
    at every session start: when settings.json runs usage-pulse from another
    plugin folder, the status line, the agent-panel rows, whichever hook
    groups were installed, and /pulse are re-registered from here. A git or
    manual install the user chose on purpose is never touched, and nothing is
    printed when there is nothing to do. Returns a process exit code.
    """
    here = Path(__file__).resolve()
    settings, err = _load_settings_for_write(_claude_config_dir() / "settings.json")
    if err or not isinstance(settings, dict):
        return 0   # a broken settings.json is --setup's job to report, not a session hook's
    status_line = settings.get("statusLine")
    command = status_line.get("command", "") if isinstance(status_line, dict) else ""
    old = _pulse_script_in_command(command)
    if not old:
        return 0
    old_path = Path(os.path.expandvars(str(old).replace("$HOME", str(Path.home()))))
    try:
        if old_path.resolve() == here:
            return 0
    except OSError:
        pass
    if not (_is_plugin_cache_path(old_path) and _is_plugin_cache_path(here)):
        return 0
    groups = _installed_pulse_hooks(settings)
    utf8_print(f"usage-pulse: settings.json still pointed at an old plugin folder; re-pointing it at {here.parent}")
    install_status_line()
    install_pulse_command()
    if groups:
        install_hooks(groups=sorted(groups))
    return 0


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def utf8_print(text):
    """Print text with UTF-8 encoding (avoids Windows cp1252 errors)."""
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def cmd_list_themes():
    """Print all available themes with a colour preview."""
    utf8_print(f"\n{BOLD}Available themes:{RESET}\n")
    for name, colours in THEMES.items():
        if name == "rainbow":
            # Show a mini rainbow preview
            preview = rainbow_colorize(FILL * 8)
            utf8_print(f"  {name:<10} {preview}  (animated rainbow shimmer)")
        else:
            low_bar = f"{colours['low']}{FILL * 3}{RESET}"
            mid_bar = f"{colours['mid']}{FILL * 3}{RESET}"
            high_bar = f"{colours['high']}{FILL * 2}{RESET}"
            preview = f"{low_bar}{mid_bar}{high_bar}"
            utf8_print(f"  {name:<10} {preview}  ({colours['low']}low{RESET} {colours['mid']}mid{RESET} {colours['high']}high{RESET})")
    utf8_print("")


def cmd_themes_demo():
    """Print a simulated status line for each theme so users can see them in action."""
    utf8_print(f"\n{BOLD}Theme previews:{RESET}\n")
    demo_usage = {
        "five_hour": {"utilization": 42, "resets_at": None},
        "seven_day": {"utilization": 67, "resets_at": None},
    }
    user_config = load_config()
    current = user_config.get("theme", "default")
    user_bar_size = user_config.get("bar_size", DEFAULT_BAR_SIZE)
    user_bar_style = user_config.get("bar_style", DEFAULT_BAR_STYLE)
    for name in THEMES:
        demo_tc = THEME_DEMO_TEXT.get(name, "white")
        demo_config = {"theme": name, "bar_size": user_bar_size, "bar_style": user_bar_style, "text_color": demo_tc, "show": {"session": True, "weekly": True, "plan": True, "timer": False, "extra": False, "sparkline": False, "runway": False, "status_message": False, "streak": False, "model": False, "context": False}}
        line = build_status_line(demo_usage, "Max 20x", demo_config)
        marker = " <<" if name == current else ""
        utf8_print(f"  {BOLD}{name:<10}{RESET} {line}{marker}")
    utf8_print(f"\n  Set with: python claude_status.py --theme <name>\n")


def cmd_show_themes():
    """Show all themes with live status line previews using accent colours."""
    current_config = load_config()
    current_theme = current_config.get("theme", "default")
    user_bar_size = current_config.get("bar_size", DEFAULT_BAR_SIZE)

    utf8_print(f"\n{BOLD}Themes:{RESET}\n")
    demo_usage = {
        "five_hour": {"utilization": 42, "resets_at": None},
        "seven_day": {"utilization": 67, "resets_at": None},
    }
    user_bar_style = current_config.get("bar_style", DEFAULT_BAR_STYLE)
    for name in THEMES:
        # Use the accent colour so each theme looks distinct in the preview
        demo_tc = THEME_DEMO_TEXT.get(name, "white")
        demo_config = {"theme": name, "bar_size": user_bar_size, "bar_style": user_bar_style, "text_color": demo_tc, "show": {"session": True, "weekly": True, "plan": True, "timer": False, "extra": False, "sparkline": False, "runway": False, "status_message": False, "streak": False, "model": False, "context": False}}
        line = build_status_line(demo_usage, "Max 20x", demo_config)
        marker = f" {GREEN}<< current{RESET}" if name == current_theme else ""
        # Colour the theme name with its accent colour
        name_colour = TEXT_COLORS.get(demo_tc, "") if name != "rainbow" else ""
        if name == "rainbow":
            coloured_name = rainbow_colorize(f"{name:<10}", shimmer=False)
        else:
            coloured_name = f"{name_colour}{BOLD}{name:<10}{RESET}"
        utf8_print(f"  {coloured_name} {line}{marker}")
    utf8_print("")


def cmd_show_colors():
    """Show all text colours with sample text."""
    current_config = load_config()
    current_theme = current_config.get("theme", "default")
    current_tc = current_config.get("text_color", "auto")

    utf8_print(f"\n{BOLD}Text colours:{RESET}\n")
    sample = "Session 42% | Weekly 67%"
    for tc_name, tc_code in TEXT_COLORS.items():
        # Colour the name label with its own colour
        if tc_name == "none":
            coloured_label = f"{DIM}{tc_name:<14}{RESET}"
            utf8_print(f"  {coloured_label} {DIM}(no colour applied){RESET}")
        elif tc_name == "default":
            coloured_label = f"\033[39m{tc_name:<14}{RESET}"
            utf8_print(f"  {coloured_label} \033[39m{sample}{RESET}")
        elif tc_name == "dim":
            coloured_label = f"{tc_code}{tc_name:<14}{RESET}"
            utf8_print(f"  {coloured_label} {tc_code}{sample}{RESET}")
        else:
            coloured_label = f"{tc_code}{BOLD}{tc_name:<14}{RESET}"
            utf8_print(f"  {coloured_label} {tc_code}{sample}{RESET}")
    if current_tc == "auto":
        resolved = THEME_TEXT_DEFAULTS.get(current_theme, "white")
        utf8_print(f"\n  Current: {BOLD}auto{RESET} (using {resolved} for {current_theme} theme)")
    else:
        utf8_print(f"\n  Current: {BOLD}{current_tc}{RESET}")
    utf8_print("")


def cmd_show_all():
    """Show all themes and text colours with visual previews."""
    cmd_show_themes()
    cmd_show_colors()


def cmd_set_theme(name):
    """Set the active theme and save to config.

    Special case: ``--theme default`` applies the full factory-reset preset
    so that bar size, text colour, animation, etc. all return to defaults —
    not just the colour palette.
    """
    if name not in THEMES:
        utf8_print(f"Unknown theme: {_sanitize(name)}")
        utf8_print(f"Available: {', '.join(THEMES.keys())}")
        return
    # "default" means full factory reset, not just the colour palette
    if name == "default" and "default" in PRESETS:
        cmd_preset("default")
        return
    config = load_config()
    config["theme"] = name
    save_config(config)
    # Clear the cache so the new theme takes effect immediately
    try:
        os.remove(get_cache_path())
    except OSError:
        pass
    if name == "rainbow":
        preview = rainbow_colorize(FILL * 8)
    else:
        colours = THEMES[name]
        preview = f"{colours['low']}{FILL * 3}{colours['mid']}{FILL * 3}{colours['high']}{FILL * 2}{RESET}"
    utf8_print(f"Theme set to {BOLD}{name}{RESET}  {preview}")


REVERSE = "\033[7m"  # reverse video — highlights the selected row in the picker


def _enable_windows_ansi():
    """Enable ANSI/VT processing on the Windows console so the picker renders.

    Windows Terminal enables this by default; legacy conhost does not. Modern
    Windows (10+) supports ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        os.system("")  # last-resort: spawning a shell flips VT on as a side effect


def _read_key():
    """Block for a single keypress and return a normalised token.

    Returns one of: 'up', 'down', 'left', 'right', 'enter', 'esc', 'ctrl-c',
    'backspace', or the literal character pressed (e.g. 'q', 'k', ' '). Arrow
    keys differ by platform: Windows sends a '\\x00'/'\\xe0' prefix then a
    letter; POSIX sends an ESC '[' sequence. Backspace arrives as '\\x08' on
    Windows and as '\\x7f' (DEL) from most POSIX terminals; both map to
    'backspace' so the setup wizard's text fields work everywhere.
    """
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # special-key prefix; the real code follows
            code = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(code, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            return "ctrl-c"
        if ch in ("\x08", "\x7f"):
            return "backspace"
        return ch
    # POSIX
    import termios
    import tty
    import select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    # Read raw bytes straight from the descriptor. sys.stdin is a buffered
    # text stream: sys.stdin.read(1) would slurp a whole "\x1b[B" sequence
    # into Python's buffer, leaving the fd empty so the select() below would
    # wrongly report a lone ESC and arrows would cancel the wizard.
    def _byte():
        return os.read(fd, 1)

    def _ready(timeout):
        return bool(select.select([fd], [], [], timeout)[0])

    try:
        # TCSANOW, not setraw's default TCSAFLUSH: a flush would throw away
        # any key pressed while the previous screen was being drawn.
        tty.setraw(fd, termios.TCSANOW)
        b = _byte()
        if b == b"\x1b":
            # Could be a lone ESC or the start of an arrow sequence — peek briefly.
            if not _ready(0.05):
                return "esc"
            # "ESC [ x" is the usual form; "ESC O x" is sent in
            # application-cursor mode by some terminals.
            if _byte() in (b"[", b"O"):
                code = _byte()
                return {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}.get(code, "")
            return "esc"
        # Multi-byte UTF-8 character (e.g. an accented letter typed into a
        # text field): pull the continuation bytes the lead byte promises.
        if b and b[0] >= 0xC0:
            extra = 3 if b[0] >= 0xF0 else 2 if b[0] >= 0xE0 else 1
            for _ in range(extra):
                if not _ready(0.05):
                    break
                b += _byte()
        ch = b.decode("utf-8", errors="replace")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            return "ctrl-c"
        if ch in ("\x08", "\x7f"):
            return "backspace"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render_picker(names, cursor, current_theme, bar_size, bar_style):
    """Build the full picker screen: one live status-line preview per theme,
    with the cursor row highlighted in reverse video."""
    demo_usage = {
        "five_hour": {"utilization": 42, "resets_at": None},
        "seven_day": {"utilization": 67, "resets_at": None},
    }
    lines = [
        "",
        f"  {BOLD}Pick a theme{RESET}   {DIM}↑/↓ move · Enter keep · Esc cancel{RESET}",
        f"  {DIM}{'─' * 60}{RESET}",
    ]
    for i, name in enumerate(names):
        demo_tc = THEME_DEMO_TEXT.get(name, "white")
        demo_config = {
            "theme": name, "bar_size": bar_size, "bar_style": bar_style,
            "text_color": demo_tc,
            "show": {"session": True, "weekly": True, "plan": True, "timer": False,
                     "extra": False, "sparkline": False, "runway": False,
                     "status_message": False, "streak": False, "model": False, "context": False},
        }
        preview = build_status_line(demo_usage, "Max 20x", demo_config)
        tag = f" {GREEN}(current){RESET}" if name == current_theme else ""
        if i == cursor:
            label = f"{BOLD}▸{RESET} {REVERSE}{BOLD} {name:<8} {RESET}"
        else:
            label = f"  {BOLD} {name:<8} {RESET}"
        lines.append(f"  {label} {preview}{tag}")
    lines.append("")
    return "\n".join(lines)


def _pick_theme_loop(config, step=None):
    """Run the arrow-key theme picker; return the chosen theme name or None.

    Draws into whatever screen the caller has set up: it neither enters nor
    leaves the alternate screen and saves nothing, so ``cmd_pick_theme`` and
    the setup wizard can both use it. With ``step`` (the wizard's step
    header) ← / ``b`` return ``_BACK`` so the wizard can go back a screen.
    """
    current_theme = config.get("theme", "default")
    bar_size = config.get("bar_size", DEFAULT_BAR_SIZE)
    bar_style = config.get("bar_style", DEFAULT_BAR_STYLE)
    names = list(THEMES.keys())
    cursor = names.index(current_theme) if current_theme in names else 0
    while True:
        screen = _render_picker(names, cursor, current_theme, bar_size, bar_style)
        if step is not None:
            screen = (_screen_header(step) + screen
                      + f"  {DIM}← back · Esc cancels setup (nothing is saved){RESET}\n")
        _tty_write("\x1b[H\x1b[J" + screen)
        key = _read_key()
        if key in ("up", "k"):
            cursor = (cursor - 1) % len(names)
        elif key in ("down", "j"):
            cursor = (cursor + 1) % len(names)
        elif key == "enter":
            return names[cursor]
        elif step is not None and key in _BACK_KEYS:
            return _BACK
        elif key in ("esc", "ctrl-c", "q"):
            return None


def cmd_pick_theme():
    """Interactive arrow-key theme picker with a live status-line preview.

    Requires an interactive terminal. When stdin/stdout is not a TTY (e.g. run
    through a tool that captures output) it falls back to the static gallery so
    nothing crashes and the user can still pick with ``--theme <name>``.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        cmd_themes_demo()
        utf8_print(f"  {DIM}The live picker needs an interactive terminal. "
                   f"Pick one above and set it with:  --theme <name>{RESET}")
        return

    _enable_windows_ansi()

    config = load_config()
    chosen = None
    _tty_write("\x1b[?1049h\x1b[?25l")  # enter alternate screen + hide cursor
    try:
        chosen = _pick_theme_loop(config)
    finally:
        _tty_write("\x1b[?25h\x1b[?1049l")  # show cursor + leave alternate screen

    if chosen:
        cmd_set_theme(chosen)  # handles default→factory-reset, cache clear, confirmation
    else:
        utf8_print("Theme unchanged.")


# ---------------------------------------------------------------------------
# Screen toolkit — full-screen prompts for the setup wizard.
#
# Every screen clears and redraws with ESC[H ESC[J (legacy conhost ignores the
# alternate-screen switch, so a redraw must never rely on it) and reads keys
# only through the module-global _read_key, so tests drive any screen by
# patching _read_key with a scripted key sequence. Each returns its value,
# _BACK when the user asked for the previous screen (← or b), or None when
# they cancelled (Esc, q, Ctrl-C).
# ---------------------------------------------------------------------------

_BACK = object()   # "go back one screen"
_SKIP = object()   # wizard step had nothing to ask (e.g. no providers found)
_BACK_KEYS = ("left", "b")
_CANCEL_KEYS = ("esc", "ctrl-c", "q")


def _tty_write(text):
    """Write straight to the terminal as UTF-8 and flush."""
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def _screen_header(step):
    """The dim 'usage-pulse setup · Step 3/13 · Providers' line."""
    if not step:
        return ""
    return f"\n  {DIM}usage-pulse setup · {step}{RESET}\n"


def _screen_frame(step, title, body_lines, hint):
    """Build one full screen: clear, header, title, body, hint."""
    out = ["\x1b[H\x1b[J" + _screen_header(step),
           f"  {BOLD}{title}{RESET}",
           f"  {DIM}{chr(0x2500) * 60}{RESET}"]
    out.extend(body_lines)
    out.append("")
    if hint:
        out.append(f"  {DIM}{hint}{RESET}")
    return "\n".join(out) + "\n"


def _body(lines):
    """Indent free text for a screen body (None/empty → nothing)."""
    return [f"  {line}" for line in (lines or [])]


def _screen_select(step, title, items, index=0, preview=None, hint=None, body=None):
    """Single choice from ``items`` — ``(key, label, description)`` tuples.

    ``preview(key)`` (optional) returns a line drawn above the list for the row
    under the cursor, re-rendered on every move. Returns the chosen index.
    """
    if not items:
        return _BACK
    cursor = max(0, min(int(index or 0), len(items) - 1))
    hint = hint or "↑/↓ move · Enter choose · ← back · Esc cancel"
    width = max(len(str(label)) for _k, label, _d in items)
    while True:
        lines = _body(body)
        if body:
            lines.append("")
        if preview is not None:
            try:
                shown = preview(items[cursor][0])
            except Exception:
                shown = ""
            if shown:
                lines += [f"  {shown}{RESET}", ""]
        for i, (_key, label, desc) in enumerate(items):
            desc_str = f"  {DIM}{desc}{RESET}" if desc else ""
            if i == cursor:
                lines.append(f"  {BOLD}▸{RESET} {REVERSE}{BOLD} {str(label):<{width}} {RESET}{desc_str}")
            else:
                lines.append(f"     {str(label):<{width}} {desc_str}")
        _tty_write(_screen_frame(step, title, lines, hint))
        key = _read_key()
        if key in ("up", "k"):
            cursor = (cursor - 1) % len(items)
        elif key in ("down", "j"):
            cursor = (cursor + 1) % len(items)
        elif key == "enter":
            return cursor
        elif key in _BACK_KEYS:
            return _BACK
        elif key in _CANCEL_KEYS:
            return None
        elif len(key) == 1 and key.isdigit() and 1 <= int(key) <= len(items):
            cursor = int(key) - 1


def _screen_multiselect(step, title, items, checked=None, hint=None, body=None, actions=None):
    """Tick any number of ``items`` — ``(key, label, description)`` tuples.

    Space toggles the row under the cursor, ``a`` ticks all, ``n`` none,
    Enter confirms. ``actions`` maps extra keys to ``fn(index)``; ``items`` is
    re-read after each one, so an action may edit labels in place (the
    providers screen's ``r`` rename does). Returns the set of ticked keys.
    """
    if not items:
        return set()
    ticked = set(checked or ())
    cursor = 0
    actions = actions or {}
    hint = hint or ("↑/↓ move · Space tick · a all · n none · "
                    "Enter confirm · ← back · Esc cancel")
    while True:
        lines = _body(body)
        if body:
            lines.append("")
        width = max(len(str(label)) for _k, label, _d in items)
        for i, (key, label, desc) in enumerate(items):
            box = f"{GREEN}[x]{RESET}" if key in ticked else "[ ]"
            desc_str = f"  {DIM}{desc}{RESET}" if desc else ""
            row = f"{box} {str(label):<{width}}"
            if i == cursor:
                lines.append(f"  {BOLD}▸{RESET} {REVERSE}{row}{RESET}{desc_str}")
            else:
                lines.append(f"    {row}{desc_str}")
        _tty_write(_screen_frame(step, title, lines, hint))
        key = _read_key()
        if key in ("up", "k"):
            cursor = (cursor - 1) % len(items)
        elif key in ("down", "j"):
            cursor = (cursor + 1) % len(items)
        elif key == " ":
            k = items[cursor][0]
            ticked.symmetric_difference_update({k})
        elif key == "a":
            ticked = {k for k, _l, _d in items}
        elif key == "n":
            ticked = set()
        elif key == "enter":
            return {k for k, _l, _d in items if k in ticked}
        elif key in _BACK_KEYS:
            return _BACK
        elif key in _CANCEL_KEYS:
            return None
        elif key in actions:
            actions[key](cursor)


def _screen_text(step, title, default="", validate=None, body=None, hint=None, max_len=64):
    """Free-text entry. ``validate(text)`` returns an error message or None.

    Letters (including q and b) are typed, so only ← goes back and Esc /
    Ctrl-C cancel. Backspace deletes. Returns the entered text.
    """
    buf = list(str(default or ""))[:max_len]
    error = ""
    hint = hint or "type · Backspace delete · Enter accept · ← back · Esc cancel"
    while True:
        lines = _body(body)
        if body:
            lines.append("")
        lines.append(f"  > {''.join(buf)}{REVERSE} {RESET}")
        if error:
            lines += ["", f"  {RED}{error}{RESET}"]
        _tty_write(_screen_frame(step, title, lines, hint))
        key = _read_key()
        if key == "enter":
            text = "".join(buf).strip()
            problem = validate(text) if validate else None
            if problem:
                error = problem
                continue
            return text
        if key == "backspace":
            if buf:
                buf.pop()
            error = ""
        elif key == "left":
            return _BACK
        elif key in ("esc", "ctrl-c"):
            return None
        elif len(key) == 1 and key.isprintable() and len(buf) < max_len:
            buf.append(key)
            error = ""


def _screen_confirm(step, title, default=True, body=None, yes="Yes", no="No", hint=None):
    """Yes/No question. ``y``/``n`` answer at once; Enter takes the highlighted
    answer, which starts on ``default``. Returns True or False."""
    choice = bool(default)
    hint = hint or "y / n · ↑/↓ move · Enter choose · ← back · Esc cancel"
    while True:
        lines = _body(body)
        if body:
            lines.append("")
        for value, label in ((True, yes), (False, no)):
            if value == choice:
                lines.append(f"  {BOLD}▸{RESET} {REVERSE}{BOLD} {label} {RESET}")
            else:
                lines.append(f"     {label}")
        _tty_write(_screen_frame(step, title, lines, hint))
        key = _read_key()
        if key in ("up", "down", "k", "j", "right"):
            choice = not choice
        elif key == "enter":
            return choice
        elif key == "y":
            return True
        elif key == "n":
            return False
        elif key in _BACK_KEYS:
            return _BACK
        elif key in _CANCEL_KEYS:
            return None


# ---------------------------------------------------------------------------
# Setup wizard — ``--setup``
#
# gather → summary → apply. Every screen edits one in-memory plan; nothing is
# written until the summary screen's Enter, so Esc anywhere leaves the machine
# exactly as it was. The config is saved once, at the end, with the theme set
# directly — cmd_set_theme("default") would run the factory-reset preset and
# wipe every other choice.
# ---------------------------------------------------------------------------

# Widgets offered on the wizard's optional-widgets screen, in display order.
# Keys missing from DEFAULT_SHOW (a widget from a newer or older build) are
# simply not offered.
_SETUP_WIDGETS = (
    ("lines", "Lines changed", "+142 -37 this session"),
    ("cache", "Cache hit rate", "share of input served from the prompt cache"),
    ("cache_warm", "Cache warmth", "prompt cache hit ratio and time until it expires"),
    ("pr", "PR badge", "clickable #123 with its review state"),
    ("repo", "Repo badge", "owner/name of the current repository"),
    ("session_name", "Session name", "the name set with /rename"),
    ("weekly_cost", "7-day cost", "rolling weekly API-equivalent spend"),
    ("cumulative_cost", "All-time cost", "API-equivalent spend across all sessions"),
    ("spend_limit", "Spend limit", "used / limit, when your gateway reports one"),
    ("budget", "Budget", "spend against an amount you set (mirrors --max-budget-usd)"),
    ("thinking", "Thinking state", "whether extended thinking is on"),
    ("burn_rate", "Burn rate", "how fast the 5-hour window is filling"),
    ("git_drift", "Git drift", "commits behind/ahead of upstream"),
    ("files_changed", "Files changed", "count of uncommitted files"),
    ("last_tool", "Last tool", "the last tool Claude used (needs the heartbeat hook)"),
)

_SETUP_HOOK_ITEMS = (
    ("agents", "Live agents", "agents 3 live · 47/200  (SubagentStart/Stop)"),
    ("team", "Agent team", "team 3 · tasks 4/7  (TeammateIdle/TaskCreated/TaskCompleted)"),
    ("heartbeat", "Heartbeat", "spinner + tool count; runs Python after every tool call (PostToolUse)"),
)

# Provider types the unattended path may switch on by itself: they only read
# local files (Codex, Gemini) or another Claude account's own login. MiniMax
# runs a subprocess, so it is only ever enabled by an explicit choice.
_AUTO_PROVIDER_TYPES = ("claude", "codex", "gemini")


def _providers_api():
    """Return ``(detect_providers, set_providers)``, either None when missing.

    The providers feature is optional for the wizard: when this build lacks it
    the providers screen is skipped with a note instead of failing.
    """
    detect = globals().get("detect_providers")
    setp = globals().get("set_providers")
    return (detect if callable(detect) else None, setp if callable(setp) else None)


def _short_home(path):
    """Show a path with the home directory as ``~``."""
    text = str(path or "")
    try:
        home = str(Path.home())
    except Exception:
        return text
    if home and text.lower().startswith(home.lower()):
        return "~" + text[len(home):]
    return text


def _stdio_is_tty():
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _setup_env():
    """Inspect this machine for the setup report. Reads only, writes nothing."""
    cfg_dir = _claude_config_dir()
    settings_path = cfg_dir / "settings.json"
    settings, error = _load_settings_for_write(settings_path)
    env = {
        "python": sys.version_info[:3],
        "python_ok": sys.version_info >= (3, 8),
        "claude": _CLAUDE_PATH,
        "config_dir": cfg_dir,
        "config_dir_from_env": bool(os.environ.get("CLAUDE_CONFIG_DIR", "").strip()),
        "settings_path": settings_path,
        "settings_exists": settings_path.exists(),
        "settings_error": error,
        "status_line": "none",
        "status_line_cmd": "",
        "subagent_line": "none",
        "hooks": set(),
        "pulse_command": (cfg_dir / "commands" / "pulse.md").exists(),
        "providers_available": all(_providers_api()),
    }
    if settings is not None:
        sl = settings.get("statusLine")
        cmd = str(sl.get("command", "")) if isinstance(sl, dict) else ""
        env["status_line_cmd"] = cmd
        if "claude_status.py" in cmd:
            env["status_line"] = "ours"
        elif cmd.strip():
            env["status_line"] = "other"
        sub = settings.get("subagentStatusLine")
        sub_cmd = str(sub.get("command", "")) if isinstance(sub, dict) else ""
        if "claude_status.py" in sub_cmd:
            env["subagent_line"] = "ours"
        elif sub_cmd.strip():
            env["subagent_line"] = "other"
        env["hooks"] = _installed_pulse_hooks(settings)
    here = Path(__file__).resolve()
    if "plugins" in [p.lower() for p in here.parts]:
        env["install_kind"] = "plugin"
    elif (here.parent / ".git").exists():
        env["install_kind"] = "git"
    else:
        env["install_kind"] = "raw"
    env["script_dir"] = here.parent
    return env


def _setup_env_lines(env):
    """The environment report as plain text lines (shared by both modes)."""
    py = ".".join(str(n) for n in env["python"])
    lines = [
        f"Python        {py}" + ("" if env["python_ok"] else "  (usage-pulse needs 3.8 or newer)"),
        "Claude Code   " + (_short_home(env["claude"]) if env["claude"]
                            else "not found on PATH (fine if you start it another way)"),
        f"Config dir    {_short_home(env['config_dir'])}"
        + ("  (from CLAUDE_CONFIG_DIR)" if env["config_dir_from_env"] else ""),
    ]
    if env["settings_error"]:
        lines.append(f"settings.json PROBLEM: {env['settings_error']}")
    elif not env["settings_exists"]:
        lines.append("settings.json not there yet (setup creates it)")
    else:
        lines.append("settings.json ok")
    kind = env["status_line"]
    if kind == "ours":
        lines.append("Status line   usage-pulse (installed)")
    elif kind == "other":
        lines.append(f"Status line   another tool: {_sanitize(env['status_line_cmd'])[:70]}")
    else:
        lines.append("Status line   not set")
    lines.append("Agent panel   " + {"ours": "usage-pulse rows", "other": "another tool's rows",
                                     "none": "Claude Code's default rows"}[env["subagent_line"]])
    hooks = ", ".join(g for g in HOOK_GROUP_NAMES if g in env["hooks"]) or "none"
    lines.append(f"Hooks         {hooks}")
    lines.append("/pulse        " + ("installed" if env["pulse_command"] else "not installed"))
    lines.append("This copy     " + {
        "git": f"git clone at {_short_home(env['script_dir'])} (update with --update)",
        "plugin": "Claude Code plugin (update it with /plugin)",
        "raw": f"plain download at {_short_home(env['script_dir'])} (re-run the installer to update)",
    }[env["install_kind"]])
    return lines


def _setup_detect_providers():
    """Run detect_providers() defensively: returns (rows, note)."""
    detect, setp = _providers_api()
    if detect is None or setp is None:
        return [], "not available in this build"
    try:
        rows = detect() or []
    except Exception as e:
        return [], f"detection failed ({type(e).__name__})"
    rows = [r for r in rows if isinstance(r, dict) and r.get("id")]
    return rows, ("" if rows else "none detected")


def _configured_provider_ids(config):
    entries = config.get("providers")
    if not isinstance(entries, list):
        return set()
    return {str(e.get("id")) for e in entries if isinstance(e, dict) and e.get("id")}


def _enabled_provider_ids(config):
    """Ids of providers that are configured *and* switched on."""
    entries = config.get("providers")
    if not isinstance(entries, list):
        return set()
    return {str(e.get("id")) for e in entries
            if isinstance(e, dict) and e.get("id") and e.get("enabled")}


def _auto_provider_ids(rows, config):
    """Providers the defaults switch on: detected, not the active account, a
    file/login reader (never MiniMax), never configured before (a provider
    the user disabled stays disabled when setup runs again), and recommended
    by detection: for an unconfigured row ``enabled`` is the recommendation
    (a Gemini file older than a week, for instance, comes back False)."""
    known = _configured_provider_ids(config)
    return {str(r["id"]) for r in rows
            if not r.get("active") and r.get("enabled")
            and r.get("type") in _AUTO_PROVIDER_TYPES
            and str(r["id"]) not in known}


def _setup_preview_line(config):
    """A demo status line drawn with ``config``'s look (theme, size, style,
    layout, effort format) and a fixed set of widgets, for the wizard's live
    previews. Never touches the network or git."""
    now = datetime.now(timezone.utc)
    usage = {
        "five_hour": {"utilization": 27, "resets_at": (now + timedelta(hours=2, minutes=53)).isoformat()},
        "seven_day": {"utilization": 73, "resets_at": (now + timedelta(days=3, hours=4)).isoformat()},
    }
    show = {k: k.endswith("_bar") for k in DEFAULT_SHOW}   # the bars themselves stay on
    for k in ("session", "weekly", "timer", "weekly_timer", "context", "model", "effort", "lines"):
        show[k] = True
    demo = dict(config)
    demo["show"] = show
    demo["animate"] = "off"
    demo.pop("widget_priority", None)
    stdin = {"model_name": "Opus 5.5", "context_pct": 35, "effort": "xhigh",
             "lines_added": 142, "lines_removed": 37}
    return build_status_line(usage, "Max 20x", demo, stdin, cache_age=0)


def _new_setup_plan(config, env):
    return {
        "config": config,
        "install": True,
        "hooks": set(),
        "hooks_remove": set(),
        "providers": None,          # {"enable": set, "disable": set, "labels": {}}
        "provider_note": "",
        "replaced_status_line": env["status_line_cmd"] if env["status_line"] == "other" else None,
    }


def _fmt_value(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    if value in ("", None):
        return "(none)"
    return str(value)


def _setup_changes(original, plan, env):
    """Human-readable list of what applying ``plan`` will do."""
    config = plan["config"]
    out = []
    if plan["install"]:
        if env["status_line"] == "ours":
            out.append("Status line: reinstall usage-pulse + agent-panel rows + /pulse")
        elif env["status_line"] == "other":
            out.append(f"Status line: replace {_sanitize(env['status_line_cmd'])[:50]} with usage-pulse")
        else:
            out.append("Status line: install usage-pulse + agent-panel rows + /pulse")
    else:
        out.append("Status line: leave as it is")
    add = [g for g in HOOK_GROUP_NAMES if g in plan["hooks"]]
    rem = [g for g in HOOK_GROUP_NAMES if g in plan["hooks_remove"]]
    hook_bits = []
    if add:
        hook_bits.append("install " + ", ".join(add))
    if rem:
        hook_bits.append("remove " + ", ".join(rem))
    out.append("Hooks: " + ("; ".join(hook_bits) if hook_bits else "none"))
    prov = plan.get("providers")
    if prov and (prov["enable"] or prov["disable"] or prov["labels"]):
        bits = []
        if prov["enable"]:
            bits.append("show " + ", ".join(sorted(prov["enable"])))
        if prov["disable"]:
            bits.append("hide " + ", ".join(sorted(prov["disable"])))
        for pid, label in sorted(prov["labels"].items()):
            bits.append(f"rename {pid} to {label}")
        out.append("Providers: " + "; ".join(bits))
    elif plan.get("provider_note"):
        out.append(f"Providers: {plan['provider_note']}")
    else:
        out.append("Providers: no change")
    for key in sorted(set(original) | set(config)):
        if key in ("show", "providers") or key.startswith("_"):
            continue
        old, new = original.get(key), config.get(key)
        if old != new:
            out.append(f"{key.replace('_', ' ').capitalize()}: {_fmt_value(old)} → {_fmt_value(new)}")
    old_show, new_show = original.get("show", {}), config.get("show", {})
    on = sorted(k for k in new_show if new_show.get(k) and not old_show.get(k))
    off = sorted(k for k in new_show if not new_show.get(k) and old_show.get(k))
    if on:
        out.append("Show: " + ", ".join(on))
    if off:
        out.append("Hide: " + ", ".join(off))
    if config.get("show", {}).get("last_tool") and "heartbeat" not in (plan["hooks"] | (env["hooks"] - plan["hooks_remove"])):
        out.append("Note: Last tool stays blank without the heartbeat hook")
    return out


def _setup_apply(plan, env):
    """Write everything ``plan`` asks for. Returns True when all of it worked."""
    config = plan["config"]
    ok = True
    if plan["install"]:
        install_status_line()
        install_pulse_command()
        settings, _err = _load_settings_for_write(env["settings_path"])
        cmd = ((settings or {}).get("statusLine") or {}).get("command", "")
        if "claude_status.py" not in str(cmd):
            ok = False
            utf8_print(f"{RED}The status line was not written to {env['settings_path']}.{RESET}")
    if plan["hooks_remove"]:
        ok = remove_hooks(plan["hooks_remove"]) and ok
    if plan["hooks"]:
        ok = install_hooks(plan["hooks"]) and ok
    prov = plan.get("providers")
    if prov and (prov["enable"] or prov["disable"] or prov["labels"]):
        _detect, setp = _providers_api()
        try:
            result = setp(config, enable_ids=tuple(sorted(prov["enable"])),
                          disable_ids=tuple(sorted(prov["disable"])),
                          labels=dict(prov["labels"]) or None)
            if isinstance(result, dict):
                config = result
            if prov["enable"] and "providers" in DEFAULT_SHOW:
                config.setdefault("show", {})["providers"] = True
        except Exception as e:
            ok = False
            utf8_print(f"{RED}Could not update providers: {type(e).__name__}: {_sanitize(str(e))}{RESET}")
    save_config(config)
    try:
        os.remove(get_cache_path())
    except OSError:
        pass
    return ok


def _setup_interactive(env):
    """Full-screen guided setup. Returns a process exit code."""
    import copy
    original = load_config()
    config = copy.deepcopy(original)
    plan = _new_setup_plan(config, env)
    rows, note = _setup_detect_providers()
    plan["provider_note"] = note
    prov_orig_labels = {str(r["id"]): _sanitize(str(r.get("label") or r["id"]))[:24] for r in rows}
    prov_checked = ({str(r["id"]) for r in rows if r.get("enabled")}
                    | _auto_provider_ids(rows, config))
    prov_labels = {}
    caps_minutes = max(1, int(round(_model_caps_ttl(config) / 60)))
    state = {"error": None}

    def welcome(step):
        lines = _setup_env_lines(env)
        if env["settings_error"]:
            state["error"] = env["settings_error"]
            _tty_write(_screen_frame(step, "Setup can't continue", _body(lines),
                                     "Fix settings.json, then run --setup again. Press any key."))
            _read_key()
            return None
        r = _screen_confirm(step, "Set up usage-pulse", True,
                            body=lines + ["", "Nothing is written until you confirm the summary at the end."],
                            yes="Start", no="Quit", hint="Enter start · Esc quit")
        return None if r is False else r

    def status_line(step):
        kind = env["status_line"]
        if kind == "ours":
            title = "Reinstall the usage-pulse status line?"
            body = ["Refreshes the command in settings.json, the agent-panel rows",
                    "(subagentStatusLine) and the /pulse command. Safe to repeat."]
        elif kind == "other":
            title = "Replace your current status line?"
            body = [f"settings.json runs: {_sanitize(env['status_line_cmd'])[:70]}",
                    "usage-pulse replaces it. The old command is printed when setup",
                    "finishes, so you can put it back."]
        else:
            title = "Install the usage-pulse status line?"
            body = ["Adds statusLine and subagentStatusLine (one row per running agent)",
                    "to settings.json, and the /pulse command for later tweaks."]
        r = _screen_confirm(step, title, plan["install"], body=body)
        if isinstance(r, bool):
            plan["install"] = r
            return True
        return r

    def hooks(step):
        installed = env["hooks"]
        default = set(installed) if installed else {"agents", "team"}
        current = plan.get("_hooks_seen", default)
        r = _screen_multiselect(step, "Which hooks should usage-pulse install?",
                                list(_SETUP_HOOK_ITEMS), current,
                                body=["Hooks are commands Claude Code runs on events. None of these can",
                                      "block Claude: the events that could are pinned to exit 0."])
        if not isinstance(r, set):
            return r
        plan["_hooks_seen"] = r
        plan["hooks"] = set(r)
        plan["hooks_remove"] = set(installed) - r
        show = config["show"]
        base = original["show"].get("heartbeat", False)
        if "heartbeat" in r and "heartbeat" not in installed:
            show["heartbeat"] = True
        elif "heartbeat" in installed and "heartbeat" not in r:
            show["heartbeat"] = False
        else:
            show["heartbeat"] = base
        return True

    def providers(step):
        if not rows:
            return _SKIP
        items = []
        for r in rows:
            pid = str(r["id"])
            label = _sanitize(str(prov_labels.get(pid) or r.get("label") or pid))[:24]
            bits = [str(r.get("type") or "?")]
            if r.get("path"):
                bits.append(_short_home(_sanitize(str(r["path"]))))
            if r.get("active"):
                bits.append("this account (already the main line)")
            if r.get("note"):
                bits.append(_sanitize(str(r["note"])))
            items.append((pid, label, " · ".join(bits)))

        def rename(index):
            pid, label, _desc = items[index]
            new = _screen_text(step, f"Label for {pid}", default=label, max_len=24,
                               validate=lambda t: None if t else "A label can't be empty.",
                               hint="type · Backspace delete · Enter accept · ←/Esc keep the old label")
            if isinstance(new, str) and new:
                new = _sanitize(new)[:24]
                prov_labels[pid] = new
                items[index] = (pid, new, items[index][2])

        current = plan.get("_prov_seen", prov_checked)
        r = _screen_multiselect(step, "Show usage rows for your other accounts and tools?",
                                items, current, actions={"r": rename},
                                body=["Each ticked provider gets its own row under the main line.",
                                      "Codex and Gemini rows read local files. Another Claude account's row",
                                      "asks api.anthropic.com with that account's own token (never refreshed)."],
                                hint="Space tick · r rename · a all · n none · Enter confirm "
                                     "· ← back · Esc cancel")
        if not isinstance(r, set):
            return r
        plan["_prov_seen"] = r
        # What is on today comes from the config: for a row that is not
        # configured yet, detection's "enabled" is only its recommendation.
        was = _enabled_provider_ids(config)
        labels = {pid: lab for pid, lab in prov_labels.items() if lab != prov_orig_labels.get(pid)}
        plan["providers"] = {"enable": r - was, "disable": was - r, "labels": labels}
        return True

    def caps(step):
        items = [(k, label, "") for k, label in (("opus", "Opus"), ("sonnet", "Sonnet"), ("fable", "Fable"))
                 if k in DEFAULT_SHOW]
        if not items:
            return _SKIP
        current = {k for k, _l, _d in items if config["show"].get(k)}
        r = _screen_multiselect(step, "Per-model weekly cap bars?", items, current, body=[
            "Claude Code's status-line input carries only the 5-hour and weekly",
            "windows. Each per-model bar needs one usage-API call every",
            f"{caps_minutes} minutes with your Claude Code OAuth token. Off by default."])
        if not isinstance(r, set):
            return r
        for k, _l, _d in items:
            config["show"][k] = k in r
        return True

    def theme(step):
        r = _pick_theme_loop(config, step=step)
        if isinstance(r, str):
            config["theme"] = r
            return True
        return r

    def _choice(step, title, key, options, preview=True, body=None):
        names = [o[0] for o in options]
        cur = config.get(key)
        idx = names.index(cur) if cur in names else 0
        pv = (lambda k: _setup_preview_line(dict(config, **{key: k}))) if preview else None
        r = _screen_select(step, title, options, idx, preview=pv, body=body)
        if isinstance(r, int):
            config[key] = names[r]
            return True
        return r

    def bar_size(step):
        return _choice(step, "Bar size", "bar_size",
                       [(n, n, f"{w} characters") for n, w in BAR_SIZES.items()]
                       + [(BAR_SIZE_AUTO, BAR_SIZE_AUTO, "large, shrinking to fit a narrow window")])

    def bar_style(step):
        return _choice(step, "Bar style", "bar_style",
                       [(n, n, f"{f}{f}{f}{e}{e}") for n, (f, e) in BAR_STYLES.items()])

    def layout(step):
        desc = {"standard": "labels, bars and timers", "compact": "short labels",
                "minimal": "bars only", "percent-first": "percentage before the bar"}
        return _choice(step, "Layout", "layout", [(n, n, desc.get(n, "")) for n in LAYOUTS])

    def currency(step):
        detected = _detect_default_currency()
        names = {"$": "US dollar (costs are calculated in USD)",
                 "£": "British pound (converted daily)", "€": "Euro (converted daily)"}
        syms = []
        # First run: the repo template's "$" is not a choice the user made, so
        # lead with the detected currency instead.
        first = config.get("currency") if get_config_path().exists() else detected
        for s in (first or "$", detected, "$", "£", "€"):
            if s and s not in syms:
                syms.append(s)
        options = [(s, s, names.get(s, "converted daily") + ("  · detected" if s == detected else ""))
                   for s in syms]
        options.append(("__other__", "Other…", "type any symbol: ¥ ₹ C$ kr"))
        while True:
            r = _screen_select(step, "Currency for cost widgets", options, 0)
            if not isinstance(r, int):
                return r
            if options[r][0] != "__other__":
                config["currency"] = options[r][0]
                return True
            t = _screen_text(step, "Currency symbol (up to 5 characters)", "", max_len=5,
                             validate=lambda v: None if _sanitize(v)[:5] else "Type a symbol, or ← to go back.")
            if isinstance(t, str):
                config["currency"] = _sanitize(t)[:5]
                return True
            if t is None:
                return None
            # _BACK → show the currency list again

    def clock(step):
        return _choice(step, "Clock format for reset times", "clock_format",
                       [("12h", "12h", "Fri 5pm"), ("24h", "24h", "Fri 17:00")], preview=False)

    def effort(step):
        return _choice(step, "How should reasoning effort be written?", "effort_format",
                       [(f, f, _format_effort("high", f)) for f in ("labeled", "full", "short")
                        if f in EFFORT_FORMATS])

    def widgets(step):
        items = [(k, label, desc) for k, label, desc in _SETUP_WIDGETS if k in DEFAULT_SHOW]
        show = config["show"]

        def is_on(k):
            if k == "budget":
                return bool(show.get("budget")) and float(config.get("budget_usd") or 0) > 0
            return bool(show.get(k, DEFAULT_SHOW.get(k, False)))

        while True:
            before = {k for k, _l, _d in items if is_on(k)}
            r = _screen_multiselect(step, "Optional widgets", items, before,
                                    body=["Ticked widgets appear when they have something to show."])
            if not isinstance(r, set):
                return r
            if "budget" in r and "budget" not in before:
                amount = _screen_text(step, "Budget in USD (match your --max-budget-usd)", "",
                                      max_len=10, validate=_validate_budget)
                if amount is None:
                    return None
                if amount is _BACK:
                    continue
                config["budget_usd"] = float(amount.lstrip("$"))
            for k, _l, _d in items:
                on = k in r
                if on != (k in before):
                    show[k] = on
            return True

    def summary(step):
        lines = _setup_changes(original, plan, env)
        r = _screen_confirm(step, "Apply these changes?", True, body=lines,
                            yes="Apply", no="Cancel (change nothing)")
        if r is True:
            return True
        if r is False:
            return None
        return r

    steps = [("Welcome", welcome), ("Status line", status_line), ("Hooks", hooks),
             ("Providers", providers), ("Model caps", caps), ("Theme", theme),
             ("Bar size", bar_size), ("Bar style", bar_style), ("Layout", layout),
             ("Currency", currency), ("Clock", clock), ("Effort", effort),
             ("Widgets", widgets), ("Summary", summary)]

    _enable_windows_ansi()
    _tty_write("\x1b[?1049h\x1b[?25l")
    finished = False
    try:
        i, direction = 0, 1
        while 0 <= i < len(steps):
            name, fn = steps[i]
            result = fn(f"Step {i}/{len(steps) - 1} · {name}")
            if result is None:
                break
            if result is _BACK:
                i, direction = max(0, i - 1), -1
            elif result is _SKIP:
                i += direction
                if i < 0:
                    i, direction = 0, 1
            else:
                i, direction = i + 1, 1
        else:
            finished = True
    except KeyboardInterrupt:
        finished = False
    finally:
        _tty_write("\x1b[?25h\x1b[?1049l")

    if state["error"]:
        utf8_print(f"{RED}Error: {state['error']}{RESET}")
        utf8_print("Nothing changed.")
        return 1
    if not finished:
        utf8_print("Nothing changed.")
        return 0
    plan.pop("_hooks_seen", None)
    plan.pop("_prov_seen", None)
    for line in _setup_changes(original, plan, env):
        utf8_print(f"  {line}")
    utf8_print("")
    ok = _setup_apply(plan, env)
    if plan["install"] and plan["replaced_status_line"]:
        utf8_print(f"Replaced your previous status line command: {_sanitize(plan['replaced_status_line'])}")
    utf8_print("")
    if ok:
        utf8_print(f"{GREEN}Done.{RESET} Restart Claude Code to load it. Tweak anything later with /pulse.")
        return 0
    utf8_print(f"{RED}Setup finished with errors (see above).{RESET}")
    return 1


def _validate_budget(text):
    try:
        value = float(str(text).lstrip("$"))
    except ValueError:
        return "Type an amount such as 25, or ← to go back."
    if not (value > 0) or value != value or value == float("inf"):
        return "The budget must be more than 0."
    return None


def _setup_noninteractive(env):
    """Unattended setup with safe defaults. Returns a process exit code.

    Installs the status line, the agent-panel rows and /pulse, and switches on
    newly detected file/login providers. It installs no hooks and never
    enables MiniMax: an unattended install keeps a static bar.
    """
    utf8_print("usage-pulse setup (defaults)")
    for line in _setup_env_lines(env):
        utf8_print(f"  {line}")
    utf8_print("")
    if env["settings_error"]:
        print(f"Error: {env['settings_error']}", file=sys.stderr)
        utf8_print("Nothing changed.")
        return 1
    config = load_config()
    plan = _new_setup_plan(config, env)
    rows, note = _setup_detect_providers()
    enable = _auto_provider_ids(rows, config)
    if enable:
        plan["providers"] = {"enable": enable, "disable": set(), "labels": {}}
    else:
        plan["provider_note"] = note or "no new ones"
    install_status_line()
    install_pulse_command()
    ok = True
    settings, _err = _load_settings_for_write(env["settings_path"])
    if "claude_status.py" not in str(((settings or {}).get("statusLine") or {}).get("command", "")):
        ok = False
        utf8_print(f"{RED}The status line was not written to {env['settings_path']}.{RESET}")
    if plan["providers"]:
        plan["install"] = False   # already done above; apply only the rest
        ok = _setup_apply(plan, env) and ok
    if plan["replaced_status_line"]:
        utf8_print(f"Replaced your previous status line command: {_sanitize(plan['replaced_status_line'])}")
    utf8_print("")
    prov_txt = (", ".join(sorted(enable)) if enable else (plan["provider_note"] or "none"))
    utf8_print(f"Set up: status line, agent-panel rows, /pulse. Providers: {prov_txt}. Hooks: none.")
    utf8_print("For hooks, theme and widgets run --setup in a terminal, or /pulse setup inside Claude Code.")
    return 0 if ok else 1


def cmd_setup(args):
    """``--setup [--non-interactive]``: guided first-run setup.

    Interactive when stdin and stdout are both terminals and neither
    ``--non-interactive`` nor ``USAGE_PULSE_NONINTERACTIVE`` (or the older
    ``CLAUDE_PULSE_NONINTERACTIVE``) asks otherwise;
    unattended defaults everywhere else (pipes, CI, Git Bash's mintty).
    Returns a process exit code.
    """
    try:
        forced = ("--non-interactive" in args
                  or any(os.environ.get(k, "").strip() not in ("", "0")
                         for k in ("USAGE_PULSE_NONINTERACTIVE", "CLAUDE_PULSE_NONINTERACTIVE")))
        env = _setup_env()
        if forced:
            return _setup_noninteractive(env)
        if not _stdio_is_tty():
            utf8_print("No interactive terminal here, so setup uses the defaults.")
            if sys.platform == "win32" and (os.environ.get("MSYSTEM") or os.environ.get("TERM_PROGRAM") == "mintty"):
                utf8_print("Git Bash (mintty) can't run the full-screen setup: use Windows Terminal or PowerShell,"
                           " or prefix the command with winpty.")
            return _setup_noninteractive(env)
        return _setup_interactive(env)
    except KeyboardInterrupt:
        utf8_print("Setup interrupted.")
        return 130
    except Exception as e:
        print(f"Setup failed: {type(e).__name__}: {_sanitize(str(e))}", file=sys.stderr)
        return 1


def cmd_show(parts_str):
    """Enable the given comma-separated parts."""
    config = load_config()
    parts = [p.strip().lower() for p in parts_str.split(",")]
    valid = set(DEFAULT_SHOW.keys())
    for part in parts:
        if part not in valid:
            utf8_print(f"Unknown part: {_sanitize(part)} (valid: {', '.join(sorted(valid))})")
            return
    for part in parts:
        config["show"][part] = True
        # Clear explicit hide flag so auto-show can work again
        if part == "extra":
            config.pop("extra_hidden", None)
    save_config(config)
    utf8_print(f"Enabled: {', '.join(parts)}")


def cmd_hide(parts_str):
    """Disable the given comma-separated parts."""
    config = load_config()
    parts = [p.strip().lower() for p in parts_str.split(",")]
    valid = set(DEFAULT_SHOW.keys())
    for part in parts:
        if part not in valid:
            utf8_print(f"Unknown part: {_sanitize(part)} (valid: {', '.join(sorted(valid))})")
            return
    for part in parts:
        config["show"][part] = False
        # Mark extra as explicitly hidden so auto-show respects it
        if part == "extra":
            config["extra_hidden"] = True
    save_config(config)
    utf8_print(f"Disabled: {', '.join(parts)}")


def cmd_preset(name):
    """Apply a named preset configuration."""
    if name not in PRESETS:
        utf8_print(f"Unknown preset: {_sanitize(name)}")
        utf8_print(f"\nAvailable presets:")
        for pname, pdata in PRESETS.items():
            utf8_print(f"  {BOLD}{pname:<10}{RESET} {pdata['description']}")
        return
    preset = PRESETS[name]
    config = load_config()
    # Apply config overrides (bar_size, layout, max_width, etc.)
    for key, val in preset["config"].items():
        config[key] = val
    # Apply show/hide overrides — respect user preferences for update notifications
    for key, val in preset["show_overrides"].items():
        config["show"][key] = val
    # Full reset should also clear sticky flags
    if name == "default":
        config.pop("extra_hidden", None)
    save_config(config)
    try:
        os.remove(get_cache_path())
    except OSError:
        pass
    utf8_print(f"Preset {BOLD}{name}{RESET} applied!")
    utf8_print(f"  {preset['description']}")
    # Show a preview
    demo_usage = {
        "five_hour": {"utilization": 42, "resets_at": None},
        "seven_day": {"utilization": 67, "resets_at": None},
    }
    line = build_status_line(demo_usage, "Max 20x", config)
    utf8_print(f"  Preview: {line}")


def cmd_print_config():
    """Print the current configuration summary."""
    config = load_config()
    theme_name = config.get("theme", "default")

    if theme_name == "rainbow":
        preview = rainbow_colorize(FILL * 8)
    else:
        colours = THEMES.get(theme_name, THEMES["default"])
        preview = f"{colours['low']}{FILL * 3}{colours['mid']}{FILL * 3}{colours['high']}{FILL * 2}{RESET}"

    utf8_print(f"\n{BOLD}usage-pulse v{VERSION}{RESET}\n")
    utf8_print(f"  Theme:     {theme_name}  {preview}")
    utf8_print(f"  Cache TTL: {config.get('cache_ttl_seconds', DEFAULT_CACHE_TTL)}s")
    utf8_print(f"  Currency:  {_sanitize(config.get('currency', chr(163)))}")
    bs = config.get("bar_size", DEFAULT_BAR_SIZE)
    bw_display, bw_fit = _bar_width_setting(config)
    if bw_fit:
        utf8_print(f"  Bar size:  {bs} (up to {bw_display} chars, shrinks to fit the window)")
    else:
        utf8_print(f"  Bar size:  {bs} ({bw_display} chars)")
    mw = config.get("max_width", DEFAULT_MAX_WIDTH_PCT)
    utf8_print(f"  Max width: {mw}% of terminal")
    bst = config.get("bar_style", DEFAULT_BAR_STYLE)
    bst_chars = BAR_STYLES.get(bst, BAR_STYLES[DEFAULT_BAR_STYLE])
    if bst in BAR_GRADIENT_STYLES:
        _g = BAR_GRADIENT_STYLES[bst][0]
        utf8_print(f"  Bar style: {bst} ({_g[0]}..{_g[-1]})")
    else:
        utf8_print(f"  Bar style: {bst} ({bst_chars[0]}{bst_chars[1]})")
    ly = config.get("layout", DEFAULT_LAYOUT)
    utf8_print(f"  Layout:    {ly}")
    cf = config.get("context_format", "percent")
    utf8_print(f"  Context:   {cf}")
    ed = config.get("extra_display", "auto")
    utf8_print(f"  Extra display: {ed}")
    ps = config.get("pace_style", DEFAULT_PACE_STYLE)
    utf8_print(f"  Pace style: {ps if ps in PACE_STYLES else DEFAULT_PACE_STYLE}")
    show = config.get("show", DEFAULT_SHOW)
    wt_fmt = _sanitize(str(config.get("weekly_timer_format", DEFAULT_WEEKLY_TIMER_FORMAT)))
    if wt_fmt not in WEEKLY_TIMER_FORMATS:
        wt_fmt = DEFAULT_WEEKLY_TIMER_FORMAT
    wt_pfx = _sanitize(str(config.get("weekly_timer_prefix", DEFAULT_WEEKLY_TIMER_PREFIX)))[:10]
    wt_vis = show.get("weekly_timer", True)
    wt_state = f"{GREEN}on{RESET}" if wt_vis else f"{RED}off{RESET}"
    wt_clk = config.get("clock_format", DEFAULT_CLOCK_FORMAT)
    if wt_clk not in CLOCK_FORMATS:
        wt_clk = DEFAULT_CLOCK_FORMAT
    utf8_print(f"  Weekly timer:  {wt_state}  format={wt_fmt}  prefix=\"{wt_pfx}\"  clock={wt_clk}")
    anim = config.get("animate", False)
    anim_state = f"{GREEN}on{RESET}" if anim else f"{RED}off{RESET}"
    utf8_print(f"  Animation:    {anim_state}  ({'rainbow always moving' if anim else 'static'})")
    tc = config.get("text_color", "auto")
    if tc == "auto":
        resolved = THEME_TEXT_DEFAULTS.get(theme_name, "white")
        tc_code = TEXT_COLORS.get(resolved, "")
        utf8_print(f"  Text colour:  {tc_code}auto{RESET}  (using {tc_code}{resolved}{RESET} for {theme_name} theme)")
    else:
        tc_code = TEXT_COLORS.get(tc, "")
        utf8_print(f"  Text colour:  {tc_code}{tc}{RESET}")
    # Update check
    local = get_local_commit()
    if local:
        update = check_for_update(interactive=True)
        if update:
            utf8_print(f"  Update:       {BRIGHT_YELLOW}available{RESET}  (run {BOLD}/pulse update{RESET} or {BOLD}--update{RESET})")
        elif update is False:
            utf8_print(f"  Update:       {GREEN}up to date (v{VERSION}){RESET}")
        else:
            utf8_print(f"  Update:       {DIM}check failed{RESET}")
    # Claude Code update check
    if _CLAUDE_PATH:
        try:
            result = subprocess.run(
                [_CLAUDE_PATH, "--version"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                local_ver = _sanitize(result.stdout.strip().split()[0])
                cc_update = check_claude_code_update(local_ver, interactive=True)
                if cc_update:
                    # Read cached remote version
                    try:
                        cc_cache_path = get_state_dir() / "claude_code_update.json"
                        with open(cc_cache_path, "r", encoding="utf-8") as f:
                            cc_cached = json.load(f)
                        remote_ver = _sanitize(cc_cached.get("remote", "?"))
                    except Exception:
                        remote_ver = "newer"
                    # Claude Code updates itself in the background: a version
                    # that is already on disk only needs a restart.
                    if _version_cmp(_installed_claude_version(), remote_ver) in (0, 1):
                        utf8_print(f"  Claude Code:  {BRIGHT_YELLOW}{local_ver} \u2192 {remote_ver} installed{RESET}  ({BOLD}restart Claude Code{RESET} to use it)")
                    else:
                        utf8_print(f"  Claude Code:  {BRIGHT_YELLOW}{local_ver} \u2192 {remote_ver} available{RESET}  (run {BOLD}claude update{RESET} in a new terminal)")
                elif cc_update is False:
                    utf8_print(f"  Claude Code:  {GREEN}{local_ver} (up to date){RESET}")
                else:
                    utf8_print(f"  Claude Code:  {DIM}{local_ver} (check failed){RESET}")
        except Exception:
            utf8_print(f"  Claude Code:  {DIM}check failed{RESET}")

    # Extra credits status — check the API
    utf8_print(f"\n  {BOLD}Extra Credits:{RESET}")
    try:
        token, _ = get_credentials()
        if token:
            _usage = fetch_usage(token)
            _extra = _usage.get("extra_usage")
            if _extra and _extra.get("is_enabled"):
                currency = config.get("currency", "$")
                used = (_extra.get("used_credits") or 0) / 100  # API returns pence/cents
                limit = (_extra.get("monthly_limit") or 0) / 100
                pct = min(_extra.get("utilization") or 0, 100)
                utf8_print(f"    Status:    {GREEN}active{RESET}")
                utf8_print(f"    Used:      {currency}{used:.2f} / {currency}{limit:.2f} ({pct:.0f}%)")
                if config.get("extra_hidden"):
                    utf8_print(f"    Display:   {RED}hidden{RESET}  (run {BOLD}--show extra{RESET} to re-enable)")
                else:
                    utf8_print(f"    Display:   {GREEN}auto-shown{RESET}  (run {BOLD}--hide extra{RESET} to suppress)")
            else:
                utf8_print(f"    Status:    {DIM}not active{RESET}")
                if show.get("extra", False):
                    utf8_print(f"    Display:   {GREEN}on{RESET} (forced)  — will show 'none' until credits are gifted")
                else:
                    utf8_print(f"    Display:   {DIM}auto{RESET} — will appear when credits are gifted")
        else:
            utf8_print(f"    Status:    {DIM}unknown{RESET} (no credentials)")
    except Exception:
        utf8_print(f"    Status:    {DIM}check failed{RESET}")

    # providers (3.4.0)
    try:
        _pentries = _normalize_providers(config.get("providers"))
        utf8_print(f"\n  {BOLD}Providers:{RESET}")
        if _pentries:
            _pactive = _active_provider_id(config, _pentries)
            for _pe in _pentries:
                _ploc = _provider_location(_pe)
                _pstate = f"{GREEN}on{RESET} " if _pe.get("enabled") else f"{RED}off{RESET}"
                _pmark = f"  {DIM}(this window's account){RESET}" if _pe["id"] == _pactive else ""
                _plabel = _sanitize(_pe.get("label") or _provider_default_label(_pe))
                _psrc = _sanitize(_tilde(_ploc)) if _ploc else "mmx"
                utf8_print(f"    {_pe['id']:<16} {_pstate} {_plabel:<24} {_psrc}{_pmark}")
        else:
            utf8_print(f"    {DIM}none{RESET}  (run {BOLD}--detect-providers{RESET})")
    except Exception:
        utf8_print(f"    {DIM}unreadable{RESET}")

    utf8_print(f"\n  {BOLD}Visibility:{RESET}")
    for key in DEFAULT_SHOW:
        state = f"{GREEN}on{RESET}" if show.get(key, DEFAULT_SHOW[key]) else f"{RED}off{RESET}"
        utf8_print(f"    {key:<10} {state}")
    utf8_print("")


# ---------------------------------------------------------------------------
# Providers CLI (3.4.0): --detect-providers, --providers ...
# ---------------------------------------------------------------------------

_GEMINI_AUTO_ENABLE_AGE = 7 * 86400  # an agy login unused for a week stays off


def _claude_creds_meta(config_dir):
    """``(has_token, plan, expired)`` from ``<dir>/.credentials.json``.

    The token itself is only tested for truthiness and never returned.
    """
    try:
        data, _ = _read_small_json(Path(config_dir) / ".credentials.json", limit=64_000)
    except (OSError, ValueError):
        return False, None, False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return False, None, False
    try:
        expires = float(oauth.get("expiresAt") or 0) / 1000.0
    except (TypeError, ValueError):
        expires = 0.0
    return True, _plan_from_tier(oauth.get("rateLimitTier")), bool(expires and expires <= time.time())


def _age_seconds(ts, now):
    try:
        return max(0, int(now - float(ts))) if ts else None
    except (TypeError, ValueError):
        return None


def _format_age(seconds):
    if seconds is None:
        return ""
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def detect_providers(config=None):
    """Find the subscriptions this machine has. Never prints, never raises.

    Returns a JSON-serialisable list of
    ``{id, type, label, path, active, enabled, tier, age_seconds, note}``:

    * Claude: ``~/.claude``, ``~/.claude-*`` and ``$CLAUDE_CONFIG_DIR``, kept
      only when ``.credentials.json`` holds a token (on macOS a dir that has
      only a Keychain login is listed as ``keychain-only``, disabled).
    * Codex: ``$CODEX_HOME`` or ``~/.codex`` with a rollout file or a ChatGPT
      login in ``auth.json``.
    * Gemini: ``~/.gemini/antigravity-cli*/statusline_last_live.json``.
    * MiniMax: the ``mmx`` CLI on PATH (always ``enabled: False`` unless the
      user enabled it: it spawns a process).
    * Configured entries detection cannot see (``file`` rows, vanished dirs).

    ``enabled`` is the configured state for an id already in
    ``config["providers"]``, otherwise the recommended default. ``path`` uses
    ``~``. No e-mail address or account name is read into the result: labels
    come from the config, ``~/.claude-accounts.json`` labels, or defaults.
    """
    now = time.time()
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}
    configured = {e["id"]: e for e in _normalize_providers((config or {}).get("providers"))}
    found = []
    used_ids = set()

    def _add(item):
        conf = configured.get(item["id"])
        if conf is not None:
            item["enabled"] = bool(conf.get("enabled"))
            if conf.get("label"):
                item["label"] = conf["label"]
        used_ids.add(item["id"])
        found.append(item)

    # Claude accounts
    try:
        active_key = _path_key(_claude_config_dir())
    except Exception:
        active_key = None
    home = Path.home()
    candidates = [home / ".claude"]
    try:
        candidates += sorted(p for p in home.glob(".claude-*") if p.is_dir())
    except OSError:
        pass
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env_dir:
        candidates.append(Path(os.path.expanduser(env_dir)))
    seen_dirs = set()
    for d in candidates:
        try:
            if not d.is_dir():
                continue
            key = _path_key(d)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            has_token, plan, expired = _claude_creds_meta(d)
            note = "ok"
            if not has_token:
                is_default = key == _path_key(home / ".claude")
                if sys.platform == "darwin" and ((d / ".claude.json").exists() or is_default):
                    # No token file to read; its row still fills from that
                    # account's own window (stdin seeding) while one is open.
                    note = "keychain-only"
                else:
                    continue
            elif expired:
                note = "expired token"
            pid = _claude_provider_id_for_dir(d)
            base, n = pid, 2
            while pid in used_ids:
                pid = f"{base[:29]}-{n}"
                n += 1
            active = key == active_key
            if active:
                note = "active"
            cache = _read_provider_cache(pid) or {}
            _add({"id": pid, "type": "claude", "label": _default_claude_label(d),
                  "path": _tilde(d), "active": active,
                  "enabled": note != "keychain-only", "tier": plan,
                  "age_seconds": _age_seconds(cache.get("timestamp"), now), "note": note})
        except Exception:
            continue

    # Codex
    try:
        codex = _codex_home(configured.get("codex"))
        files = _find_codex_rollouts(codex, 1)
        # A fresh ``codex login`` has no rollout yet, but the usage API answers.
        token, _, jwt_plan = _read_codex_token(codex, now)
        has_login = bool(token)
        token = None
        if files or has_login:
            mtime = files[0].stat().st_mtime if files else None
            found_rl = None
            try:
                found_rl = _codex_rate_limits_from_tail(files[0], now) if files else None
            except OSError:
                pass
            _add({"id": "codex", "type": "codex", "label": "Codex", "path": _tilde(codex),
                  "active": False, "enabled": True,
                  "tier": (found_rl[1] if found_rl else None) or _codex_tier_label(jwt_plan),
                  "age_seconds": _age_seconds(mtime, now),
                  "note": "ok" if (found_rl or has_login) else "no rate limits in newest rollout yet"})
    except Exception:
        pass

    # Gemini / Antigravity CLI logins
    try:
        gem_root = home / ".gemini"
        dirs = sorted(p for p in gem_root.glob("antigravity-cli*") if p.is_dir())
    except OSError:
        dirs = []
    for d in dirs:
        try:
            status = d / _GEMINI_STATUS_FILE
            if not status.is_file():
                continue
            suffix = d.name[len("antigravity-cli"):].lstrip("-_").lower()
            pid = "gemini" + (f":{suffix}" if suffix else "")
            if not _normalize_provider_id(pid) or pid in used_ids:
                continue
            r = _read_gemini_quota(d, "gemini", now) or {}
            age = _age_seconds(status.stat().st_mtime, now)
            fresh = age is not None and age < _GEMINI_AUTO_ENABLE_AGE
            _add({"id": pid, "type": "gemini",
                  "label": _provider_default_label({"id": pid, "type": "gemini"}),
                  "path": _tilde(d), "active": False, "enabled": fresh,
                  "tier": r.get("tier"), "age_seconds": age,
                  "note": "ok" if fresh else f"stale {_format_age(age)}"})
        except Exception:
            continue

    # MiniMax
    try:
        if shutil.which("mmx"):
            _add({"id": "minimax", "type": "minimax", "label": "MiniMax", "path": None,
                  "active": False, "enabled": False, "tier": None, "age_seconds": None,
                  "note": "runs mmx quota show; off unless enabled"})
    except Exception:
        pass

    # Configured entries detection did not find
    for pid, entry in configured.items():
        if pid in used_ids:
            continue
        loc = _provider_location(entry)
        exists = False
        try:
            exists = bool(loc) and loc.exists()
        except OSError:
            pass
        _add({"id": pid, "type": entry["type"],
              "label": entry.get("label") or _provider_default_label(entry),
              "path": _tilde(loc) if loc else None, "active": False,
              "enabled": bool(entry.get("enabled")), "tier": None, "age_seconds": None,
              "note": "configured" if exists else "configured; source not found"})
    return found


def _provider_entry_from_detected(item):
    entry = {"id": item["id"], "type": item["type"], "enabled": True}
    if item.get("label"):
        entry["label"] = item["label"]
    if item.get("path"):
        entry["dir" if item["type"] == "claude" else "path"] = item["path"]
    return entry


def set_providers(config, enable_ids=(), disable_ids=(), labels=None):
    """Enable / disable / label providers in ``config["providers"]``.

    Ids already configured are updated in place (an existing label is kept);
    detected ids not yet configured are added. Mutates and returns
    ``config``; the caller runs ``save_config``. All ids are validated
    before anything changes: an unknown id raises ``ValueError`` with a
    readable message and leaves ``config`` untouched.
    """
    labels = dict(labels or {})
    providers = _normalize_providers(config.get("providers"))
    by_id = {p["id"]: p for p in providers}
    detected = None
    newly_added = set()

    def _resolve(raw):
        nonlocal detected
        pid = _normalize_provider_id(raw)
        if pid is None:
            raise ValueError(f"'{_sanitize(str(raw))[:40]}' is not a valid provider id "
                             "(letters, digits, ':', '_' and '-'; up to 32 characters).")
        if pid in by_id:
            return pid
        if detected is None:
            detected = {d["id"]: d for d in detect_providers(config)}
        if pid not in detected:
            known = ", ".join(sorted(set(by_id) | set(detected))) or "none"
            raise ValueError(f"Unknown provider '{pid}'. Known on this machine: {known}. "
                             "For a JSON file use: --providers add-file <id> <path>")
        if len(by_id) >= _MAX_PROVIDERS:
            raise ValueError(f"At most {_MAX_PROVIDERS} providers can be configured; "
                             "remove one first (--providers remove <id>).")
        entry = _provider_entry_from_detected(detected[pid])
        by_id[pid] = entry
        providers.append(entry)
        newly_added.add(pid)
        return pid

    plan = []
    for raw in enable_ids or ():
        plan.append(("enable", _resolve(raw), None))
    for raw in disable_ids or ():
        plan.append(("disable", _resolve(raw), None))
    for raw, text in labels.items():
        pid = _resolve(raw)
        if text is not None and not isinstance(text, str):
            raise ValueError(f"Label for '{pid}' must be text.")
        plan.append(("label", pid, text))
    toggled = {pid for action, pid, _ in plan if action != "label"}
    for action, pid, text in plan:
        entry = by_id[pid]
        if action == "enable":
            entry["enabled"] = True
        elif action == "disable":
            entry["enabled"] = False
        else:
            clean = _clean_provider_label(text) if text else None
            if clean:
                entry["label"] = clean
            else:
                entry.pop("label", None)
    # A detected id that was only labelled (never enabled) is added switched off.
    for pid in newly_added - toggled:
        by_id[pid]["enabled"] = False
    config["providers"] = _normalize_providers(providers)
    return config


def cmd_detect_providers(as_json=False):
    items = detect_providers()
    if as_json:
        sys.stdout.write(json.dumps(items, indent=2) + "\n")
        return
    if not items:
        utf8_print("No other subscriptions found (Claude accounts, Codex, Gemini/Antigravity, MiniMax).")
        return
    utf8_print(f"\n{BOLD}Subscriptions on this machine{RESET}\n")
    _print_provider_table(items)
    suggest = [i["id"] for i in items if i["enabled"] and not i["active"]]
    configured = {e["id"] for e in _normalize_providers(load_config().get("providers"))}
    todo = [pid for pid in suggest if pid not in configured]
    if todo:
        utf8_print(f"\n  Turn rows on with:  {BOLD}--providers enable {','.join(todo)}{RESET}")
    utf8_print(f"  {DIM}The active account is the main line; it gets a row in your other windows.{RESET}\n")


def _print_provider_table(items):
    head = f"  {'ID':<16} {'TYPE':<8} {'LABEL':<24} {'ON':<4} {'SOURCE':<34} STATUS"
    utf8_print(f"{DIM}{head}{RESET}")
    for i in items:
        on = f"{GREEN}on{RESET}  " if i.get("enabled") else f"{DIM}off{RESET} "
        status = _sanitize(i.get("note") or "")
        age = _format_age(i.get("age_seconds"))
        if age and status in ("ok", "active", "configured"):
            status += f" ({age} ago)" if i["type"] != "claude" else f" (data {age} old)"
        if i.get("tier"):
            status += f"  {_sanitize(i['tier'])}"
        src = _sanitize(i.get("path") or "-")[:34]
        utf8_print(f"  {_sanitize(i['id']):<16} {i['type']:<8} {_sanitize(i['label']):<24} "
                   f"{on} {src:<34} {status}")


_PROVIDERS_USAGE = """Usage: --providers <command>
  list                          configured providers (default)
  detect [--json]               what this machine has (same as --detect-providers)
  enable <id>[,<id>...]         turn rows on   e.g. --providers enable claude:b,codex
  disable <id>[,<id>...]        turn rows off
  label <id> <text>             rename a row   e.g. --providers label claude:b Work
  remove <id>                   drop an entry from the config
  add-file <id> <path> [label]  a row read from a JSON file another tool writes"""


def _split_ids(args):
    out = []
    for a in args:
        out += [p.strip() for p in a.split(",") if p.strip()]
    return out


def cmd_providers(argv):
    """Handle ``--providers ...``. Returns a process exit code (0 = ok)."""
    argv = list(argv or [])
    action = argv[0].lower() if argv else "list"
    rest = argv[1:]
    if action in ("-h", "--help", "help"):
        utf8_print(_PROVIDERS_USAGE)
        return 0
    if action == "detect":
        cmd_detect_providers(as_json="--json" in rest)
        return 0
    config = load_config()
    if action == "list":
        entries = _normalize_providers(config.get("providers"))
        if not entries:
            utf8_print("No providers configured. Run --detect-providers to see what this machine has.")
            return 0
        active = _active_provider_id(config, entries)
        items = []
        for e in entries:
            loc = _provider_location(e)
            items.append({"id": e["id"], "type": e["type"],
                          "label": e.get("label") or _provider_default_label(e),
                          "path": _tilde(loc) if loc else None, "enabled": e.get("enabled"),
                          "active": e["id"] == active, "tier": None, "age_seconds": None,
                          "note": "active (main line)" if e["id"] == active else ""})
        utf8_print("")
        _print_provider_table(items)
        if not (config.get("show") or {}).get("providers", True):
            utf8_print(f"\n  {YELLOW}Rows are hidden{RESET} (run --show providers)")
        utf8_print("")
        return 0
    try:
        if action in ("enable", "disable"):
            ids = _split_ids(rest)
            if not ids:
                utf8_print(_PROVIDERS_USAGE)
                return 2
            if action == "enable":
                set_providers(config, enable_ids=ids)
            else:
                set_providers(config, disable_ids=ids)
            msg = f"{'Enabled' if action == 'enable' else 'Disabled'}: {', '.join(i.lower() for i in ids)}"
        elif action == "label":
            if len(rest) < 2:
                utf8_print(_PROVIDERS_USAGE)
                return 2
            text = " ".join(rest[1:])
            set_providers(config, labels={rest[0]: text})
            msg = f"Label for {rest[0].lower()}: {_clean_provider_label(text) or '(default)'}"
        elif action == "remove":
            if len(rest) != 1:
                utf8_print(_PROVIDERS_USAGE)
                return 2
            pid = _normalize_provider_id(rest[0])
            entries = _normalize_providers(config.get("providers"))
            if pid is None or pid not in {e["id"] for e in entries}:
                raise ValueError(f"'{_sanitize(rest[0])[:40]}' is not configured "
                                 "(see --providers list).")
            config["providers"] = [e for e in entries if e["id"] != pid]
            msg = f"Removed: {pid}"
        elif action == "add-file":
            if len(rest) < 2:
                utf8_print(_PROVIDERS_USAGE)
                return 2
            raw_id = rest[0].strip().lower()
            if raw_id.startswith("file:"):
                raw_id = raw_id[len("file:"):]
            pid = _normalize_provider_id("file:" + raw_id)
            if pid is None or not raw_id:
                raise ValueError(f"'{_sanitize(rest[0])[:40]}' is not a valid id "
                                 "(letters, digits, '_' and '-').")
            path = rest[1].strip()
            if not path or "\x00" in path or len(path) > 1024:
                raise ValueError("Give the path of the JSON file.")
            entries = _normalize_providers(config.get("providers"))
            label = _clean_provider_label(" ".join(rest[2:])) if len(rest) > 2 else None
            existing = next((e for e in entries if e["id"] == pid), None)
            if existing is None:
                if len(entries) >= _MAX_PROVIDERS:
                    raise ValueError(f"At most {_MAX_PROVIDERS} providers can be configured; "
                                     "remove one first (--providers remove <id>).")
                existing = {"id": pid, "type": "file", "enabled": True}
                entries.append(existing)
            existing.update({"path": path, "enabled": True})
            if label:
                existing["label"] = label
            config["providers"] = _normalize_providers(entries)
            msg = f"Added {pid} -> {_sanitize(path)}"
            if not Path(os.path.expanduser(path)).is_file():
                msg += f"\n  {YELLOW}Note:{RESET} the file does not exist yet; the row shows -- until it does."
        else:
            utf8_print(f"Unknown --providers command '{_sanitize(action)[:40]}'.\n{_PROVIDERS_USAGE}")
            return 2
    except ValueError as exc:
        utf8_print(f"{RED}Error:{RESET} {exc}")
        return 1
    save_config(config)
    try:
        os.remove(get_cache_path())
    except OSError:
        pass
    utf8_print(msg)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Every command-line flag main() understands: (flag, args, group, help).
# The single source for --help, the README flag audit and the /pulse docs;
# tests/test_help.py checks it against main()'s `"--flag" in args` chain both
# ways, so a new flag must be listed here.
CLI_GROUPS = ("Setup", "Look", "Widgets", "Providers", "Hooks", "Info", "Internal")
CLI_FLAGS = [
    ("--setup", "[--non-interactive]", "Setup", "Guided setup: status line, hooks, providers, theme, widgets"),
    ("--install", "", "Setup", "Install the status line, agent-panel rows and /pulse (no questions)"),
    ("--update", "", "Setup", "Update a git install to the latest release"),
    ("--uninstall", "", "Setup", "Remove the status line, hooks and /pulse from Claude Code's settings"),
    ("--help", "", "Setup", "Show this list (also -h)"),
    ("-h", "", "Setup", "Same as --help"),
    ("--theme", "<name>", "Look", "Set the colour theme (see --themes)"),
    ("--pick-theme", "", "Look", "Arrow-key theme picker with a live preview"),
    ("--pick", "", "Look", "Same as --pick-theme"),
    ("--themes", "", "Look", "List themes"),
    ("--themes-demo", "", "Look", "Preview the status line in every theme"),
    ("--show-themes", "", "Look", "Theme previews in their accent colours"),
    ("--show-colors", "", "Look", "List text colours"),
    ("--show-all", "", "Look", "--show-themes and --show-colors together"),
    ("--text-color", "<name|auto>", "Look", "Colour for labels and percentages"),
    ("--animate", "<off|rainbow|pulse|glow|shift>", "Look", "Bar animation"),
    ("--animation-speed", "<slow|normal|fast>", "Look", "Animation speed"),
    ("--bar-size", "<small|small-medium|medium|medium-large|large|auto>", "Look", "Bar width (auto shrinks to fit)"),
    ("--bar-style", "<name>", "Look", "Bar characters (classic, block, dot, braille, ...)"),
    ("--layout", "<standard|compact|minimal|percent-first>", "Look", "How labels, bars and numbers are arranged"),
    ("--max-width", "<20-100>", "Look", "Percent of the terminal width to use"),
    ("--wrap", "<off|auto>", "Look", "Wrap onto up to 4 rows instead of truncating"),
    ("--preset", "<minimal|default>", "Look", "Apply a bundle of settings (default = factory reset)"),
    ("--show", "<parts>", "Widgets", "Turn widgets on (comma-separated)"),
    ("--hide", "<parts>", "Widgets", "Turn widgets off (comma-separated)"),
    ("--priority", "<widget=N,...>", "Widgets", "Reorder widgets (lower = further left)"),
    ("--currency", "<symbol|code>", "Widgets", "Currency for cost widgets: a symbol or ISO code such as DKK (converted from USD)"),
    ("--clock-format", "<12h|24h>", "Widgets", "Clock for reset times"),
    ("--effort-format", "<labeled|full|short>", "Widgets", "How reasoning effort is written"),
    ("--context-format", "<percent|tokens>", "Widgets", "Context window as a percentage or tokens"),
    ("--extra-display", "<auto|full|amount>", "Widgets", "How extra-usage credits are shown"),
    ("--weekly-timer-format", "<auto|countdown|date|full>", "Widgets", "Weekly reset timer style"),
    ("--weekly-timer-prefix", "<text>", "Widgets", "Text before the weekly reset time (e.g. R:)"),
    ("--streak-style", "<fire|text>", "Widgets", "Usage streak style"),
    ("--pace-style", "<arrows|percent>", "Widgets", "Pace widget: arrows against the clock, or the elapsed share"),
    ("--budget", "<amount|off>", "Widgets", "Spend budget to match --max-budget-usd"),
    ("--limits", "[key=N,...]", "Widgets", "Subagent and web-search caps used as denominators"),
    ("--focus", "<start [min]|stop|status>", "Widgets", "Focus (pomodoro) timer"),
    ("--detect-providers", "[--json]", "Providers", "Find other Claude accounts, Codex, Gemini and MiniMax on this machine"),
    ("--providers", "<list|detect|enable|disable|label|remove|add-file> [...]", "Providers",
     "Manage the extra status rows: --providers help lists the sub-commands"),
    ("--install-hooks", "[--only heartbeat,agents,team]", "Hooks", "Install hooks (all three groups unless --only)"),
    ("--config", "", "Info", "Print the current configuration"),
    ("--stats", "", "Info", "Usage statistics and streaks"),
    ("--heatmap", "", "Info", "Usage heatmap"),
    ("--debug-stdin", "", "Info", "Save and parse the JSON Claude Code pipes in"),
    ("--subagent-status-line", "", "Internal", "Agent-panel rows (run by Claude Code)"),
    ("--hook-refresh", "[tool]", "Internal", "PostToolUse hook"),
    ("--hook-subagent-start", "", "Internal", "SubagentStart hook"),
    ("--hook-subagent-stop", "", "Internal", "SubagentStop hook"),
    ("--hook-teammate-idle", "", "Internal", "TeammateIdle hook"),
    ("--hook-task-created", "", "Internal", "TaskCreated hook"),
    ("--hook-task-completed", "", "Internal", "TaskCompleted hook"),
    ("--sync-install-path", "", "Internal", "Plugin SessionStart hook: re-point settings.json after a plugin update"),
]
# Options that only ever follow another flag; known to the unknown-flag guard.
_CLI_SUBOPTIONS = ("--non-interactive", "--only", "--json")


def cmd_help():
    """Print CLI_FLAGS grouped, for --help / -h."""
    utf8_print(f"usage-pulse {VERSION} — usage status line for Claude Code")
    utf8_print("")
    utf8_print("Usage: python claude_status.py [flag [args]]")
    utf8_print("With no flag and JSON on stdin (how Claude Code runs it) it prints the status line.")
    for group in CLI_GROUPS:
        rows = [r for r in CLI_FLAGS if r[2] == group and r[0] != "-h"]
        if not rows:
            continue
        utf8_print("")
        utf8_print(group)
        for flag, fargs, _group, text in rows:
            left = f"{flag} {fargs}".strip()
            if len(left) > 34:
                utf8_print(f"  {left}")
                utf8_print(f"  {'':<34}  {text}")
            else:
                utf8_print(f"  {left:<34}  {text}")
    utf8_print("")
    utf8_print("Inside Claude Code: /pulse (or /pulse setup) does all of this by asking you.")


def _unknown_cli_flags(args):
    """Flags in ``args`` that neither CLI_FLAGS nor a sub-option knows."""
    known = {row[0] for row in CLI_FLAGS} | set(_CLI_SUBOPTIONS)
    return [a for a in args if a.startswith("-") and len(a) > 1 and a not in known
            and not a.lstrip("-").replace(".", "").isdigit()]


def _spend_limit_live(spend):
    """True for a stdin ``spend_limit`` block whose period is still running.

    Claude Code drops the block once its ``resets_at`` passes, so one carried
    in the persisted context past that time is old news, not a gateway.
    """
    if not isinstance(spend, dict) or spend.get("used_percentage") is None:
        return False
    reset = _parse_reset(spend.get("resets_at"))
    if reset is None:
        return True
    try:
        return reset.timestamp() > time.time()
    except (OverflowError, OSError, ValueError):
        return True


def _stdin_has_session_data(stdin_ctx):
    """Does stdin give anything to draw without a usage window (F08)?"""
    if not isinstance(stdin_ctx, dict):
        return False
    return (any(stdin_ctx.get(k) is not None for k in ("model_name", "context_pct", "cost_usd"))
            or _spend_limit_live(stdin_ctx.get("spend_limit")))


def _stdin_only_line(config, stdin_ctx):
    """The status line for a window with session data but no usage windows.

    A Claude apps gateway sends only ``spend_limit``; Bedrock, Vertex, Foundry
    and API-key sessions send model, context and cost and no ``rate_limits``
    at all. Nothing of theirs is a Session or Weekly reading, so those two
    bars are left out instead of drawn as a made-up 0 %. Never reads
    credentials and never calls the usage API (F08).
    """
    cfg = dict(config)
    show = dict(config.get("show", DEFAULT_SHOW))
    show["session"] = False
    show["weekly"] = False
    cfg["show"] = show
    line = build_status_line({}, "", cfg, stdin_ctx, cache_age=None)
    _update_session_state({}, stdin_ctx, config)
    if stdin_ctx.get("context_pct") is not None:
        _append_context_history(stdin_ctx["context_pct"])
    line = append_update_indicator(line, config)
    line = append_claude_update_indicator(line, config, stdin_ctx)
    return _fit_line(_append_provider_rows(line, config), config)


def main():
    # Handle SIGPIPE gracefully on Unix (e.g. when piped to head)
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    args = sys.argv[1:]

    if "--update" in args:
        cmd_update()
        return

    if "--help" in args or "-h" in args:
        cmd_help()
        return

    # A mistyped flag typed at a terminal used to fall through to the status
    # line and print a bar. Status-line mode always has stdin piped, so this
    # guard never fires for Claude Code itself.
    if args and args[0].startswith("-") and _unknown_cli_flags(args[:1]):
        try:
            _interactive_stdin = sys.stdin.isatty()
        except (AttributeError, ValueError, OSError):
            _interactive_stdin = False
        if _interactive_stdin:
            utf8_print(f"Unknown flag: {_sanitize(args[0])}  (see --help)")
            sys.exit(2)

    if "--setup" in args:
        code = cmd_setup(args)
        if code:
            sys.exit(code)
        return

    if "--sync-install-path" in args:
        code = cmd_sync_install_path()
        if code:
            sys.exit(code)
        return

    if "--uninstall" in args:
        code = cmd_uninstall()
        if code:
            sys.exit(code)
        return

    if "--install" in args:
        install_status_line()
        install_pulse_command()
        return

    if "--preset" in args:
        idx = args.index("--preset")
        if idx + 1 < len(args):
            cmd_preset(args[idx + 1].lower())
        else:
            utf8_print("Usage: --preset <name>\n")
            for pname, pdata in PRESETS.items():
                utf8_print(f"  {BOLD}{pname:<10}{RESET} {pdata['description']}")
        return

    if "--show-all" in args:
        cmd_show_all()
        return

    if "--show-themes" in args:
        cmd_show_themes()
        return

    if "--show-colors" in args:
        cmd_show_colors()
        return

    if "--themes-demo" in args:
        cmd_themes_demo()
        return

    if "--pick-theme" in args or "--pick" in args:
        cmd_pick_theme()
        return

    if "--themes" in args:
        cmd_list_themes()
        return

    if "--theme" in args:
        idx = args.index("--theme")
        if idx + 1 < len(args):
            cmd_set_theme(args[idx + 1])
        else:
            utf8_print("Usage: --theme <name>")
        return

    if "--show" in args:
        idx = args.index("--show")
        if idx + 1 < len(args):
            cmd_show(args[idx + 1])
        else:
            utf8_print("Usage: --show <parts>  (comma-separated: session,weekly,plan,timer,extra,update)")
        return

    if "--hide" in args:
        idx = args.index("--hide")
        if idx + 1 < len(args):
            cmd_hide(args[idx + 1])
        else:
            utf8_print("Usage: --hide <parts>  (comma-separated: session,weekly,plan,timer,extra,update)")
        return

    if "--priority" in args:
        idx = args.index("--priority")
        if idx + 1 < len(args):
            raw = args[idx + 1]
            config = load_config()
            wp = config.get("widget_priority", {})
            valid = set(WIDGET_PRIORITY.keys())
            for pair in raw.split(","):
                if "=" not in pair:
                    utf8_print(f"Bad format: {_sanitize(pair)} (use widget=number)")
                    return
                wid, val = pair.split("=", 1)
                wid = wid.strip().lower()
                if wid not in valid:
                    utf8_print(f"Unknown widget: {_sanitize(wid)} (valid: {', '.join(sorted(valid))})")
                    return
                try:
                    wp[wid] = int(val)
                except ValueError:
                    utf8_print(f"Bad priority: {_sanitize(val)} (must be a number)")
                    return
            config["widget_priority"] = wp
            save_config(config)
            utf8_print(f"Widget priorities updated: {', '.join(f'{k}={v}' for k, v in wp.items())}")
        else:
            utf8_print("Usage: --priority widget=N,widget=N")
            utf8_print(f"\nDefaults:")
            for wid, pri in sorted(WIDGET_PRIORITY.items(), key=lambda x: x[1]):
                utf8_print(f"  {wid:20s} {pri}")
        return

    if "--text-color" in args:
        idx = args.index("--text-color")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in TEXT_COLORS and val != "auto":
                utf8_print(f"Unknown colour: {_sanitize(val)}")
                utf8_print(f"Available: auto, {', '.join(TEXT_COLORS.keys())}")
                return
            config = load_config()
            config["text_color"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            if val == "auto":
                resolved = THEME_TEXT_DEFAULTS.get(config.get("theme", "default"), "white")
                utf8_print(f"Text colour: {BOLD}auto{RESET} (using {resolved} for {config.get('theme', 'default')} theme)")
            else:
                code = TEXT_COLORS.get(val, "")
                utf8_print(f"Text colour: {code}{BOLD}{val}{RESET}")
        else:
            utf8_print(f"Usage: --text-color <name>")
            utf8_print(f"Available: auto, {', '.join(TEXT_COLORS.keys())}")
        return

    if "--animate" in args:
        idx = args.index("--animate")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            # Legacy compat: on/off map to rainbow/off
            if val in ("on", "true", "yes", "1"):
                val = "rainbow"
            elif val in ("off", "false", "no", "0"):
                val = "off"
            if val not in ANIMATE_MODES:
                utf8_print(f"Unknown animation: {_sanitize(val)}")
                utf8_print(f"Available: {', '.join(ANIMATE_MODES)}")
                return
            config = load_config()
            config["animate"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            descriptions = {
                "off": "static, no animation",
                "rainbow": "flowing rainbow gradient (overwrites theme colours)",
                "pulse": "bars fade between theme colour and white",
                "glow": "bars brighten and dim dramatically",
                "shift": "bright highlight slides across the bar",
            }
            if val == "off":
                utf8_print(f"Animation: {RED}off{RESET}  (static)")
            else:
                utf8_print(f"Animation: {GREEN}{val}{RESET}  ({descriptions.get(val, '')})")
        else:
            utf8_print(f"Usage: --animate <mode>\n")
            utf8_print(f"  {'off':<10} Static, no animation")
            utf8_print(f"  {'rainbow':<10} Flowing rainbow gradient")
            utf8_print(f"  {'pulse':<10} Bars fade to white and back")
            utf8_print(f"  {'glow':<10} Bars brighten and dim")
            utf8_print(f"  {'shift':<10} Highlight slides across bars")
        return

    if "--bar-size" in args:
        idx = args.index("--bar-size")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in BAR_SIZE_CHOICES:
                utf8_print(f"Unknown size: {_sanitize(val)}")
                utf8_print(f"Available: {', '.join(BAR_SIZE_CHOICES)}")
                return
            config = load_config()
            config["bar_size"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            bw, fit = _bar_width_setting(config)
            demo_bar = f"{GREEN}{FILL * bw}{RESET}"
            note = "large, shrinking to fit a narrow window" if fit else f"{bw} chars"
            utf8_print(f"Bar size: {BOLD}{val}{RESET} ({note})  {demo_bar}")
        else:
            utf8_print(f"Usage: --bar-size <{'|'.join(BAR_SIZE_CHOICES)}>")
            for name, width in BAR_SIZES.items():
                demo = f"{GREEN}{FILL * width}{RESET}"
                utf8_print(f"  {name:<14} {demo}  ({width} chars)")
            utf8_print(f"  {BAR_SIZE_AUTO:<14} large, shrinking to fit a narrow window")
        return

    if "--max-width" in args:
        idx = args.index("--max-width")
        if idx + 1 < len(args):
            try:
                val = int(args[idx + 1])
                if not (20 <= val <= 100):
                    utf8_print("max-width must be between 20 and 100")
                    return
            except ValueError:
                utf8_print("max-width must be a number (percentage of terminal width)")
                return
            config = load_config()
            config["max_width"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            utf8_print(f"Max width: {BOLD}{val}%{RESET} of terminal width")
        else:
            utf8_print("Usage: --max-width <20-100>  (percentage, default 80)")
        return

    if "--wrap" in args:
        idx = args.index("--wrap")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in ("off", "auto"):
                utf8_print(f"Unknown wrap mode: {_sanitize(val)}")
                utf8_print("Available: off, auto")
                return
            config = load_config()
            config["wrap"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            if val == "auto":
                utf8_print(f"Wrap: {GREEN}auto{RESET}  (splits at | when line overflows)")
            else:
                utf8_print(f"Wrap: {BOLD}off{RESET}  (truncate, default)")
        else:
            utf8_print("Usage: --wrap <off|auto>")
            utf8_print(f"  {'off':<10} Truncate long lines (default)")
            utf8_print(f"  {'auto':<10} Wrap onto up to 4 rows at | separators when it overflows")
        return

    if "--bar-style" in args:
        idx = args.index("--bar-style")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in BAR_STYLES:
                utf8_print(f"Unknown style: {_sanitize(val)}")
                utf8_print(f"Available: {', '.join(BAR_STYLES.keys())}")
                return
            config = load_config()
            config["bar_style"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            fill_ch, empty_ch = BAR_STYLES[val]
            _grad_data = BAR_GRADIENT_STYLES.get(val)
            if _grad_data:
                _grad = _grad_data[0]
                n = len(_grad)
                colored = "".join(_grad[n - 1 - i * (n - 1) // 7] for i in range(4))
                dimmed = "".join(_grad[n - 1 - (i + 4) * (n - 1) // 7] for i in range(4))
                demo = f"{GREEN}{colored}{DIM}{dimmed}{RESET}"
            else:
                demo = f"{GREEN}{fill_ch * 4}{DIM}{empty_ch * 4}{RESET}"
            utf8_print(f"Bar style: {BOLD}{val}{RESET}  {demo}")
        else:
            utf8_print(f"Usage: --bar-style <name>\n")
            for name, (fc, ec) in BAR_STYLES.items():
                _grad_data = BAR_GRADIENT_STYLES.get(name)
                if _grad_data:
                    _grad = _grad_data[0]
                    n = len(_grad)
                    colored = "".join(_grad[n - 1 - i * (n - 1) // 7] for i in range(4))
                    dimmed = "".join(_grad[n - 1 - (i + 4) * (n - 1) // 7] for i in range(4))
                    demo = f"{GREEN}{colored}{DIM}{dimmed}{RESET}"
                else:
                    demo = f"{GREEN}{fc * 4}{DIM}{ec * 4}{RESET}"
                utf8_print(f"  {name:<10} {demo}")
        return

    if "--extra-display" in args:
        idx = args.index("--extra-display")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in ("auto", "full", "amount"):
                utf8_print(f"Unknown value: {_sanitize(val)}  (use auto, full, or amount)")
                return
            config = load_config()
            config["extra_display"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            descriptions = {
                "auto": "auto-detects (amount only if no spending limit, full bar otherwise)",
                "full": "progress bar with amount and limit",
                "amount": "spend amount only, no bar",
            }
            utf8_print(f"Extra display: {BOLD}{val}{RESET}  ({descriptions[val]})")
        else:
            utf8_print("Usage: --extra-display <auto|full|amount>")
            utf8_print(f"  {'auto':<8} Auto-detect (amount only if no spending limit)")
            utf8_print(f"  {'full':<8} Progress bar with amount and limit")
            utf8_print(f"  {'amount':<8} Spend amount only, no bar")
        return

    if "--context-format" in args:
        idx = args.index("--context-format")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in ("percent", "tokens"):
                utf8_print(f"Unknown format: {_sanitize(val)}  (use percent or tokens)")
                return
            config = load_config()
            config["context_format"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            utf8_print(f"Context format: {BOLD}{val}{RESET}")
            if val == "tokens":
                utf8_print(f"{DIM}  Note: Claude Code uses a 200k context window.")
                utf8_print(f"  The 1M window is an API-only beta feature and not used here.{RESET}")
        else:
            utf8_print("Usage: --context-format percent|tokens")
        return

    if "--layout" in args:
        idx = args.index("--layout")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in LAYOUTS:
                utf8_print(f"Unknown layout: {_sanitize(val)}")
                utf8_print(f"Available: {', '.join(LAYOUTS)}")
                return
            config = load_config()
            config["layout"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            utf8_print(f"Layout: {BOLD}{val}{RESET}")
        else:
            utf8_print(f"Usage: --layout <name>")
            utf8_print(f"Available: {', '.join(LAYOUTS)}")
        return

    if "--currency" in args:
        idx = args.index("--currency")
        if idx + 1 < len(args):
            val = _sanitize(args[idx + 1])[:5]  # strip escapes, max 5 chars
            config = load_config()
            config["currency"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            utf8_print(f"Currency symbol: {BOLD}{val}{RESET}")
        else:
            utf8_print("Usage: --currency <symbol|code>  (e.g. \u00a3, $, \u20ac, \u00a5, DKK, NOK)")
        return

    if "--weekly-timer-format" in args:
        idx = args.index("--weekly-timer-format")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in WEEKLY_TIMER_FORMATS:
                utf8_print(f"Unknown format: {_sanitize(val)}")
                utf8_print(f"Available: {', '.join(WEEKLY_TIMER_FORMATS)}")
                return
            config = load_config()
            config["weekly_timer_format"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            descriptions = {
                "auto": "date when >24h, countdown when <24h",
                "countdown": "always show countdown (2d 5h / 14h 22m)",
                "date": "always show date (Sat 5pm)",
                "full": "both date and countdown (Sat 5pm \u00b7 2d 5h)",
            }
            utf8_print(f"Weekly timer format: {BOLD}{val}{RESET}  ({descriptions[val]})")
        else:
            utf8_print(f"Usage: --weekly-timer-format <mode>\n")
            utf8_print(f"  auto       date when >24h, countdown when <24h (default)")
            utf8_print(f"  countdown  always show countdown: 2d 5h / 14h 22m / 45m")
            utf8_print(f"  date       always show date: Sat 5pm")
            utf8_print(f"  full       both: Sat 5pm \u00b7 2d 5h")
        return

    if "--weekly-timer-prefix" in args:
        idx = args.index("--weekly-timer-prefix")
        if idx + 1 < len(args):
            val = _sanitize(args[idx + 1])[:10]  # strip escapes, max 10 chars
            config = load_config()
            config["weekly_timer_prefix"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            if val:
                utf8_print(f"Weekly timer prefix: {BOLD}{val}{RESET}")
            else:
                utf8_print(f"Weekly timer prefix: {DIM}(none){RESET}")
        else:
            utf8_print('Usage: --weekly-timer-prefix <text>  (e.g. "R:", "Resets:", "")')
        return

    if "--clock-format" in args:
        idx = args.index("--clock-format")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in CLOCK_FORMATS:
                utf8_print(f"Unknown clock format: {_sanitize(val)}")
                utf8_print(f"Available: {', '.join(CLOCK_FORMATS)}")
                return
            config = load_config()
            config["clock_format"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            descriptions = {
                "12h": "12-hour with am/pm (Fri 5pm)",
                "24h": "24-hour (Fri 17:00)",
            }
            utf8_print(f"Clock format: {BOLD}{val}{RESET}  ({descriptions[val]})")
        else:
            utf8_print("Usage: --clock-format <mode>\n")
            utf8_print("  12h  12-hour with am/pm: Fri 5pm (default)")
            utf8_print("  24h  24-hour: Fri 17:00")
        return

    if "--stats" in args:
        cmd_stats()
        return

    if "--streak-style" in args:
        idx = args.index("--streak-style")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in ("fire", "text"):
                utf8_print(f"Unknown streak style: {_sanitize(val)}  (use fire or text)")
                return
            config = load_config()
            config["streak_style"] = val
            save_config(config)
            utf8_print(f"Streak style: {BOLD}{val}{RESET}")
        else:
            utf8_print("Usage: --streak-style fire|text")
        return

    if "--debug-stdin" in args:
        raw = ""
        if sys.stdin.isatty():
            utf8_print("No stdin data (interactive terminal). Pipe data or use from Claude Code.")
            return
        try:
            raw = _read_stdin_text(65536)
        except Exception:
            pass
        debug_path = get_state_dir() / "stdin_debug.json"
        try:
            with _secure_open_write(debug_path) as f:
                f.write(raw if raw else "{}")
        except OSError:
            pass
        utf8_print(f"Stdin debug written to: {debug_path}")
        if raw.strip():
            ctx = _parse_stdin_context(raw)
            utf8_print(f"Parsed context: {json.dumps(ctx, indent=2)}")
        return

    if "--heatmap" in args:
        cmd_heatmap()
        return

    if "--install-hooks" in args:
        groups = None
        if "--only" in args:
            idx = args.index("--only")
            try:
                groups = _parse_hook_groups(args[idx + 1] if idx + 1 < len(args) else "")
            except ValueError as e:
                utf8_print(f"{e}")
                utf8_print(f"Usage: --install-hooks [--only {','.join(HOOK_GROUP_NAMES)}|all]")
                sys.exit(2)
        if not install_hooks(groups):
            sys.exit(1)
        return

    if "--hook-refresh" in args:
        idx = args.index("--hook-refresh")
        tool_name = args[idx + 1] if idx + 1 < len(args) else "unknown"
        hook_refresh(tool_name)
        return

    if "--focus" in args:
        idx = args.index("--focus")
        if idx + 1 < len(args):
            action = args[idx + 1].lower()
            minutes = args[idx + 2] if idx + 2 < len(args) and action == "start" else None
            cmd_pomodoro(action, minutes)
        else:
            utf8_print("Usage: --focus start [minutes] | stop | status")
        return

    if "--animation-speed" in args:
        idx = args.index("--animation-speed")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val not in ANIMATION_SPEEDS:
                utf8_print(f"Unknown speed: {_sanitize(val)}")
                utf8_print(f"Available: {', '.join(ANIMATION_SPEEDS.keys())}")
                return
            config = load_config()
            config["animation_speed"] = val
            save_config(config)
            try:
                os.remove(get_cache_path())
            except OSError:
                pass
            utf8_print(f"Animation speed: {BOLD}{val}{RESET}  ({ANIMATION_SPEEDS[val]}x)")
        else:
            utf8_print("Usage: --animation-speed <slow|normal|fast>")
        return

    if "--subagent-status-line" in args:
        cmd_subagent_status_line()
        return

    if "--hook-subagent-start" in args:
        hook_subagent("start")
        return

    if "--hook-subagent-stop" in args:
        hook_subagent("stop")
        return

    if "--hook-teammate-idle" in args:
        hook_team("idle")
        return

    if "--hook-task-created" in args:
        hook_team("created")
        return

    if "--hook-task-completed" in args:
        hook_team("completed")
        return

    if "--budget" in args:
        idx = args.index("--budget")
        config = load_config()
        if idx + 1 < len(args):
            raw = args[idx + 1]
            try:
                value = 0.0 if raw.lower() in ("off", "none", "0") else float(raw.lstrip("$£€"))
            except ValueError:
                utf8_print("Usage: --budget <amount|off>   e.g. --budget 25  (matches --max-budget-usd)")
                return
            config["budget_usd"] = value
            save_config(config)
            if value > 0:
                utf8_print(f"Budget: {BOLD}${value:,.2f}{RESET}  (set this to match your --max-budget-usd)")
            else:
                utf8_print(f"Budget: {RED}off{RESET}")
        else:
            cur = config.get("budget_usd", 0)
            state = f"${cur:,.2f}" if cur else f"{RED}off{RESET}"
            utf8_print(f"Budget: {BOLD}{state}{RESET}")
        return

    if "--limits" in args:
        idx = args.index("--limits")
        config = load_config()
        limits = config.get("limits", dict(DEFAULT_LIMITS))
        if idx + 1 < len(args) and "=" in args[idx + 1]:
            for pair in args[idx + 1].split(","):
                if "=" not in pair:
                    continue
                key, _, val = pair.partition("=")
                key = key.strip()
                if key not in DEFAULT_LIMITS:
                    utf8_print(f"Unknown limit '{key}'. Known: {', '.join(DEFAULT_LIMITS)}")
                    return
                try:
                    limits[key] = max(0, int(val))
                except ValueError:
                    utf8_print(f"Limit '{key}' must be a whole number (0 hides the denominator).")
                    return
            config["limits"] = limits
            save_config(config)
        utf8_print(f"{BOLD}Caps{RESET} {DIM}(Claude Code enforces these; it does not report them,{RESET}")
        utf8_print(f"{DIM}     so set them here to match your setup){RESET}")
        for key, default in DEFAULT_LIMITS.items():
            cur = limits.get(key, default)
            mark = "" if cur == default else f"  {DIM}(default {default}){RESET}"
            utf8_print(f"  {key:<22} {BOLD}{cur or 'hidden'}{RESET}{mark}")
        return

    if "--effort-format" in args:
        idx = args.index("--effort-format")
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val in EFFORT_FORMATS:
                config = load_config()
                config["effort_format"] = val
                save_config(config)
                sample = _format_effort("medium", val)
                utf8_print(f"Effort format: {BOLD}{val}{RESET}  (renders as: {sample})")
            else:
                utf8_print(f"Usage: --effort-format <{'|'.join(EFFORT_FORMATS)}>")
                for fmt in EFFORT_FORMATS:
                    utf8_print(f"  {fmt:<10} {_format_effort('medium', fmt)}")
        else:
            config = load_config()
            current = config.get("effort_format", DEFAULT_EFFORT_FORMAT)
            utf8_print(f"Effort format: {BOLD}{current}{RESET}  (renders as: {_format_effort('medium', current)})")
        return

    if "--pace-style" in args:
        idx = args.index("--pace-style")
        samples = {"arrows": "\u2191 over pace, \u2193 under, \u21c8/\u21ca well off the pace, \u2192 on track",
                   "percent": "(43%), the share of the window's time that has passed"}
        if idx + 1 < len(args):
            val = args[idx + 1].lower()
            if val in PACE_STYLES:
                config = load_config()
                config["pace_style"] = val
                save_config(config)
                utf8_print(f"Pace style: {BOLD}{val}{RESET}  ({samples[val]})")
                if not config.get("show", {}).get("pace"):
                    utf8_print(f"  The pace widget is off; {BOLD}--show pace{RESET} turns it on")
            else:
                utf8_print(f"Usage: --pace-style <{'|'.join(PACE_STYLES)}>")
                for style in PACE_STYLES:
                    utf8_print(f"  {style:<10} {samples[style]}")
        else:
            config = load_config()
            current = config.get("pace_style", DEFAULT_PACE_STYLE)
            if current not in PACE_STYLES:
                current = DEFAULT_PACE_STYLE
            utf8_print(f"Pace style: {BOLD}{current}{RESET}  ({samples[current]})")
        return

    if "--detect-providers" in args:
        cmd_detect_providers(as_json="--json" in args)
        return

    if "--providers" in args:
        rc = cmd_providers(args[args.index("--providers") + 1:])
        if rc:
            raise SystemExit(rc)
        return

    if "--config" in args:
        cmd_print_config()
        return

    # Normal status line mode
    config = load_config()
    cache_ttl = config.get("cache_ttl_seconds", DEFAULT_CACHE_TTL)

    # Self-heal the repaint timer. Transitions that no command observes —
    # the heartbeat ageing past its freshness TTL is the main one — would
    # otherwise leave a stale 15s `refreshInterval` armed indefinitely.
    # Each repaint (including the timed ones that stale timer causes) checks
    # whether the timer is still wanted; a no-op costs one settings.json
    # read and never blocks on anything but local disk.
    try:
        sync_status_line_refresh(config)
    except Exception:
        pass

    # Note: _detect_status_bar_conflict() removed — it suppressed all output
    # when leftover npm @anthropic-ai/claude-code files existed on disk,
    # even after migrating to the native installer.

    # One-time cleanup of legacy hooks from pre-v2.2.0
    try:
        _cleanup_hooks()
    except Exception:
        pass

    raw_stdin = ""
    if not sys.stdin.isatty():
        try:
            raw_stdin = _read_stdin_text(65536)
        except Exception:
            pass
    stdin_ctx = _parse_stdin_context(raw_stdin)

    # Persist stdin context (model, context %) in a separate file so it
    # survives across refreshes that don't receive stdin data from Claude Code.
    # Merge new data into persisted data so partial updates (e.g. model but
    # no context_pct during thinking) don't wipe previously known fields.
    # Fields worth carrying across a refresh that arrives without stdin.
    # Deliberately excludes `agent_name`: a subagent name is only true for the
    # turn it ran on, so persisting it would leave a stale "▸reviewer" pinned
    # to the bar long after that subagent finished.
    _STDIN_CTX_KEYS = {
        "model_name", "context_pct", "context_used", "context_limit",
        "cost_usd", "worktree_branch", "_rate_limits",
        "lines_added", "lines_removed",
        "effort", "fast_mode", "thinking", "cc_version",
        "cache_hit_pct", "cache_read_tokens",
        "pr_number", "pr_url", "pr_review_state", "pr_kind",
        "prompt_cache_warm", "prompt_cache_hit_pct", "prompt_cache_expires_at",
        "spend_limit", "repo_slug", "session_name",
    }
    # Of those, only these describe the account or the machine. The rest
    # belong to the session that sent them: a new session must start clean
    # instead of showing the previous session's context % and cost until its
    # own first reply arrives (issue #55). The file carries the id of the
    # session that wrote it (never loaded into the context, see above), so a
    # change of session can be noticed.
    _STDIN_CTX_ACCOUNT_KEYS = {"_rate_limits", "cc_version", "spend_limit"}
    stdin_ctx_path = get_account_state_dir() / "stdin_ctx.json"
    persisted = {}
    persisted_sid = None
    try:
        with open(str(stdin_ctx_path), "r", encoding="utf-8") as f:
            raw_persisted = json.load(f)
        if isinstance(raw_persisted, dict):
            persisted_sid = str(raw_persisted.get("session_id") or "") or None
            persisted = {k: _strip_surrogates(_sanitize(str(v))) if isinstance(v, str) else v for k, v in raw_persisted.items() if k in _STDIN_CTX_KEYS}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # The carried rate limits can be hours or days old (they are an account
    # key, so a new session inherits them before its first reply). A window
    # whose reset has passed is over: it reads 0 %, not its old percentage
    # next to "now" (F01).
    if isinstance(persisted.get("_rate_limits"), dict):
        persisted["_rate_limits"] = _roll_over_expired_windows(persisted["_rate_limits"])
    if stdin_ctx:
        sid = str(stdin_ctx.get("session_id") or "") or None
        if sid and persisted_sid and sid != persisted_sid:
            persisted = {k: v for k, v in persisted.items() if k in _STDIN_CTX_ACCOUNT_KEYS}
        persisted.update(stdin_ctx)
        to_write = dict(persisted)
        if not sid and persisted_sid:
            to_write["session_id"] = persisted_sid   # a fragment repaint keeps the owner
        try:
            _atomic_json_write(stdin_ctx_path, to_write, indent=None)
        except OSError:
            pass
    stdin_ctx = persisted
    # Context history is kept per session (a second window's lower samples must
    # not read as this window's context filling fast).
    _set_context_session(stdin_ctx.get("session_id"))

    cache_path = get_cache_path()
    cached = read_cache(cache_path, cache_ttl)
    # A cached window can have run out since it was written (F01): it reads
    # 0 %, in the stdin branch below and in the cached branch after it alike.
    if isinstance(cached, dict) and isinstance(cached.get("usage"), dict):
        cached = dict(cached, usage=_roll_over_expired_windows(cached["usage"]))

    # --- Stdin rate limits (v2.1.80+): use data from Claude Code directly ---
    # This avoids calling the OAuth API entirely for basic session/weekly bars.
    # The API is only needed for extra credits and per-model caps (opus/sonnet).
    stdin_rl = stdin_ctx.get("_rate_limits")
    # Behind a Claude apps gateway rate_limits carries only spend_limit, so
    # there is no window above and no claude.ai login to fetch one with. The
    # spend bar and the session widgets are drawn from stdin alone, without
    # ever asking for credentials (F08).
    _gateway_run = _spend_limit_live(stdin_ctx.get("spend_limit"))
    if not stdin_rl and _gateway_run:
        line = _stdin_only_line(config, stdin_ctx)
        sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))
        return
    if stdin_rl:
        # Build a synthetic usage dict from stdin rate limits. Take *every*
        # window stdin offers, including the model-scoped weekly caps
        # (seven_day_opus / _sonnet / _fable). Previously only five_hour and
        # seven_day were copied and the per-model caps were re-fetched over
        # OAuth — which both dropped Fable entirely and made a network call for
        # data Claude Code had already handed us.
        usage_from_stdin = {k: v for k, v in stdin_rl.items() if v}
        has_model_caps = any(k.startswith("seven_day_") for k in usage_from_stdin)

        # extra_usage (bonus credits) is never on stdin, so it still comes from
        # cache, and from the API only if we have nothing cached at all.
        plan_from_cache = ""
        if cached and "usage" in cached:
            plan_from_cache = cached.get("plan", "")
            for key, value in cached["usage"].items():
                # five_hour and seven_day always come fresh from stdin. Claude
                # Code drops one once it resets, so refilling it from the
                # cache brought back the old percentage next to "now" (F01).
                if key in ("five_hour", "seven_day"):
                    continue
                if key not in usage_from_stdin:
                    usage_from_stdin[key] = value
                    if key.startswith("seven_day_"):
                        has_model_caps = True
                if key == "extra_usage":
                    has_model_caps = has_model_caps or True

        # Carry model-scoped caps and extra_usage forward from an EXPIRED
        # cache. read_cache() above returns None once the 60s TTL lapses, so
        # without this the Fable/Opus/Sonnet bars vanish on every repaint after
        # the first minute and reappear only when the refresh below happens to
        # run — a visible flicker. Weekly quotas move over days, so a value a
        # few minutes old is honest; the five-hour and weekly windows always
        # come fresh from stdin and are never overwritten here.
        _stale = _read_stale_cache(cache_path)
        _stale_usage = _roll_over_expired_windows((_stale or {}).get("usage"))
        if isinstance(_stale_usage, dict):
            for key, value in _stale_usage.items():
                if key in ("five_hour", "seven_day"):
                    continue   # stdin only, see above
                if key in _USAGE_CACHE_KEYS and key not in usage_from_stdin:
                    usage_from_stdin[key] = value
                    if key.startswith("seven_day_") or key == "extra_usage":
                        has_model_caps = True
            if not plan_from_cache:
                plan_from_cache = (_stale or {}).get("plan", "") or ""

        # Only reach for the API when stdin left us with nothing to draw, or
        # when the model-scoped caps we do have are older than
        # _MODEL_CAPS_TTL.
        #
        # Claude Code puts only five_hour and seven_day on stdin. A Fable
        # weekly cap arrives from the API solely inside the `limits[]` array
        # (as {"kind": "weekly_scoped", "scope": {"model": {"display_name":
        # "Fable"}}}), which _normalize_usage folds into seven_day_fable.
        # Because has_core was already true from stdin, the old guard skipped
        # the fetch forever and the Fable bar could never appear — the
        # widget was enabled and simply starved of data. Same starvation gave
        # "Extra: n/a" and the "check failed" credits line.
        has_core = "five_hour" in usage_from_stdin or "seven_day" in usage_from_stdin
        # The stale cache is untrusted input. A time field it cannot use (text,
        # an int too big for a float, Infinity, NaN) counts as absent: caps
        # stale, no backoff active, no recent attempt. A raise here is a blank
        # bar on every repaint, and Infinity would freeze the caps for good.
        _caps_age = time.time() - (_cache_seconds((_stale or {}).get("model_caps_fetched_at")) or 0)
        _caps_stale = _caps_age > _model_caps_ttl(config)
        # Only refresh if a widget that consumes this data is actually on
        # screen. The per-model bars are opt-in (DEFAULT_SHOW has them off)
        # precisely because this is a network call: a user who never turned
        # them on must never pay for it, and Claude Pro would get null windows
        # back anyway — the per-repaint fetch #47 removed, just slower.
        _caps_show = config.get("show", {})
        _caps_wanted = any(_caps_show.get(k, DEFAULT_SHOW.get(k, False))
                           for k in ("opus", "sonnet", "fable", "extra"))
        # Extra usage also draws itself without being switched on, as soon as
        # the account has credits (and unless --hide extra). Left out of this
        # test, its figure was never refetched: the first one fetched stayed
        # on the bar for months (F17).
        if not _caps_wanted and not config.get("extra_hidden", False):
            _extra_seen = _extra_usage_view(usage_from_stdin.get("extra_usage"))
            _caps_wanted = _extra_seen is not None and _extra_seen["limit"] > 0
        # Preserve across the end-of-branch write_cache below. Without this the
        # final write drops the field, the age reads as 0 on the next repaint,
        # and the "stale" test fires every single draw — a fetch per repaint,
        # which is what the 5-minute TTL exists to prevent.
        _caps_ts = (_stale or {}).get("model_caps_fetched_at") or None
        _caps_attempted = (_stale or {}).get("model_caps_attempted_at") or None
        # Same reason: the end-of-branch write must not erase 429 backoff.
        _rl_until = (_stale or {}).get("rate_limited_until") or None
        _rl_fails = (_stale or {}).get("rate_limit_fails") or None
        # An unusable stamp is not carried, though: written back, it would
        # crash or freeze every later repaint the same way. A usable one is
        # carried exactly as it was written.
        if _cache_seconds(_caps_ts) is None:
            _caps_ts = None
        if _cache_seconds(_caps_attempted) is None:
            _caps_attempted = None
        if _cache_seconds(_rl_until) is None:
            _rl_until = None
        _blocked_until = _cache_seconds(_rl_until) or 0
        _backoff_active = time.time() < _blocked_until
        _recent_attempt = (time.time() - (_cache_seconds(_caps_attempted) or 0)) < _MODEL_CAPS_RETRY
        # A spend limit on stdin means a Claude apps gateway: no claude.ai
        # login, so the usage API is never asked (F08).
        if not _gateway_run and ((not has_core and not has_model_caps and "extra_usage" not in usage_from_stdin) or \
                (_caps_wanted and _caps_stale and not _backoff_active and not _recent_attempt)):
            try:
                # The kill guard is asked first. When it says no (this
                # account's last fetch was cut short, or another window of it
                # is mid-fetch) nothing is sent, so nothing is recorded either:
                # the next repaint asks again instead of waiting out a
                # _MODEL_CAPS_RETRY it never used.
                if _begin_net_attempt("usage", _USAGE_KILL_GUARD, primary=True):
                    try:
                        # Stamp the attempt before the request so a run that
                        # Claude Code cancels mid-flight (or one that times
                        # out, or finds no token) still holds the next attempt
                        # off for _MODEL_CAPS_RETRY seconds.
                        _caps_attempted = time.time()
                        write_cache(cache_path, "", usage=usage_from_stdin,
                                    plan=plan_from_cache,
                                    model_caps_fetched_at=_caps_ts,
                                    rate_limited_until=_rl_until,
                                    rate_limit_fails=_rl_fails,
                                    model_caps_attempted_at=_caps_attempted)
                        token, api_plan = get_credentials()
                        if token:
                            api_usage = fetch_usage(token, timeout=_MODEL_CAPS_TIMEOUT)
                    except Exception:
                        _end_net_attempt("usage")
                        raise
                    _end_net_attempt("usage")
                    if token:
                        for key, value in api_usage.items():
                            # Model caps refresh in place: an expired
                            # carried-over value must be replaced, not kept.
                            # five_hour and seven_day still come from stdin
                            # and are left alone.
                            if key not in _USAGE_CACHE_KEYS:
                                continue
                            if key in ("five_hour", "seven_day") and key in usage_from_stdin:
                                continue
                            usage_from_stdin[key] = value
                        if api_plan:
                            plan_from_cache = api_plan
                        _caps_ts = time.time()
                        _rl_until = None   # success clears the backoff
                        _rl_fails = None
                        write_cache(cache_path, "", usage=usage_from_stdin,
                                    plan=plan_from_cache,
                                    model_caps_fetched_at=_caps_ts,
                                    model_caps_attempted_at=_caps_attempted)
            except urllib.error.HTTPError as exc:
                # A 429 here must set the shared backoff. Without it this path
                # retried on every repaint: each draw saw no timestamp, judged
                # the caps stale, and called again — which is exactly how a
                # burst of repaints earns a multi-thousand-second retry-after
                # from the endpoint. Honour Retry-After when the server sends
                # a usable one (capped, see _retry_after_seconds), else fall
                # back to the existing exponential schedule.
                if getattr(exc, "code", None) == 429:
                    _ra = _retry_after_seconds(getattr(exc, "headers", None))
                    _delay, _fails = _rate_limit_backoff(
                        (_stale or {}).get("rate_limit_fails"))
                    _delay = max(_delay, _ra)
                    _rl_until = time.time() + _delay
                    _rl_fails = _fails
                    write_cache(cache_path, "", usage=usage_from_stdin,
                                plan=plan_from_cache,
                                model_caps_fetched_at=_caps_ts,
                                rate_limited_until=_rl_until,
                                rate_limit_fails=_rl_fails,
                                model_caps_attempted_at=_caps_attempted)
            except Exception:
                pass

        # Whatever was carried or fetched above can have run out since (F01);
        # nothing past its reset is drawn, recorded or written back to disk.
        usage_from_stdin = _roll_over_expired_windows(usage_from_stdin)
        # When the extra-usage figure was last fetched, for its age marker. Set
        # after stdin_ctx was persisted, so it only reaches the renderer (F17).
        stdin_ctx["_extra_fetched_at"] = _cache_seconds(_caps_ts) or None

        # Get plan from credentials (lightweight, no API call)
        if not plan_from_cache and not _gateway_run:
            _, plan_from_cache = get_credentials()
            plan_from_cache = plan_from_cache or ""
        # The name the plan is sold under, from the profile at most once a day
        # and only while the plan is drawn. The tier-derived name stays the
        # fallback, and what the cache records.
        if not _gateway_run:
            _refresh_plan_label(config, (_stale or {}).get("rate_limited_until"))
        shown_plan = _display_plan(plan_from_cache, config)

        # Guarded: a value in a file the renderer cannot draw must not leave a
        # blank bar. The side effects below still run, so the damage heals.
        line = _guarded_status_line(usage_from_stdin, shown_plan, config, stdin_ctx, 0)
        _update_session_state(usage_from_stdin, stdin_ctx, config)
        _append_history(usage_from_stdin)
        if stdin_ctx.get("context_pct") is not None:
            _append_context_history(stdin_ctx["context_pct"])

        # Write to cache so staleness tracking works
        write_cache(cache_path, line, usage_from_stdin, plan_from_cache,
                    model_caps_fetched_at=_caps_ts,
                    rate_limited_until=_rl_until, rate_limit_fails=_rl_fails,
                    model_caps_attempted_at=_caps_attempted)
        _seed_active_provider_cache(usage_from_stdin, config)

        line = _finish_line(line, config, stdin_ctx)
        sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))
        return

    # --- Cached data (no stdin rate limits available) ---
    if cached is not None:
        # During a 429 backoff read_cache returns the cache without checking
        # its timestamp, and float() accepts numeric text: an unusable one
        # reads as +inf (the red stale label) instead of raising here.
        cache_age = _cache_age(cached)
        if "usage" in cached:
            line = _guarded_status_line(cached["usage"], _display_plan(cached.get("plan", ""), config),
                                        config, stdin_ctx, cache_age,
                                        fallback=cached.get("line", ""))
            _update_session_state(cached["usage"], stdin_ctx, config)
        else:
            line = cached.get("line", "")
        if not isinstance(line, str) or not line.strip():
            line = _SAFE_FALLBACK_LINE   # never a blank bar
        line = _finish_line(line, config, stdin_ctx)
        sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))
        return

    # --- API fallback (first call or no stdin rate limits) ---
    token, plan = get_credentials()
    if not token:
        # No claude.ai login, but stdin has a model, context or cost to draw
        # (Bedrock, Vertex, Foundry, API key): draw them rather than tell a
        # working session to log in. The message stays for when stdin gave
        # nothing. Not cached: a cached line would be served to every window.
        if _stdin_has_session_data(stdin_ctx):
            line = _stdin_only_line(config, stdin_ctx)
            if _strip_ansi(line).strip():
                sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))
                return
        if os.environ.get("ANTHROPIC_API_KEY"):
            line = "API key detected \u2014 usage-pulse requires a Pro/Max subscription"
        else:
            line = "No credentials \u2014 run claude and /login"
        write_cache(cache_path, line)
        line = _fit_line(_append_provider_rows(line, config), config)
        sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))
        return

    # Claude Code cancels a repaint that outlives its refresh interval, and a
    # cancelled repaint can't note that its request was slow. So the stamp goes
    # down first and is lifted once there is a line to print; finding it still
    # there means the last repaint died in here — draw what we have instead of
    # walking into the same request again.
    if not _begin_net_attempt("usage", _USAGE_KILL_GUARD, primary=True):
        # The stale cache is whatever JSON object is on disk — read_cache's
        # shape checks have not run on it — and nothing below sits inside the
        # fetch error handlers, so a raise here would be an empty stdout.
        line = ""
        try:
            stale = _read_stale_cache(cache_path) or {}
            if isinstance(stale.get("usage"), dict) and stale["usage"]:
                line = build_status_line(stale["usage"], stale.get("plan", plan),
                                         config, stdin_ctx,
                                         cache_age=_cache_age(stale))
            elif isinstance(stale.get("line"), str):
                line = stale["line"]
        except Exception:
            line = ""
        if not line:
            line = "Usage unavailable \u2014 retrying shortly"
        line = _finish_line(line, config, stdin_ctx)
        sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))
        return

    try:
        usage = fetch_usage(token)
        _refresh_plan_label(config, token=token)
        line = build_status_line(usage, _display_plan(plan, config), config, stdin_ctx, cache_age=0)
    except urllib.error.HTTPError as e:
        usage = None
        if e.code == 401:
            new_token, plan = refresh_and_retry(plan)
            if new_token:
                try:
                    usage = fetch_usage(new_token)
                    line = build_status_line(usage, plan, config, stdin_ctx, cache_age=0)
                except Exception:
                    usage = None
                    line = "Token refresh failed \u2014 restart Claude to re-login"
            else:
                line = "Token expired \u2014 restart Claude to refresh"
        elif e.code == 403:
            line = "Access denied \u2014 check your subscription"
        elif e.code == 429:
            # Exponential backoff, persisted so the *next* invocation also
            # holds off. Previously a 429 with usable stale data wrote nothing
            # back, so every refresh retried immediately and kept the limit lit.
            stale = _read_stale_cache(cache_path)
            delay, fails = _rate_limit_backoff((stale or {}).get("rate_limit_fails"))
            retry_at = time.time() + delay

            stale_usage = stale.get("usage") if stale else None
            line = None
            if isinstance(stale_usage, dict) and stale_usage:
                # This runs before the backoff is written, so a raise here
                # also lost the backoff and the next repaint called the API
                # again. A value the renderer cannot draw (the file is
                # untrusted) falls through to the message below, whose
                # rewrite drops it. An unusable, missing or zero timestamp
                # reads as long stale here, and the rewrite records it as
                # _UNKNOWN_DATA_TIME so it stays stale on later repaints.
                stale_age = _cache_age(stale)
                stale_ts = stale.get("timestamp")
                if not _cache_seconds(stale_ts):
                    stale_ts = _UNKNOWN_DATA_TIME
                try:
                    line = build_status_line(
                        stale_usage, stale.get("plan", plan), config, stdin_ctx,
                        cache_age=stale_age,
                    )
                except Exception:
                    line = None
            if line is not None:
                usage = stale_usage
                write_cache(
                    cache_path, line, usage, stale.get("plan", plan),
                    rate_limited_until=retry_at, rate_limit_fails=fails,
                    data_timestamp=stale_ts,
                )
            else:
                mins = max(1, int(delay // 60))
                line = f"Rate limited \u2014 retrying in {mins} min"
                write_cache(
                    cache_path, line,
                    rate_limited_until=retry_at, rate_limit_fails=fails,
                )
            _end_net_attempt("usage")
            line = _fit_line(_append_provider_rows(line, config), config)
            sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))
            return
        else:
            line = f"API error: {e.code}"
    except urllib.error.URLError as e:
        usage = None
        reason = getattr(e, "reason", None)
        is_ssl = False
        try:
            import ssl
            is_ssl = isinstance(reason, ssl.SSLCertVerificationError)
        except (ImportError, AttributeError):
            is_ssl = reason and "CERTIFICATE_VERIFY_FAILED" in str(reason)
        if is_ssl:
            if sys.platform == "darwin":
                line = "SSL cert error \u2014 run: /Applications/Python*/Install\\ Certificates.command"
            else:
                line = "SSL cert error \u2014 check Python SSL certificates"
        else:
            line = "Network error \u2014 retrying next refresh"
    except json.JSONDecodeError:
        usage = None
        line = "API returned invalid data"
    except (TypeError, ValueError):
        usage = None
        line = "Data error"
    except Exception as e:
        usage = None
        line = f"Usage unavailable: {type(e).__name__}"
    _end_net_attempt("usage")

    if usage is not None:
        write_cache(cache_path, line, usage, plan)
        _append_history(usage)
        _update_heatmap(usage)
        _update_session_state(usage, stdin_ctx, config)
        if stdin_ctx and stdin_ctx.get("context_pct") is not None:
            _append_context_history(stdin_ctx["context_pct"])
        try:
            stats, milestone = _update_stats()
            if milestone:
                line = line + f" {BRIGHT_YELLOW}{milestone}{RESET}"
        except Exception:
            pass
    else:
        # Cache error lines so we don't hammer the API on every refresh
        write_cache(cache_path, line)
    line = _finish_line(line, config, stdin_ctx)
    sys.stdout.buffer.write((line + RESET + "\n").encode("utf-8", "replace"))


def _handle_fatal_error(exc):
    """What to do when main() raises: the ``__main__`` block calls this.

    Claude Code runs the status line, the agent-panel rows and the hooks with
    stdin piped and nobody watching, and none of them may crash it with a
    traceback. So there the error is swallowed: the status line prints a clean
    blank line, the rows and hooks print nothing (a hook's stdout is read as
    context), and the process exits 0.

    A command a person types (``--theme``, ``--install``, ``--stats`` ...) must
    not hide a failure: it used to print a blank line and exit 0, so a config
    that could not be saved looked saved. It says what went wrong on stderr and
    exits 1. So does the bare script run at a terminal.
    """
    args = sys.argv[1:]
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    status_line_mode = not args
    run_by_claude_code = status_line_mode or args[0] in {
        row[0] for row in CLI_FLAGS if row[2] == "Internal"}
    if run_by_claude_code and not interactive:
        if status_line_mode:
            try:
                sys.stdout.buffer.write((RESET + "\n").encode("utf-8", "replace"))
            except Exception:
                pass
        return
    message = f"usage-pulse: {type(exc).__name__}: {exc}\n"
    try:
        sys.stderr.write(message)
    except (UnicodeError, OSError, ValueError):
        try:
            sys.stderr.write(message.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        _handle_fatal_error(exc)
