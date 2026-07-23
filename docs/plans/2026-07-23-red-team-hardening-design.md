# Red-Team Hardening Design

## Objective

Make the current hardening patch safe to publish by correcting the database
recovery regression, adding missing regression coverage, and adversarially
testing the changed and adjacent behavior. Confirmed high- and medium-severity
defects are fixed test-first; low-risk polish is documented rather than allowed
to expand the change set.

## Approach

Use targeted hardening rather than a broad refactor. Preserve the existing
storage, API, ingestion, and desktop architecture and strengthen its behavioral
contracts at the boundaries where malformed input, stale state, filesystem
ambiguity, or concurrency can cause user-visible damage.

Alternatives considered:

1. Fix only database recovery. This is fast but leaves several manually verified
   API changes without durable regression coverage.
2. Targeted hardening (selected). This closes confirmed defects and probes
   adjacent failure modes without destabilizing unrelated code.
3. Broad storage/API refactor. This could simplify some internals but is too
   risky inside an already large, uncommitted patch.

## Database Recovery

Automatic quarantine is allowed only when SQLite explicitly classifies the
failure as `SQLITE_CORRUPT` or `SQLITE_NOTADB`. Transient locks, read-only
filesystems, disk-I/O failures, full disks, and migration defects must preserve
the original database and surface the original exception.

Quarantine destinations must be collision-safe. Existing quarantines must never
be overwritten. WAL/SHM sidecars are moved only to unique destinations and are
never deleted merely because a move failed.

## Red-Team Matrix

- Storage: transient SQLite failures, corrupt files, future schemas, quarantine
  collisions, WAL/SHM preservation, symlinks, traversal-like stored paths,
  unclaimed audio, ambiguous book-to-folder mapping, retention races, and
  repeated maintenance.
- API: non-finite numbers, numeric extremes, malformed JSON, oversized and
  extensionless sources, controller/provider error translation, and auth/path
  boundaries.
- Ingestion: verse/list preservation, genuine back-matter removal, pathological
  regex inputs, oversized single paragraphs/sentences, unusual EPUB TOCs, and
  empty/encoding edge cases.
- Desktop: same-frame repeated actions, active-job state, out-of-range numeric
  input, tab transition races, disconnected backend state, and queue/paste
  failures.

## Testing and Exit Criteria

Every new fix begins with a failing regression test that reproduces the defect.
Focused tests must pass before moving to the next behavior. Final verification
includes Ruff, the Python contract suite, frontend typecheck/tests/build, and
the repository's relevant desktop/Rust checks. Any environment-dependent gate
that cannot run is reported explicitly.

The pass is complete only when no confirmed high/medium issue remains in the
examined scope, all implemented fixes have regression coverage, and the final
verification commands finish successfully.
