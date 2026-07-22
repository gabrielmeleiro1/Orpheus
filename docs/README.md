# Documentation

Maintained project documentation lives under `docs/`. The root `README.md` is the public entry point.

## Reading Order

1. [README.md](../README.md)
2. [DEPENDENCIES.md](DEPENDENCIES.md)
3. [ARCHITECTURE.md](ARCHITECTURE.md)
4. [CURRENT_STATE.md](CURRENT_STATE.md)
5. [RUN_APPLICATION.md](RUN_APPLICATION.md)
6. [TESTING.md](TESTING.md)
7. [COMMIT_WORKFLOW.md](COMMIT_WORKFLOW.md)

## Active Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): system boundaries, runtime contracts, and [where to change X](ARCHITECTURE.md#where-to-change-x).
- [CURRENT_STATE.md](CURRENT_STATE.md): what is currently supported and how the product behaves.
- [DEPENDENCIES.md](DEPENDENCIES.md): system prerequisites and package manifests for GitHub clones.
- [RUN_APPLICATION.md](RUN_APPLICATION.md): setup, launch, packaging, and install workflow.
- [RUNTIME_SELECTION.md](RUNTIME_SELECTION.md): how a TTS runtime provider is selected per platform.
- [TESTING.md](TESTING.md): contract, smoke, lint, and release verification commands.
- [COMMIT_WORKFLOW.md](COMMIT_WORKFLOW.md): commit order, message style, staging, and verification rules.

### Architecture inventory

Package-level boundary maps:

- [architecture/python-boundaries.md](architecture/python-boundaries.md)
- [architecture/desktop-boundaries.md](architecture/desktop-boundaries.md)
- [architecture/tooling-boundaries.md](architecture/tooling-boundaries.md)

## Local Runtime State

- `data/`, `output/`, and `temp/` are local-only runtime state.
