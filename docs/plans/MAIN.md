# claude-swap plan

This fork's work queue. Upstream (`realiti4/claude-swap`) issues and PRs are not triaged here: a line cites an upstream `#N` only when the work is taken on. Terms are in the [glossary](../glossary.md); the code map is [ARCHITECTURE.md](../ARCHITECTURE.md).

<!-- plan-doc-hygiene: 2026-10-03 fcbf3bf -->

## Current focus

## Next up

## Backlog

- [ ] Fix a stale line citation in a `_perform_switch` comment: `switcher.py:7243` says the direct activation branch is at `(:6148-6165)`, but it now starts at the `force_activate` branch near `switcher.py:6820`. Name the branch instead of citing line numbers.
