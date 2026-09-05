# Documentation

Maintained project documentation lives under `docs/`. The root `README.md` is the public entry point.

## Reading Order

1. [README.md](../README.md)
2. [DEPENDENCIES.md](DEPENDENCIES.md)
3. [ARCHITECTURE.md](ARCHITECTURE.md)
4. [CURRENT_STATE.md](CURRENT_STATE.md)
5. [RUN_APPLICATION.md](RUN_APPLICATION.md)
6. [TESTING.md](TESTING.md)
7. [RELEASING_MACOS.md](RELEASING_MACOS.md)
8. [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md)
9. [COMMIT_WORKFLOW.md](COMMIT_WORKFLOW.md)

## Active Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): system boundaries, runtime contracts, and [where to change X](ARCHITECTURE.md#where-to-change-x).
- [CURRENT_STATE.md](CURRENT_STATE.md): what is currently supported and how the product behaves.
- [DEPENDENCIES.md](DEPENDENCIES.md): system prerequisites and package manifests for GitHub clones.
- [RUN_APPLICATION.md](RUN_APPLICATION.md): setup, launch, packaging, and install workflow.
- [RUNTIME_SELECTION.md](RUNTIME_SELECTION.md): how a TTS runtime provider is selected per platform.
- [TESTING.md](TESTING.md): contract, smoke, lint, and release verification commands.
- [RELEASING_MACOS.md](RELEASING_MACOS.md): local qualification gate vs source CI, portable checksum staging, and manual GitHub Release draft/review/publish.
- [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md): end-user checksum verification and supported Gatekeeper Open / Open Anyway install.
- [COMMIT_WORKFLOW.md](COMMIT_WORKFLOW.md): commit order, message style, staging, and verification rules.

## Local Runtime State

- `data/`, `output/`, and `temp/` are local-only runtime state.
