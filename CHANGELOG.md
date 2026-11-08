# Changelog

All notable changes to claude-pulse are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Ideas, not promises:

- Antigravity as a host, not just a provider row.
- A detached background refresh for other Claude accounts, so a repaint never waits on the network.
- Native Kimi and GLM rows, once their usage endpoints are confirmed.
- Reading `~/.claude/tasks/<team>/` so the team widget can show task subjects.
- A `skills/` layout for the plugin.

## [3.8.0] - 2026-11-08

### Added

- **The Codex row reads the live pool.** With a ChatGPT login in `~/.codex/auth.json`, the row asks `chatgpt.com/backend-api/wham/usage`, the endpoint the Codex CLI itself uses, at most once every `providers_refresh_seconds` and never more than once a minute, with the same 429 / 401 / error back-off as the other Claude accounts. Codex limits are shared with ChatGPT on the web and with every other machine on the account, so the session files alone fell behind whenever Codex was used elsewhere; they remain the fallback when the token has expired or the call fails. The token is sent to `chatgpt.com` only, with every redirect refused, and is never refreshed, stored or logged. `--detect-providers` finds a fresh `codex login` before its first session file exists, and a Codex row older than 30 minutes is drawn stale like the Claude rows.
- **Pace arrows on provider rows.** With the `pace` widget on, every provider row's window gets the same arrow (or elapsed share with `--pace-style percent`) as the main line. Stale rows show none.

### Fixed

- **The plan is named as it is sold.** The credentials file only carries the rate-limit tier, so an Ultra account read `Max 20x`. With the `plan` widget or provider tiers on, the name now comes from the OAuth profile's `plan_display_name` (`Ultra`, `Max`, `Pro`), asked once a day with the attempt stamped first and a hard time ceiling; the tier stays the fallback, and nothing else in that reply is read.
- **An account without a 5-hour window gets no 0% session bar.** The usage API reports `five_hour: null` for such accounts. The bar now shows the weekly window alone instead of a 0% session placeholder, and the session-state writer treats the window as absent instead of raising, as the history and heatmap writers have since 3.7.0.

## [3.7.0] - 2026-11-03

### Added

- **`--uninstall`** takes usage-pulse out of Claude Code's settings: the status line, the agent-panel rows, every usage-pulse hook and the `/pulse` command. It backs up `settings.json` first and leaves other tools' entries alone. Before, deleting the script left hooks that made Claude Code report an error after every tool call.
- **`↻ Restart Claude`** replaces `↑ Claude Update` when the newer Claude Code is already installed on disk and only a restart applies it. `--config` says to restart in that case too.
- `--currency` takes any ISO code the rate service lists (`DKK`, `NOK`, `SEK` and so on), not just a symbol. A bare `kr` follows your machine's country: Danish kroner in Denmark, Norwegian in Norway, Swedish elsewhere.

### Changed

- **`--wrap auto` now uses up to four rows** instead of two, and it works with the rainbow theme and rainbow animation, which used to be cut instead. A line that still does not fit loses whole widgets from the end and finishes with a dim `…`, where it used to be cut in the middle of a word or a bar.
- **The 7-day and all-time cost widgets are refreshed by a background process.** They show the last figure meanwhile and appear a few seconds after you first switch them on. The first redraw after updating shows no figure until that scan finishes.
- `--bar-size auto` measures the real line, including the staleness label and plan name, so shrink-to-fit actually fits.
- A currency with no rate source is shown in dollars instead of a dollar figure under that currency's symbol.

### Fixed

- **The bar no longer goes blank over a damaged file.** A wrong value in `cache.json`, a credentials file with `"rateLimitTier": null`, or a damaged `hook_state.json`, history, stats, animation or heatmap file used to blank the bar on every redraw, some of them for good. Each now reads as empty and is rewritten. Cache, history, stats and the other shared state files are written atomically, so two windows, or a redraw Claude Code cancels, can no longer leave an empty file that wipes your history or the rate-limit backoff.
- **Accented and non-Latin names no longer come out garbled or blank the bar on Windows.** Claude Code's data is read as UTF-8 whatever the code page, so model, branch and session names show correctly, git branch names too, and a stray invalid character becomes `?` instead of emptying the line.
- **A hand-edited `config.json` is read properly.** One saved with a byte-order mark (Notepad's "UTF-8 with BOM") was silently ignored, and one saved as UTF-16 or ANSI blanked the bar. UTF-8, UTF-8 with BOM and UTF-16 work; a file that cannot be decoded falls back to the defaults.
- **A window that has reset no longer keeps its old percentage.** After the 5-hour or weekly reset the bar showed the old figure next to `now`, also in new sessions. It now reads 0%.
- **The 7-day, all-time and `--stats` costs were about twice too high.** Duplicate transcript lines were counted twice and 1-hour cache writes were priced as 5-minute ones. Each response is now counted once and cache writes are priced by how long they last.
- **After `/compact` the context bar clears** instead of keeping the old value, and tokens mode no longer shows `0/1M` next to a 92% bar.
- **Each window shows its own numbers.** The branch, changed-file count and ahead/behind count are kept per repo folder, so a window no longer shows another repo's branch. The heartbeat shows this session's tool count and time instead of lifetime totals (`4001 tools 72h`) and stays empty until the session's first tool call. `⚠ Context ↑fast` no longer fires because a second window of the same account has a lower context.
- **Extra-usage credits stay current.** They refresh with the per-model caps, are converted to your currency, and show `(stale)` or `(3d ago)` when the figure is old.
- **The weekly reset keeps its minutes** in the 12-hour clock: `Fri 10:30am`, not `Fri 10am`.
- **Danish and Norwegian costs use the right krone.** They were converted at the Swedish rate, and `DKK` and `NOK` could not be chosen.
- **Claude apps gateway and no-login users get their bar.** They saw `No credentials — run claude and /login` instead of the spend bar, model, context and cost that Claude Code already sends.
- `+N sessions` no longer counts a window's own redraws, and Windows no longer collects a new marker file for every redraw; markers are only written while the widget is on.
- **A slow git no longer freezes the bar.** `git_drift` and `files_changed` note the attempt before running git, give git at most 1.5 seconds and keep showing the last answer; ahead/behind takes one git call instead of two. Plugin installs and folders that are not git installs no longer start git on every redraw.
- **MiniMax and a second Claude account can no longer stall a redraw.** `mmx quota show` runs at most every two minutes and, on Windows, its 3-second timeout really does stop it. A second account's usage fetch waits for its own redraw instead of running beside the main fetch, and gives up after 1.5 seconds.
- **The staleness label and the update arrows survive a narrow window.** `(5m ago)` sits next to the numbers it describes and is dropped last, and the arrows have room reserved instead of being cut first.
- Wide characters (CJK, emoji) are measured by the terminal cells they take, and zero-width ones are no longer counted, so lines and agent-panel rows fit. The rainbow theme no longer corrupts clickable links such as the pull request badge.
- **Windows: the bar appears on machines without Git for Windows.** The installed command was a PowerShell syntax error there. It now runs unchanged under Git Bash, PowerShell 7 and Windows PowerShell 5.1, and no longer depends on `$HOME`, which Git Bash and Windows disagree about. **If your bar never appeared for this reason, run the installer or `--setup` once more after updating:** existing commands are not rewritten automatically.
- The plugin's `SessionStart` hook works without Git Bash, so a plugin update re-points the status line on every Windows machine. Both installers skip the Microsoft Store Python stub and use the first Python 3.8 or newer that runs. On Windows the command prefers the `py -3` launcher, so removing a venv or upgrading Python does not blank the bar.
- **Every hook command ends in `; exit 0`,** the heartbeat and agent-start hooks too, so a moved or deleted install never makes Claude Code report a hook error after each tool call. The first redraw after a fresh install no longer deletes the heartbeat hook that setup just installed.
- **Settings writes are safer.** On Windows they retry when another process has the file open (`WinError 5`), which stops `--setup` failing at random. A symlinked `settings.json` stays a symlink, non-ASCII text is no longer turned into `\u` escapes, and a `padding` you set on the status line survives a reinstall. A command that fails, such as one that cannot save the config, now says so and exits 1 instead of printing a blank line and looking like success.
- **`--update` no longer wipes your edits.** It refuses a side branch, a detached checkout or uncommitted changes, only fast-forwards, and reports `ahead of main` for a checkout that already has the newest commit. It also refreshes the installed `/pulse` command.
- **Claude Code update arrow is right.** It is shown only when the version on your release channel is newer than the running one (a newer build used to be told to update), it follows `autoUpdatesChannel` (`stable` or `latest`), and one cached request covers both channels.
- Contributors: the tests no longer depend on your terminal width, no longer call the real usage API or read your real config when `CLAUDE_CODE_OAUTH_TOKEN` or your macOS login is present, work with `USAGE_PULSE_NONINTERACTIVE` exported, and leave no `settings.json` backups in your state folder.

## [3.6.2] - 2026-11-03

### Fixed

- **The status line no longer vanishes on slow networks or networks that drop IPv6.** Claude Code cancels a status-line script when the next refresh fires, and usage-pulse ran its network checks before it printed the bar. On a network that advertises IPv6 but drops it, every address waited out its own timeout, so one check could take over 30 seconds: each run was cancelled, nothing was cached, and the bar never drew. The bar is now printed from cache first and at most one optional network check runs per redraw. Each fetch, DNS lookup included, has a hard ceiling of 1.5 seconds, and IPv6 and IPv4 addresses are tried alternately (RFC 8305) so a dead family costs one short attempt. An attempt is recorded before it starts, so a cancelled run is not retried straight away.
- A rate-limit reply (HTTP 429) from the usage API always records its backoff, and `Retry-After` is read as plain seconds, capped at 6 hours.
- A damaged cache file (unreadable bytes, timestamps such as Infinity or NaN, very deep or very long JSON) counts as a cache miss instead of crashing the redraw or looking fresh forever.
- A damaged `exchange_rate.json` can no longer blank the bar or print a nonsense cost. A rate too big for a float used to raise an uncaught error on every redraw, and NaN, Infinity, zero, negative or non-numeric rates were used as they were (showing `£nan` or a negative cost). Such a rate, from the cache or from the rate service, is now ignored: the bar fetches a fresh rate or uses the built-in one. A damaged or future timestamp no longer keeps an old rate "fresh" forever.
- HTTP error responses are closed, and the OAuth token is only ever sent over https.
- `tests/test_rate_limit_backoff.py` no longer writes to your real `~/.claude/settings.json` when you run it.

### Removed

- **The `activity` widget (`⚡ Active`).** Its tool-call count was only updated when the next tool ran and never aged on screen, so after any burst of calls it stayed lit for up to five minutes, which in a working session meant nearly always. The heartbeat spinner already shows live tool activity. A saved `activity` setting is dropped the next time your config loads, and `--show activity` now reports an unknown part.

## [3.6.1] - 2026-11-02

### Fixed

- **Arrow keys no longer cancel `--setup` on Linux, macOS and WSL** ([#56](https://github.com/NoobyGains/usage-pulse/issues/56), reported by [@MarcusSnowTaxConnex](https://github.com/MarcusSnowTaxConnex)). The key reader took its first byte from the buffered `sys.stdin`, which swallowed the rest of the `ESC [ B` sequence, so the check for a following byte found nothing and every arrow came back as Esc. Keys are now read as raw bytes from the terminal. `ESC O` arrows (application-cursor mode) and accented letters typed into text fields work too.
- Keys pressed while a wizard screen was being redrawn are no longer thrown away. Switching the terminal to raw mode used to flush pending input, which dropped fast typing and held-down arrows.
- Plugin-cache path detection (used by `--sync-install-path`) now splits on both `/` and `\`, so a Windows path is recognised wherever the check runs. Before, `tests/test_sync_install_path.py` failed on Linux and macOS.

## [3.6.0] - 2026-10-21

### Added

- **Pace arrows.** With the `pace` widget on, each window shows an arrow after its percentage instead of a bare number: `↑` when usage is ahead of the window's clock, `↓` when it is behind, doubled (`⇈` in red, `⇊` in green) once the gap passes twenty points, and a dim `→` when it is on track. `--pace-style percent` brings back the elapsed-time percentage.
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
