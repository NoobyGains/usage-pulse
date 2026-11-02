# Changelog

All notable changes to claude-pulse are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Ideas, not promises:

- Antigravity as a host, not just a provider row.
- A detached background refresh for other Claude accounts, so a repaint never waits on the network.
- Native Kimi and GLM rows, once their usage endpoints are confirmed.
- Reading `~/.claude/tasks/<team>/` so the team widget can show task subjects.
- Live Codex limits through its app-server instead of the last session file.
- A `skills/` layout for the plugin.

## [3.6.1] - 2026-11-02

### Fixed

- **Arrow keys no longer cancel `--setup` on Linux, macOS and WSL** ([#56](https://github.com/NoobyGains/usage-pulse/issues/56), reported by [@MarcusSnowTaxConnex](https://github.com/MarcusSnowTaxConnex)). The key reader took its first byte from the buffered `sys.stdin`, which swallowed the rest of the `ESC [ B` sequence, so the check for a following byte found nothing and every arrow came back as Esc. Keys are now read as raw bytes from the terminal. `ESC O` arrows (application-cursor mode) and accented letters typed into text fields work too.
- Keys pressed while a wizard screen was being redrawn are no longer thrown away. Switching the terminal to raw mode used to flush pending input, which dropped fast typing and held-down arrows.

## [3.6.0] - 2026-10-21

### Added

- **Pace arrows.** With the `pace` widget on, each window shows an arrow after its percentage instead of a bare number: `↑` when usage is ahead of the window's clock, `↓` when it is behind, doubled (`⇈` in red, `⇊` in green) once the gap passes twenty points, and a dim `→` when it is on track. `--pace-style percent` brings back the elapsed-time percentage. The idea started in [@marcelosanchez](https://github.com/marcelosanchez)'s fork.
- **Bars you can switch off one at a time.** `--hide session_bar`, `weekly_bar` or `context_bar` keeps that window's percentage and timer and drops the bar itself, for a narrower line. The idea started in [@lucachitayat](https://github.com/lucachitayat)'s fork.
- `--bar-size auto`, the shrink-to-fit behaviour described below, for anyone who wants bars that narrow with the window.

### Fixed

- **Bars no longer shrink on their own.** The status line narrowed its bars whenever the whole line would not fit the terminal, down to two characters, and 3.4.0's use of the `COLUMNS` value Claude Code passes switched that on for Windows: in a window about a hundred columns wide, `large` bars collapsed to `━─`. A named bar size is now exactly that wide. A line that does not fit is truncated at `--max-width` or wrapped with `--wrap auto`, as documented; the old behaviour is `--bar-size auto`.
- Terminal-width detection no longer raises on Windows when `COLUMNS` is unset (`os.O_NOCTTY` does not exist there), and when no width can be detected on any platform the line is now left alone instead of being cut at a guessed 120 columns: Claude Code always passes `COLUMNS`, and any other host clips at its own edge.

## [3.5.0] - 2026-10-21

### Changed

- **Renamed to usage-pulse.** Anthropic's plugin directory reserves names that start with `claude-`, and a different "Claude Pulse" is already listed there. The plugin, the marketplace, the slash-command namespace (`/usage-pulse:setup`, `/usage-pulse:pulse`), the GitHub repository (`NoobyGains/usage-pulse`, the old address redirects) and every name the script prints are now usage-pulse. The script file is still `claude_status.py`, the `/pulse` command keeps its name, and the state folder does not move, so nothing installed stops working.
- **Existing installs keep working.** A git or one-liner install in `~/.claude-pulse` keeps updating from the renamed repository, and the installers update it in place; new installs go to `~/.usage-pulse`. `USAGE_PULSE_DIR`, `USAGE_PULSE_REPO_URL` and `USAGE_PULSE_NONINTERACTIVE` are the new names of the installer variables, and the `CLAUDE_PULSE_*` names still work. Marketplace installs need a reinstall once: `/plugin uninstall claude-pulse`, `/plugin marketplace add NoobyGains/usage-pulse`, `/plugin install usage-pulse`; the new plugin's `SessionStart` hook then moves the status line over by itself.
- **The README is new**, written around animated demos rendered from the real script (`scripts/readme_assets.py`), and the repository root is tidied: one copy of the `/pulse` command (`commands/pulse.md`), the old screenshots and the old GIF generator are gone.
- **New logo.** A title-case "Usage Pulse" wordmark built from Cascadia Code outlines (`scripts/make_logo.py`), with light and dark variants chosen by GitHub's theme and a gently animated icon.

## [3.4.2] - 2026-10-21

### Fixed

- **A new session no longer inherits the previous session's context % and cost** (#55, reported by @hardikhappy). The persisted stdin context is now scoped to the session that wrote it: when the session id changes, only the account-wide fields (rate limits, Claude Code version, spend limit) carry over, so a fresh session shows nothing until its own first reply arrives, and two sessions open at once stop showing each other's numbers.
- **Marketplace installs survive plugin updates** (#54, reported by @skywalkerisnull). A plugin lives in a per-version folder that Claude Code removes a fortnight after an update, and the status line registered from the old folder died with it. The plugin now ships a `SessionStart` hook that runs `--sync-install-path`: when `settings.json` still points at an old plugin folder, the status line, the agent-panel rows, the installed hook groups and `/pulse` are re-registered from the current one. A git or manual install is never touched, and nothing happens when the path is already current.

### Changed

- **The README is more visual**: the bar with provider rows, the setup wizard, the agent panel and the layouts are shown as images rendered from the real script.

## [3.4.1] - 2026-10-21

### Fixed

- **`--update` no longer fails on GitHub's API rate limit.** The updater and the hourly update indicator look up the latest commit through the GitHub API, which allows 60 unauthenticated requests an hour per address. On a machine where several tools poll GitHub that runs out, and `--update` stopped with "Could not reach GitHub API" although the network was fine. A failed API call now falls back to `git ls-remote`, which has no such limit.
- **The per-model cap tests no longer read the developer's own `config.json`.** They failed on any machine where the cap bars were switched on.

### Added

- **The setup wizard is tested on a real terminal.** `tests/test_setup_wizard_tty.py` runs `--setup` on a pseudo terminal (ConPTY, the console behind Windows Terminal and PowerShell), presses the keys a person would and checks the files the wizard writes. On Windows it needs `pip install pywinpty` and is skipped without it; the POSIX transport is opt-in with `CLAUDE_PULSE_TTY_TESTS=1`.

## [3.4.0] - 2026-10-21

### Added

- **Setup wizard.** `--setup` is a 14-screen guided setup with live previews: environment report, status line (it offers to replace another tool's), hook groups, providers found on this machine, per-model cap bars, theme, bar size, bar style, layout, currency (the detected one first), clock, effort format, widgets and a summary. Nothing is written until the summary is confirmed, and Esc anywhere changes nothing.
- **Unattended setup.** `--setup --non-interactive` (also used when there is no terminal, or when `CLAUDE_PULSE_NONINTERACTIVE=1`) installs the status line, the agent-panel rows and `/pulse`, and switches on detected Claude accounts, Codex and Gemini. It never enables MiniMax and installs no hooks. The plugin's `/claude-pulse:setup` runs it.
- **Installers run the wizard.** `install.sh` and `install.ps1` now finish with `--setup`: the wizard in a terminal, the defaults otherwise. `CLAUDE_PULSE_REPO_URL` overrides the clone source.
- **`--help` and `-h`.** A grouped flag table (Setup, Look, Widgets, Providers, Hooks, Info, Internal). An unknown flag typed at a terminal now points to `--help` instead of drawing a bar. The README's flag list is checked against the same table by the tests.
- **Providers: one status row per other subscription.** `--detect-providers [--json]` finds other Claude Code accounts (every `~/.claude*` folder with a login), Codex CLI, Google Antigravity / Gemini and MiniMax. `--providers list | detect | enable | disable | label | remove | add-file` manages the rows. Rows use the main line's theme, bar style and size, with at most 6 shown and 8 configured. New config keys: `providers`, `providers_label_main` and `providers_refresh_seconds`, plus the `providers` widget.
- **File provider.** `--providers add-file <id> <path> [label]` adds a row read from a JSON file that any tool can write, for services claude-pulse cannot read itself. A `rate_limits` wrapper and `utilization` are accepted, `resets_at` may be epoch seconds, epoch milliseconds or ISO 8601, and either window may be missing.
- **Multiple Claude accounts.** State for a non-default `CLAUDE_CONFIG_DIR` (usage cache, stdin context, history, sessions, heatmap, cost caches) now lives in `<state folder>/accounts/<name>-<hash>/`, so two accounts' windows stop overwriting each other. Each window seeds its own account's numbers from stdin, so with both open no API call is needed for the other account's row.
- **Hook groups.** `--install-hooks --only heartbeat,agents,team` installs groups separately, and unticking a group in the wizard removes it.
- **Agent-team widget.** `team 3 · tasks 4/7` shows teammates seen and shared-task progress from the `TeammateIdle`, `TaskCreated` and `TaskCompleted` hooks (`--install-hooks`). It needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, and task counts need `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` on newer models. The README lists its limitations.
- **Four zero-cost widgets from stdin.** `cache_warm` (prompt-cache warmth such as `♨ 92% · 3m`, off by default), `spend_limit` (`spend $12.40/$50 wk`, shown only when a Claude apps gateway reports one), `repo` (`owner/name`, off by default) and `session_name` (the `/rename` name, off by default). Agent-panel rows fall back to the subagent's type when it has no name, and terminal width detection prefers the `COLUMNS` variable Claude Code sets.
- **New models.** Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5 are in the model, context-window and pricing tables, including Haiku 5.5's higher rate above 100K prompt tokens.
- **Settings backups.** Before claude-pulse first writes `settings.json`, it copies the file to `settings-backups/` in the state folder, keeping the newest five per config folder.
- **`/pulse` routing.** `/pulse` now opens a menu (Setup, Look, Widgets, Providers, Hooks, Info) and routes `/pulse setup`, `/pulse providers`, `/pulse hooks`, `/pulse <theme>` and the rest straight to the right place.

### Changed

- **The README is rebuilt** around the wizard, providers, a widget table and a full flag list, and the "What's new" section moved here.
- **Per-model weekly cap bars (Opus, Sonnet, Fable) are opt-in and come from the usage API.** Claude Code's stdin carries only the 5-hour and 7-day windows, so these bars cost one API call per refresh. They are off until you run `--show fable` (or tick them in the wizard), refresh every `model_caps_refresh_seconds` (300 by default, 60 at the lowest), and a failed attempt is not retried for 60 seconds. Contributed by @thedanielmay in #53, then made opt-in and throttled.
- **The `SubagentStop` hook is pinned to exit 0**, like the three team hooks. Exit code 2 keeps a subagent running, and Python exits 2 by itself when the script path has gone missing.
- **The README's example uses Opus 5.5**, and the version check compares the top changelog heading to the script's version.

### Fixed

- **`--install-hooks` no longer replaces an invalid `settings.json`.** It refuses, says why, and exits 1.
- **Per-model cap refreshes no longer repeat on every repaint.** Claude Code cancels an in-flight status-line run when a newer repaint arrives, which left no timestamp; the attempt is now stamped before the request.
- **The cap tests no longer touch your real state.** They ran against the real `cache.json` and `settings.json`; they now use throwaway folders.
- **Two windows cannot double-fetch one account.** A per-account lock file makes the first window that repaints do the usage-API call; the other keeps its cached numbers. Concurrent cache writes use a unique temp file per writer.
- **Removing or reinstalling a hook group keeps foreign commands** that share a settings.json entry with ours; only our command is replaced.
- **A Python path under a home folder with spaces** is quoted in the installed commands, so `$HOME` expands safely.
- **The Codex row notices updates from an older, still-running session**, not only from the newest session file.

### Security

- **Other accounts' tokens are read only.** A provider row reads that account's token from its `.credentials.json` and never refreshes, stores or logs it. Refreshing would rotate the refresh token and sign that account out.
- **No e-mail addresses or account names are read** when detecting providers. Labels come from your config or the folder name.
- **Provider labels and JSON file contents are sanitised** against ANSI escape injection, and provider files are capped at 1 MB.
- **A copy of `settings.json` is kept before every write**, so a bad install or a wrong `CLAUDE_CONFIG_DIR` can be undone by hand.

## [3.3.0] - 2026-08-21

- **Sonnet 5 priced at its now-permanent $2/$10 rate** — Anthropic cancelled the scheduled September increase to $3/$15. Historical turns are priced by their own date either way, so a promo lapsing can never retroactively inflate the cost widgets.
- **GitLab merge requests render as `!N`** via the `pr.kind` stdin field (Claude Code 2.1.234+); GitHub PRs keep `#N`.
- **MiniMax quota works on Windows** — npm installs `mmx` as a `.cmd` shim that `subprocess` won't auto-resolve; the call now goes through a PATH lookup (thanks @PFalko, #50).
- **The repaint timer heals itself** — a focus timer expiring on its own or a heartbeat going stale no longer leaves the 15s timer armed, and a heartbeat waking from idle arms it immediately.
- **Self-hosted star chart** — GitHub restricted the stargazers API to repo collaborators, so the README chart is rendered weekly by a workflow with the repo's own token and committed as static SVGs.
- **Fable weekly cap** — read from the model-scoped `limits` the API reports, so new models are picked up without a code change.
- **Claude 5-era stdin fields** — reasoning effort, `⚡fast` mode, thinking state, active subagent, PR badge, and cache-hit ratio.
- **Fixed: the refresh timer never worked.** Every version up to 3.1.0 wrote a `refresh` key that Claude Code does not have, so it was silently ignored — animations, the heartbeat and the focus countdown froze whenever the session went idle. The real setting is `refreshInterval`, and it is now set only when something time-based is on screen.
- **~40% faster repaints** (204ms → 118ms): `claude --version` and `git rev-parse` were both running on *every* repaint just to validate hourly caches.
- **Per-model bars no longer invent data** — Claude Pro reports `null` for the model-scoped windows, and 3.1.0 drew a hardcoded `Sonnet 0%` bar in that case.
- **Zero API calls, for real** — the per-model caps arrive on stdin, but 3.1.0 discarded them and re-fetched over OAuth. That call is gone.
- **Two-line layout**, rolling **7-day cost** widget, `CLAUDE_CONFIG_DIR` support, exponential backoff on 429, and an update check that no longer nags forks forever.
- **Corrupt state can't blank the bar** — a truncated or hand-edited cache/settings file used to raise on the hot path. Every state read now degrades to a cache miss instead, and one malformed rate-limit field no longer discards the windows after it.
- **Subagent panel rows** via `subagentStatusLine`, a live agent counter from the `SubagentStart`/`SubagentStop` hooks, and an opt-in budget bar.
- **The heartbeat is now opt-in** — the `[/] 42 tools 5m` spinner is noise for most people, needs a hook, and kept the repaint timer alive. `--show heartbeat` brings it back.
- **Peak hours removed.**

Python floor is **3.8** — 3.1.0 advertised 3.6+ but did not parse below 3.12. Test suite: 21 → 204.
