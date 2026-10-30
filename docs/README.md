# Documentation

The [project README](../README.md) is the entry point. These guides cover the
details.

## Using and building Orpheus

- [DEPENDENCIES.md](DEPENDENCIES.md): system prerequisites, Python runtime
  profiles, model assets, and the clean-environment release gate.
- [RUN_APPLICATION.md](RUN_APPLICATION.md): running from source, first-launch
  setup, model management, library maintenance, data locations, packaging,
  and the Windows and macOS installers.
- [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md): installing an
  unsigned macOS release, with checksum verification and the supported
  Gatekeeper first-open steps.

## How it works

- [ARCHITECTURE.md](ARCHITECTURE.md): components, the desktop runtime
  contract, request flow, API surface, pipeline, storage, and
  [where to change X](ARCHITECTURE.md#where-to-change-x).
- [RUNTIME_SELECTION.md](RUNTIME_SELECTION.md): how the Kokoro TTS provider is
  chosen on each platform.
- [ENVIRONMENT.md](ENVIRONMENT.md): every environment variable the backend,
  shell, scripts, and tests read.

## Contributing and releasing

- [CONTRIBUTING.md](../CONTRIBUTING.md): branches, commits, checks, and pull
  requests.
- [TESTING.md](TESTING.md): test tiers, focused checks, CI workflows, the
  pre-push hook, and release verification.
- [RELEASING_MACOS.md](RELEASING_MACOS.md): the full local release gate
  (`scripts/release_qualification.py`) and the manual GitHub Release
  procedure. Source CI (`.github/workflows/source-validation.yml`) is not a
  release gate.
- [Frontend stress harness](../tools/frontend_stress/README.md): Playwright
  journeys, the interaction-state audit, screen capture, and frontend
  performance baselines.

## Local runtime state

`data/`, `output/`, and `temp/` hold local runtime state when the backend runs
from the checkout. Their contents are ignored by Git; see
[data/README.md](../data/README.md).
