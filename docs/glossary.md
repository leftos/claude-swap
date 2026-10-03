# Glossary

Terms the docs, plan and commits use in a project-specific sense.

- **Slot**: a numbered stored account (`Account-<n>`), its credential backup and config backup under the backup root.
- **Backup grant**: the one-time-use OAuth refresh token held in a slot's credential backup. POSTing it spends it; a second POST of the same grant gets `invalid_grant`.
- **Consume gate**: `ClaudeAccountSwitcher.consume_backup_grant`, the one path through which a backup grant is spent (re-read, POST, compare-and-swap persist).
- **Consume lock**: `credentials/.consume-<n>.lock`, the per-slot `FileLock` the consume gate holds from its re-read until its persist. Taken before the account lock (`.lock` in the backup root), never after it.
- **Session profile**: the per-account config directory `cswap run` launches Claude Code in, seeded from the slot's backup by `_bootstrap`.
