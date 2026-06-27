from __future__ import annotations

import sys

from . import bench


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "bench":
        return bench.main(args[1:])
    return bench.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
