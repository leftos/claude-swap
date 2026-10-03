# claude-swap — architecture

claude-swap is a single Python package (`src/claude_swap/`, installed as `cswap` and `claude-swap`) that switches Claude Code between stored accounts, watches their usage, and can switch for you before a rate limit. Layers: OS and storage primitives (`paths`, `fsutil`, `locking`, `claude_locks`, `macos_keychain`, `credentials`) → usage and account state (`oauth`, `usage_store`, `poll_policy`, `pace`, `models`, `settings`) → orchestration (`switcher.ClaudeAccountSwitcher`, `session`, `transfer`, `mappings`) → engine (`autoswitch`) → frontends (`cli`, `tui/`, `menubar`). The rule that shapes it: `ClaudeAccountSwitcher` owns account orchestration, the frontends are thin shells over it and over `AutoSwitchEngine`, and the engine and the storage leaves never import a frontend.

[`docs/README.md`](README.md) is the docs start page (this file, the plan, the glossary); `CLAUDE.md` holds the dev commands and contributor rules, and module docstrings and `README.md` are the deep docs.

## Task Index

| Task | Files, in order | Deep doc |
|---|---|---|
| Add a CLI subcommand or flag | `src/claude_swap/cli.py` (`_SUBCOMMAND_FLAGS`, `_translate_subcommand`, the parser in `main`) → `src/claude_swap/switcher.py` (the method it calls) → `tests/test_cli.py` | `README.md` ("Other commands") |
| Add or change a setting | `src/claude_swap/settings.py` (`SETTING_SPECS`) → `cli.py` (`_config_command`) → `tests/test_settings.py`, `tests/test_config_cli.py` | `README.md` ("Configuration") |
| Change auto-switch policy or add an event | `src/claude_swap/autoswitch.py` (`AutoSwitchEngine`, `*Event` classes) → `cli.py` (`_auto_command` renders events) → `tui/autoview.py` → `tests/test_autoswitch.py` | `README.md` ("Automatic switching") |
| Change usage polling cadence or backoff | `src/claude_swap/poll_policy.py` → `src/claude_swap/usage_store.py` → `src/claude_swap/snapshot_source.py` → `tests/test_poll_policy.py`, `tests/test_usage_store.py` | `poll_policy.py` module docstring |
| Change what `list` / `status` / `switch --json` emit | `src/claude_swap/json_output.py` → `switcher.py` (`accounts_snapshot`) → `tests/test_json_output.py` | `README.md` ("JSON output for scripting") |
| Change where or how credentials are stored | `src/claude_swap/credentials.py` → `macos_keychain.py` → `paths.py` → `tests/test_credentials.py`, `tests/test_macos_keychain.py`, `tests/test_macos_keychain_contract.py` | `credentials.py` module docstring |
| Change the switch or add-account flow | `src/claude_swap/switcher.py` (`switch`, `switch_to`, `add_account`, `add_account_from_token`) → `claude_locks.py` → `models.py` (`SwitchTransaction`) → `tests/test_switcher.py`, `tests/test_swap_accounts.py` | `claude_locks.py` module docstring |
| Change session mode (`cswap run`) | `src/claude_swap/session.py` → `cli.py` (`_run_command`) → `mappings.py` for directory mapping → `tests/test_session.py`, `tests/test_mappings.py` | `README.md` ("session mode") |
| Change export, import or import-usage | `src/claude_swap/transfer.py` → `switcher.py` → `tests/test_transfer.py` | `README.md` ("Backup and migration") |
| Add a one-time data migration | `src/claude_swap/migrations.py` (`MIGRATIONS`, `run_migrations`) → `tests/test_migrations.py` | `migrations.py` module docstring |
| Change the TUI | `src/claude_swap/tui/app.py` → `tui/dashboard.py` or `tui/autoview.py` → `tui/widgets.py`, `tui/theme.py`, `tui/cswap.tcss` → `tests/test_tui.py`, `tests/test_theme.py` | `tui/__init__.py` docstring |
| Change the macOS menu bar or its launchd service | `src/claude_swap/menubar.py` → `src/claude_swap/launch_agent.py` → `tests/test_menubar.py`, `tests/test_launch_agent.py` | `README.md` ("Menu bar (macOS)") |
| Change terminal colour or theme detection | `src/claude_swap/appearance.py` → `printer.py` → `tests/test_appearance.py`, `tests/test_printer.py` | `appearance.py` module docstring |

## Layers

All modules live in `src/claude_swap/`. Imports are absolute (`from claude_swap import ...`). Dependency rules below are the ones the module docstrings state.

- **`fsutil`, `locking`, `exceptions`, `logging_config`, `cache`**: OS-level leaves. `fsutil` is stated to have no `claude_swap` dependencies and holds the atomic-write helpers used by settings, credentials, mappings and session.
- **`paths`, `models`, `usage_store`**: path resolution mirroring Claude Code's own (`CLAUDE_CONFIG_DIR`, `~/.claude.json`), the dataclasses (`AccountInfo`, `AccountSnapshot`, `SwitchTransaction`, `Platform`), and the per-account usage table. `fsutil` docstring notes these three sit in an import cycle (`paths`, `models`, `usage_store`), which is why `fsutil` must stay dependency-free.
- **`macos_keychain`, `credentials`**: `CredentialStore` owns where credentials live (Keychain versus files, sticky fallback, `.enc`-wins reconciliation). Per its docstring it imports only `macos_keychain` and `paths`, never `switcher`, and must not call a switcher method through its host view.
- **`claude_locks`, `process_detection`**: cooperate with Claude Code's own advisory locks and detect running Claude Code instances, so a swap never collides with a token refresh.
- **`oauth`, `poll_policy`, `pace`, `json_output`**: token refresh and the usage API, every polling-cadence number in one place, weekly pace maths, and the schema-v1 `--json` shapes.
- **`settings`**: `settings.json` in the backup root, one `SETTING_SPECS` registry with validation and clamping.
- **`switcher`** (`ClaudeAccountSwitcher`): account orchestration and the switch, add, remove, list and status flows. Composes the modules above.
- **`session`, `transfer`, `mappings`, `migrations`, `update_check`**: session-mode profiles, export and import, directory-to-account mappings, run-once migrations, and the PyPI version check. `session`, `transfer`, `mappings` and `migrations` sit on `ClaudeAccountSwitcher` or its data.
- **`snapshot_source`**: the supported read path for dashboards and GUI shells. Runs the same on-demand pass as `cswap list`; pacing is decided by the usage store, not by the caller.
- **`autoswitch`**: `AutoSwitchEngine`, UI-agnostic per its docstring: no printing, no argparse, no TUI imports. Reports through typed events to an `on_event` callback.
- **`printer`, `appearance`, `cli`**: console output, terminal theme detection, and the `cswap` entry point (`claude_swap.cli:main` in `pyproject.toml`). `cli` lazily imports `session`, `mappings` and `autoswitch` inside the commands that need them.
- **`tui/`**: Textual app (`app`, `dashboard`, `autoview`, `widgets`, `modals`, `theme`, `data`). Consumes `accounts_snapshot`, never parses printed CLI output; blocking switcher work runs in thread workers. `textual` and `rich` are imported inside `tui.run` so plain CLI paths stay light.
- **`menubar`, `launch_agent`**: optional macOS shell over the switcher and engine; it "never re-implements account, usage, or auto-switch logic". `rumps` is the `menubar` extra and is imported lazily.

## Integration Footguns

- **Add a subcommand** → also add its flag spelling to the parser in `cli.py` `main` and its verb to `_SUBCOMMAND_FLAGS`; `_translate_subcommand` only rewrites verbs it finds there, and `run`, `auto`, `map`, `unmap`, `swap`, `move`, `alias`, `unclaimed` and `config` are pre-dispatched before the parser is built. `tests/test_cli.py` drives both spellings.
- **Change a `--json` field** → `json_output.py` (`SCHEMA_VERSION`) is imported by `transfer.py` as the schema version it accepts for `import-usage`, so a payload change also changes what `cswap import-usage` reads. Fields are additive by contract (README); `tests/test_json_output.py` and `tests/test_transfer.py` cover the two ends.
- **Add a setting** → `SETTING_SPECS` is the single registry; `_AUTOSWITCH_KEYS` is derived from it, but the dataclass fields (snake_case) and the JSON keys (camelCase) must both exist. The menu bar keeps its own separate display preferences, so an `autoswitch.*` change shows up there through `settings.py` only.
- **Add a migration** → append to `MIGRATIONS` in `migrations.py`; ids are recorded as applied, so a migration must be idempotent and return `True` only when it completed. `tests/test_migrations.py` covers the two existing ones.
- **Change the usage-store file shape** → bump `SCHEMA_VERSION` (currently 2) or `HISTORY_SCHEMA_VERSION` in `usage_store.py`; a file with a different version is read as empty.
- **Change engine events** → `cli.py` (`_auto_command` renders them as lines or JSONL), `tui/autoview.py` and `menubar.py` all consume the same stream; the `--json` event stream is additive by contract.
- **Change credential storage** → `tests/test_macos_keychain_contract.py` pins the `(service, account)` tuple the macOS backup path passes to `macos_keychain`; its real-keychain layer runs only on GitHub Actions macOS.
- **Write tests that touch account storage** → `tests/conftest.py` installs a process-global audit hook (`RealStoreWriteBlocked`) that refuses writes to the real account store, and `tests/test_real_store_guard.py` tests it. A test that builds a switcher without `temp_home` will trip it.

## Test locations

`uv run pytest` from the repo root; `pyproject.toml` sets `-n auto --dist loadgroup` (pytest-xdist), so tests must be independent and use `xdist_group` where they share state. One `tests/test_<module>.py` per source module, mostly.

- `tests/test_switcher.py`, `test_swap_accounts.py`, `test_add_account_identity.py`, `test_api_key_accounts.py`, `test_move_accounts.py`: the switcher's flows.
- `tests/test_autoswitch.py`, `test_poll_policy.py`, `test_usage_store.py`, `test_pace.py`: the engine, cadence, usage table and pace.
- `tests/test_credentials.py`, `test_macos_keychain.py`, `test_macos_keychain_contract.py`, `test_claude_locks.py`, `test_locking.py`, `test_fsutil.py`, `test_paths.py`: storage and locking layers.
- `tests/test_cli.py`, `test_config_cli.py`, `test_json_output.py`, `test_printer.py`: command surface and output.
- `tests/test_tui.py`, `test_theme.py`, `test_menubar.py`, `test_launch_agent.py`: frontends.
- `tests/test_real_store_guard.py`, `tests/conftest.py`: the isolation guard and shared fixtures (markers `no_keychain_fake`, `no_oauth_profile_fake` opt out of autouse fakes). Fixture data is in `tests/fixtures/`.

A new test goes in the file for the module it exercises; CI (`.github/workflows/ci.yml`) runs `uv run pytest` on multiple jobs including macOS.

## Deep docs

There are no subsystem docs under `docs/`. The deep detail lives in these places:

- [`README.md`](../README.md): user-facing behaviour of every command, the JSON contract, data locations.
- Module docstrings at the top of `credentials.py`, `autoswitch.py`, `usage_store.py`, `poll_policy.py`, `claude_locks.py`, `session.py`, `migrations.py` and `tui/__init__.py`: the contracts and locking protocols for each module.
