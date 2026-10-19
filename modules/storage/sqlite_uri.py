"""SQLite file URIs that preserve Windows drive and UNC path semantics."""

from pathlib import PurePath, PureWindowsPath
from urllib.parse import quote


def readonly_sqlite_uri(path: PurePath) -> str:
    """Encode an absolute filename without putting a Windows prefix in authority.

    ``Path.as_uri`` treats a verbatim Windows path's ``?`` or a UNC server as
    URI authority. SQLite rejects non-local authorities. Keep UNC servers in
    the filename instead, and strip only the Windows verbatim-path prefix.
    The original filesystem path remains unchanged for ownership and I/O.
    """
    if not path.is_absolute():
        raise ValueError("SQLite read-only URI requires an absolute path")
    filename = path.as_posix()
    if isinstance(path, PureWindowsPath):
        if filename.lower().startswith("//?/unc/"):
            filename = "//" + filename[8:]
        elif filename.startswith("//?/"):
            filename = filename[4:]
        if not filename.startswith("/"):
            filename = "/" + filename
    return "file://" + quote(filename, safe="/:") + "?mode=ro"
