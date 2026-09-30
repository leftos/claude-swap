# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

claude-swap (`cswap`) is a Python CLI, Textual TUI and optional macOS menu bar app that switches Claude Code between stored accounts, tracks each account's usage, and can auto-switch before a rate limit. It is one package, `src/claude_swap/`, built with hatchling and managed with uv.

For structure, read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) first: its Task Index says which files to read for a change, and its Integration Footguns list the cross-file edits a change drags along. User-facing behaviour and the `--json` contract are in `README.md`.

## Commands

```bash
uv sync                                  # install deps + dev group (CI: uv sync --locked)
uv run cswap help                        # run the CLI from source; bare `uv run cswap` opens the TUI
uv run pytest                            # full suite, parallel (-n auto --dist loadgroup from pyproject)
uv run pytest tests/test_cli.py          # one file
uv run pytest tests/test_cli.py::TestName::test_name -n 0   # one test, serially (-n 0 disables xdist)
python -m build                          # sdist + wheel, as publish.yml does
```

No linter, formatter or type checker is configured (none in `pyproject.toml` or CI). CI (`.github/workflows/ci.yml`) runs `uv run pytest` on Ubuntu, Windows and macOS with Python 3.12; the macOS job adds `-o faulthandler_timeout=600`. Publishing to PyPI runs on a GitHub release.

## Rules that are easy to break

- **Python 3.12 is the floor** (`requires-python = ">=3.12"`, CI runs 3.12) even though `.python-version` pins 3.14 locally: no 3.13+-only syntax or stdlib APIs.
- **Tests must never touch the real account store, `$HOME`, Keychain or network.** `tests/conftest.py` enforces this with autouse fixtures (`_isolate_real_home`, `block_real_keychain`, `block_real_oauth_profile_fetch`) and an audit hook that raises `RealStoreWriteBlocked`. Use the `temp_home` fixture for anything that builds a `ClaudeAccountSwitcher`; opt out of a fake only with the `no_keychain_fake` / `no_oauth_profile_fake` markers.
- **Tests run in parallel under xdist**, so they must not share state. Tests marked `no_keychain_fake` are pinned to one worker automatically (`xdist_group("real-keychain")` in `pytest_collection_modifyitems`).
- **Layering**: `autoswitch` and the storage modules never import a frontend (`cli`, `tui/`, `menubar`); frontends go through `ClaudeAccountSwitcher` and `AutoSwitchEngine`. `textual`/`rich` are imported inside `tui.run` and `rumps` inside `menubar`, so plain CLI paths stay light. Imports are absolute (`from claude_swap import ...`).
- **`--json` output and the `cswap auto --json` event stream are additive contracts** (`schemaVersion` 1): add fields or event kinds, never rename or remove them. `transfer.py` reads the same schema for `import-usage`.
- **`keyring` is a Windows-only dependency** used solely by the one-time migration in `migrations.py`; don't import it on a hot path.
- **Don't rename the `macos-keychain` CI job or narrow it back to the Keychain test files**: branch protection matches that job id, and the comment in `ci.yml` records why it runs the whole suite.
