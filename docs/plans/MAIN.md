# claude-swap plan

This fork's work queue. Upstream (`realiti4/claude-swap`) issues and PRs are not triaged here: a line cites an upstream `#N` only when the work is taken on. Terms are in the [glossary](../glossary.md); the code map is [ARCHITECTURE.md](../ARCHITECTURE.md).

<!-- plan-doc-hygiene: 2026-10-03 fcbf3bf -->

## Current focus

- [ ] Take the consume lock in `_perform_switch`, so a `cswap switch` never lands the live store on a backup grant being spent (ops-tower request, OPS-82 follow-up). `_perform_switch` (`switcher.py:6702`) copies the target's backup into the live store at `switcher.py:6915` and `:7192` (`:6980` is the rollback write) without `credentials/.consume-<target>.lock`, which cswap takes only at `switcher.py:2084`, `:4285` and in `session.py` `setup_session`. A switch to account N while a consume gate's POST for N is in flight leaves Claude Code holding the refresh token being spent, so its next refresh reuses a spent grant. Take that lock before the copy to the live store, before the account lock (the gate's order).

## Next up

## Backlog
