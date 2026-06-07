## Learned User Preferences

- Prefer Composer 2.5 (`composer-2.5-fast`) subagents for thermo-nuclear code quality review tasks.
- Use the `thermo-nuclear-code-quality-review` subagent for refactor plan reviews, then fix blockers from its findings.
- Delegate substantial refactors to subagents rather than doing all implementation in the parent agent.
- Follow repo commit format `type: short summary` per `docs/COMMIT_WORKFLOW.md`; split related work into focused commits.
- Want automated test feedback after `git push`: remote post-commit workflow plus local post-push tests via `scripts/install-git-hooks.sh`.
- Skip desktop in local post-push checks with `ORPHEUS_POST_PUSH_SKIP_DESKTOP=1`; bypass the push wrapper with `git -c alias.push= push`.

## Learned Workspace Facts

- Orpheus is a desktop-first TTS/audiobook app; primary UI is the Tauri desktop shell with a Python backend.
- Python/core regression gate: `python run_tests.py --contract` (see `docs/TESTING.md`).
- CI validation runs Python (Ruff + contract pytest) and Desktop (npm typecheck, cargo test, cargo check) on `macos-14`.
- Kokoro runtime contracts live under `modules/tts/runtimes/` after the runtime-boundary refactor.
- CUDA/runtime refactor covers `TTSRuntimeSpec`, typed runtime options, provider-owned device memory policies, and pipeline worker integration.
- `.github/workflows/post-commit.yml` runs validation on every branch push.
- Local post-push tests are wired through a `git push` alias installed by `scripts/install-git-hooks.sh`.
