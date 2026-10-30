# Security Policy

## Reporting a Vulnerability

Report vulnerabilities privately through GitHub Security Advisories: open the
**Security** tab of this repository and choose **Report a vulnerability**. Do
not open a public issue for security reports. You should receive a response
within a week.

Security fixes target the latest release and the `main` branch.

## Scope

The most useful reports concern:

- **The local FastAPI backend:** authentication bypass, path traversal in the
  upload, source, or audio endpoints, or any way to reach the API without the
  per-launch bearer token.
- **The Tauri shell:** CSP or capability escapes, and IPC abuse.
- **Document ingestion:** memory-safety or resource-exhaustion issues when
  parsing hostile PDF, EPUB, or Markdown input.
- **Download integrity:** model and runtime downloads in
  `scripts/download_models.py` and `scripts/setup_runtime.py`.

## Design Assumptions

The backend binds to loopback only. Every `/api/` route requires the
per-launch 256-bit bearer token, or a short-lived, scoped query token minted
with it for media and event streams. Anything that weakens either property is
a vulnerability. Model files and imported documents are untrusted input; use
trusted model artifacts only.
