# Data Directory

When the backend runs from a source checkout outside the desktop shell, it
keeps its local state here. Nothing in this directory except this README is
source; Git ignores the rest.

## Contents

- `audiobooks.db`: the SQLite library database.
- `audiobooks.db-wal`, `audiobooks.db-shm`: SQLite write-ahead log files.
- `audiobooks.db.lock`, `.orpheus-database-locks`: process locks.
- `sources/`: durable copies of source documents used by the library.
- `backups/`: database backups and the safety copies made before a restore.
- `reports/`: storage cleanup reports.

The app recreates these files as needed. Do not edit them while the backend is
running.

## Related directories

- `output/`: generated audiobooks.
- `temp/`: intermediate files during conversion.

The desktop app does not use these repository folders. It keeps the same
layout in its app data directory; see
[data locations](../docs/RUN_APPLICATION.md#data-locations).
