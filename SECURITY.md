# Security Policy

## Reporting a Vulnerability

Report vulnerabilities privately through GitHub Security Advisories on this
repository ("Report a vulnerability" under the Security tab). Do not open a
public issue for security reports. You should receive a response within a week.

## Scope

Reports most useful to the project:

- The local FastAPI backend: authentication bypass, path traversal in
  upload/source/audio endpoints, or any way to reach the API without the
  per-launch bearer token.
- The Tauri shell: CSP or capability escapes, IPC abuse.
- Document ingestion: memory-safety or resource-exhaustion issues when parsing
  hostile PDF/EPUB/Markdown input.
- Model and sidecar download integrity in `scripts/download_models.py` and
  `scripts/setup_runtime.py`.

## Design Assumptions

The backend binds to loopback only and every `/api/` route requires a
per-launch 256-bit bearer token; anything that weakens either property is a
vulnerability. Model files and imported documents are treated as untrusted
input — use trusted model artifacts only.
