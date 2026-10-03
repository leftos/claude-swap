# claude-swap plan

This fork's work queue. Upstream (`realiti4/claude-swap`) issues and PRs are not triaged here: a line cites an upstream `#N` only when the work is taken on. Terms are in the [glossary](../glossary.md); the code map is [ARCHITECTURE.md](../ARCHITECTURE.md).

<!-- plan-doc-hygiene: 2026-10-03 fcbf3bf -->

## Current focus

- [ ] Take the consume lock around `cswap run`'s bootstrap, so a session never starts on a backup grant being spent (ops-tower request, OPS-82). `session.py:765` takes only the account lock (`switcher.lock_file`) around `_bootstrap` (`session.py:860`); the consume gate (`switcher.py:2084`, `credentials/.consume-<n>.lock`) checks for a live session profile under the account lock, releases it, then POSTs. A bootstrap between the check and the POST seeds the profile with the generation being spent. Take the consume lock before the account lock, in the gate's order (`switcher.py:4284`).

## Next up

## Backlog
