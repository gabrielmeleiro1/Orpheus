# Data Directory

`data/` is local runtime state, not project source.

## What Lives Here

- `audiobooks.db`: the local SQLite library database created by the app.
- `audiobooks.db-wal`
- `audiobooks.db-shm`

These files represent the current state of one local workspace. They are not sample fixtures and they should not be treated as canonical repository content.

## Safe Expectations

- The database files can be recreated by the application.
- WAL and SHM files are transient SQLite sidecar files.
- Do not hand-edit these files while the app is running.

## Related Local Output

- `output/` stores generated audiobook artifacts.
- `temp/` stores transient processing artifacts.

Those directories are local runtime output as well.
