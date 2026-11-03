---
description: Configure the usage-pulse status bar (setup, themes, widgets, providers, hooks)
argument-hint: "[setup | <theme> | show | providers | hooks | update | help]"
---
Configure your Claude status bars — setup, themes, widgets, providers, hooks and more. $ARGUMENTS

---

## 1. Find the script (once, before anything else)

1. Read `settings.json` in Claude Code's config dir (`$CLAUDE_CONFIG_DIR` if it is set, otherwise `~/.claude`). If `statusLine.command` contains `claude_status.py`, take the script path from it: strip the quotes and expand `$HOME`. Keep the interpreter at the start of that same command as PY (for example `C:/Users/me/AppData/Local/Programs/Python/Python313/python.exe` or `python3`).
2. Otherwise use the Glob tool for `**/claude_status.py` under `~/.claude/plugins/` and pick the result whose path contains `usage-pulse` (or `claude-pulse`, the name before 3.5.0).
3. Otherwise try `~/.usage-pulse/claude_status.py`, then `~/.claude-pulse/claude_status.py` (an install from before the rename).
4. If nothing is found, say: "usage-pulse isn't installed yet. Run `/usage-pulse:setup` (plugin) or the installer from the README, then `/pulse setup`." and stop.

Save the path as SCRIPT_PATH. If no PY was found, use `python` (Windows) or `python3` (macOS/Linux). Every command below is `PY "SCRIPT_PATH" <flag>`.

## 2. Rules

- After any change, say it shows on the status line's next refresh.
- Never paste raw `--config` output (it is ANSI). Summarise it in words.
- Ask with AskUserQuestion: one question per step, at most 4 options, "Other…" last when free text makes sense.
- The live theme picker and the full-screen wizard need a real terminal, which you don't have. When the user wants them, give them the line to type themselves, prefixed with `!`:
  `!PY "SCRIPT_PATH" --pick-theme` or `!PY "SCRIPT_PATH" --setup`
  If they see a static list instead, their terminal isn't interactive; carry on with the questions here.
- Hook or status-line changes need a Claude Code restart; say so when you make one.
- `PY "SCRIPT_PATH" --help` lists every flag. Use it when a request doesn't match the table below.

## 3. Routing — what to do with $ARGUMENTS

| $ARGUMENTS | Do this |
|---|---|
| empty, `menu`, `themes`, `theme` | Top menu (section 4) |
| `setup`, `wizard`, `install`, `start` | Setup path (section 5) |
| a theme name: `default` `ocean` `sunset` `mono` `neon` `pride` `frost` `ember` `candy` `rainbow` | `--theme <name>` |
| `pick`, `picker`, `pick-theme` | Give the user `!PY "SCRIPT_PATH" --pick-theme` to type. Controls: ↑/↓ or j/k move, Enter keep, Esc cancel |
| `show`, `show all`, `preview`, `colors`, `colours` | Run `--show-themes` and `--show-colors` (two Bash calls in parallel), show the output, then say only: "Press **Ctrl+O** to expand and see the colours." |
| `show <parts>` / `hide <parts>` | `--show <parts>` / `--hide <parts>` (comma-separated, see the list below) |
| `animate <off/rainbow/pulse/glow/shift/on>` | `--animate <mode>` |
| `speed <slow/normal/fast>`, `animation-speed …` | `--animation-speed <speed>` |
| `text-color <name>`, `text-colour <name>` | `--text-color <name>` |
| `bar-size <size>`, `size <size>` | `--bar-size <size>` (small, small-medium, medium, medium-large, large, or auto to shrink to fit) |
| `bar-style <name>`, `style <name>` | `--bar-style <name>` |
| `layout <name>` | `--layout <standard/compact/minimal/percent-first>` |
| `wrap <off/auto>`, `max-width <20-100>` | `--wrap …`, `--max-width …` |
| `currency <symbol>` | `--currency <symbol>` (costs are converted from USD at a daily rate) |
| `clock <12h/24h>` | `--clock-format <12h/24h>` |
| `effort-format <labeled/full/short>` | `--effort-format <value>` |
| `weekly-timer <auto/countdown/date/full>` | `--weekly-timer-format <value>` |
| `context-format <percent/tokens>` | `--context-format <value>` |
| `extra-display <auto/full/amount>` | `--extra-display <value>` |
| `streak-style <fire/text>` | `--streak-style <value>` |
| `pace-style <arrows/percent>` | `--pace-style <value>` |
| `caps`, `models`, `model caps` | Caps question (5.4), then `--show`/`--hide opus,sonnet,fable` |
| `providers`, `accounts`, `detect` | Providers path (section 6) |
| `providers label <id> <text>` | `--providers label <id> "<text>"` |
| `providers enable <ids>` / `disable <ids>` | `--providers enable <ids>` / `--providers disable <ids>` |
| `hooks` | Hooks question (5.2) |
| `hooks heartbeat`, `hooks agents`, `hooks team`, `hooks all` | `--install-hooks --only <group>` (`all` = `--install-hooks`) |
| `focus start [min]`, `focus stop`, `focus status` | `--focus <action> [minutes]` (default 25 minutes) |
| `budget <amount/off>` | `--budget <value>`. It mirrors Claude Code's `--max-budget-usd`, which can't be read automatically |
| `limits`, `limits k=v,…` | `--limits <value>` (keys: subagent_spawns, subagent_concurrent, web_searches) |
| `preset <name>`, `minimal` | `--preset <name>` (`default` is a full factory reset: confirm first) |
| `priority <widget=N,…>` | `--priority <value>` |
| `update` | `--update`, show the output |
| `stats`, `heatmap` | `--stats` / `--heatmap`, show the output |
| `config`, `settings` | `--config`, then summarise |
| `help` | `--help`, show the output |

Valid parts: session, weekly, context, timer, weekly_timer, session_bar, weekly_bar, context_bar, cost, model, branch, heartbeat, update, claude_update, opus, sonnet, fable, plan, extra, effort, fast_mode, thinking, agent, worktree, pomodoro, context_warning, staleness, lines, cumulative_cost, weekly_cost, cache, cache_warm, spend_limit, repo, session_name, providers, subagents, team, budget, pr, burn_rate, sessions, last_tool, sparkline, runway, status_message, streak, pace, git_drift, files_changed

If `--show` answers "Unknown part", the installed version predates that widget: suggest `/pulse update`.

## 4. Top menu

Run `PY "SCRIPT_PATH" --config` silently (it also reveals a pending update; mention one if shown). Then ask:

```
Question: "What would you like to set up?"
Options:
  - "Setup" — "Guided setup: status line, hooks, providers, theme, widgets"
  - "Look" — "Theme, animation, bar size and style, layout"
  - "Widgets" — "What the bar shows, model caps, budget"
  - "Other…" — "Providers, hooks, or settings/stats"
```

Setup → section 5. Look → 5.5 and 5.6, then the animation question below. Widgets → 5.8, then 5.4. For "Other…", ask: Providers (section 6) · Hooks (5.2) · Info (summarise `--config` and show `--stats`).

Animation question:
```
Question: "Choose an animation style"
Options:
  - "Off (Recommended)" — "Static theme colours"
  - "Rainbow" — "Flowing rainbow gradient"
  - "Pulse" — "Bars fade to white and back"
  - "Other…" — "glow (brighten and dim) or shift (highlight slides across)"
```
Apply with `--animate <mode>`.

Tip line to show once: **Quick commands:** `/pulse ocean` set a theme · `/pulse show` preview all · `/pulse providers` other accounts · `/pulse hooks` live agents · `/pulse config` settings

## 5. Setup path

This mirrors the terminal wizard. First offer it: "For the full-screen version with live previews, type `!PY "SCRIPT_PATH" --setup`. Or answer a few questions here." Continue here unless they switch.

**5.1 Status line.** Read settings.json. If `statusLine.command` contains `claude_status.py`, it is installed: skip to 5.2. If another command is there, ask whether to replace it and tell them what it is. Then run `PY "SCRIPT_PATH" --install` (adds statusLine, the agent-panel rows via subagentStatusLine, and /pulse).

**5.2 Hooks** (multi-select):
```
Question: "Which live widgets do you want? Each installs a hook"
Options (multi-select):
  - "Live agents (Recommended)" — "agents 3 live · 47/200 — SubagentStart/SubagentStop"
  - "Agent team (Recommended)" — "team 3 · tasks 4/7 — TeammateIdle/TaskCreated/TaskCompleted"
  - "Heartbeat" — "spinner + tool count; runs Python after every tool call"
```
Run one command with the chosen groups: `--install-hooks --only agents,team` (add `heartbeat` if picked). If heartbeat was picked, also run `--show heartbeat`. Hooks never block Claude: the events that could are pinned to exit 0. Restart needed.

**5.3 Providers.** Follow section 6.

**5.4 Per-model caps** (multi-select, all off by default):
```
Question: "Show per-model weekly cap bars?"
Options (multi-select):
  - "Opus" — "Opus weekly cap"
  - "Sonnet" — "Sonnet weekly cap"
  - "Fable" — "Fable weekly cap"
```
Explain before asking: Claude Code's status-line input only carries the 5-hour and weekly windows, so these bars cost one usage-API call every 5 minutes with your Claude Code OAuth token. Apply `--show <picked>` and `--hide <the rest>`.

**5.5 Theme.** Run `--themes-demo` and show it, or offer the live picker line. Ask in pages of 3 + "More themes…":
- Page 1: rainbow (full-spectrum flowing colours) · default (green → yellow → red) · ocean (cyan → blue → magenta)
- Page 2: frost (icy blue → steel → white) · ember (gold → hot orange → red) · candy (pink → purple → cyan)
- Page 3: neon (vivid green → yellow → red) · sunset (yellow → orange → red) · pride (violet → green → pink) · mono (white → bright white)

Apply with `--theme <name>`. Note: `--theme default` also resets every other look setting to factory values. If they already changed size or style in this session, set those again afterwards.

**5.6 Size and style.** Two questions: bar size (Large (Recommended) 12 chars · Medium 8 · Small 4 · Other… for auto, which shrinks to fit the window) → `--bar-size`; bar style (classic ━─ · block █░ · dot ●○ · Other… for shade, pipe, square, star, braille) → `--bar-style`.

**5.7 Units.** Three questions: currency ($ · £ · € · Other…) → `--currency`; clock (12h "Fri 5pm" · 24h "Fri 17:00") → `--clock-format`; effort (Effort: High (Recommended) · High · hi) → `--effort-format labeled/full/short`.

**5.8 Widgets** (two multi-select pages, all optional):
- Page 1: Cache hit rate (`cache`) · PR badge (`pr`) · 7-day cost (`weekly_cost`) · Budget (`budget`)
- Page 2: Cache warmth (`cache_warm`) · Repo badge (`repo`) · Session name (`session_name`) · Thinking state (`thinking`)

Apply with one `--show a,b,c`. If Budget was picked, ask for the amount and run `--budget <amount>`. If `--show` rejects a part, skip it and mention it needs a newer version.

**5.9 Wrap up.** Summarise what changed in one short list. Say: "Restart Claude Code to load the status line and hooks. Change anything later with /pulse."

## 6. Providers path (other accounts and tools)

Providers add one row each under the main line: another Claude account (a second `CLAUDE_CONFIG_DIR`), Codex, Gemini/Antigravity, MiniMax, or a JSON file you point at.

1. Run `PY "SCRIPT_PATH" --detect-providers --json`. It prints a JSON list of rows: `id`, `type`, `label`, `path`, `active`, `enabled`, `tier`, `age_seconds`, `note`. If the flag is unknown, the version is too old: suggest `/pulse update`.
2. If the list is empty, say what was looked for: other `~/.claude*` account dirs, Codex sessions in `~/.codex`, Antigravity's status file in `~/.gemini`, and the `mmx` CLI. Mention `--providers add-file <id> <path> [label]` for anything else that can write a `five_hour`/`seven_day` JSON file. Stop.
3. Ask a multi-select of the found rows (at most 4 per page): label as the option, `type · path` as the description. Pre-select rows already `enabled`, plus new non-`active` ones except MiniMax. Mark the `active` row as "this account, already the main line".
4. Run `--providers enable <ids>` for the newly picked and `--providers disable <ids>` for unpicked rows that were enabled.
5. Offer renames: "Want different labels?" If yes, run `--providers label <id> "<text>"` for each (24 characters max).
6. Explain: rows are read-only. Codex and Gemini rows read local files. Another Claude account's row asks api.anthropic.com with that account's own token, at most every few minutes, and never refreshes the token. MiniMax runs its `mmx` CLI, so it is only on when you pick it.

`--providers list` shows the current set.

---

Be brief and friendly.
