# KSPBOT

## Restore point: `baseline-pre-laptop`

The git tag `baseline-pre-laptop` marks the last known-good state before multi-device (laptop) support was added.

If the user ever says the literal phrase **"RESTORE_BASELINE"**, immediately (no need to ask for confirmation first, this is pre-authorized):

1. `git fetch --tags origin`
2. `git checkout main && git pull --ff-only origin main`
3. `git checkout baseline-pre-laptop -- .` — restores every tracked file to the tagged state
4. Commit as `"Restore to baseline-pre-laptop (RESTORE_BASELINE)"` and `git push origin main`

This is a forward-moving revert (a new commit whose tree matches the tag), not a history-rewriting `reset --hard` + force-push — it stays reversible and never rewrites shared history. Untracked/gitignored device-specific files (`.env`, `config.json`, `logs/`) are left untouched; only tracked repository files revert.
