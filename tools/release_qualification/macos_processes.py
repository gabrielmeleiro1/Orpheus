"""Read-only macOS process sampling for release qualification."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


# Numeric fields first; args is the final catch-all so paths with spaces stay intact.
PS_COMMAND: tuple[str, ...] = (
    "ps",
    "-ax",
    "-o",
    "pid=,ppid=,pcpu=,rss=,args=",
)


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    ppid: int
    pcpu: float
    rss_kib: int
    comm: str
    args: str
    start_time: str | None = None

    @property
    def exe_path(self) -> Path | None:
        text = self.args.strip()
        if not text:
            return None
        # Prefer absolute command path; keep spaces (helper bundle paths).
        if text.startswith("/"):
            # Drop trailing argv when present after a clear separator following .app path.
            return Path(_executable_path_from_args(text))
        token = text.split(" ", 1)[0]
        return Path(token) if token else None


def _executable_path_from_args(args: str) -> str:
    """Return the absolute executable path from a ps args string that may contain spaces."""
    text = args.strip()
    if " -" in text:
        # Common pattern: "/path/with spaces/Orpheus --flag"
        head, _sep, _tail = text.partition(" -")
        if head.startswith("/"):
            return head
    return text


def parse_ps_output(text: str) -> list[ProcessSample]:
    samples: list[ProcessSample] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid_s, ppid_s, pcpu_s, rss_s, args = parts
        exe = _executable_path_from_args(args) if args.startswith("/") else args.split(" ", 1)[0]
        comm = Path(exe).name if exe else ""
        samples.append(
            ProcessSample(
                pid=int(pid_s),
                ppid=int(ppid_s),
                pcpu=float(pcpu_s),
                rss_kib=int(float(rss_s)),
                comm=comm,
                args=args,
            )
        )
    return samples


def sample_processes(
    *,
    runner: Callable[[Sequence[str]], str] | None = None,
) -> list[ProcessSample]:
    if runner is None:
        completed = subprocess.run(
            list(PS_COMMAND),
            check=False,
            capture_output=True,
            text=True,
        )
        text = completed.stdout or ""
    else:
        text = runner(PS_COMMAND)
    return parse_ps_output(text)


def attribute_orpheus_tree(
    samples: Iterable[ProcessSample],
    *,
    app_bundle: Path,
    runtime_root: Path | None = None,
) -> list[ProcessSample]:
    bundle = app_bundle.resolve()
    runtime = runtime_root.resolve() if runtime_root is not None else None
    by_pid = {sample.pid: sample for sample in samples}

    root_pids: set[int] = set()
    for sample in by_pid.values():
        if _path_in_roots(sample.exe_path, bundle=bundle, runtime=runtime):
            parent = by_pid.get(sample.ppid)
            parent_inside = parent is not None and _path_in_roots(
                parent.exe_path, bundle=bundle, runtime=runtime
            )
            if not parent_inside:
                root_pids.add(sample.pid)

    if not root_pids:
        root_pids = {
            sample.pid
            for sample in by_pid.values()
            if _path_in_roots(sample.exe_path, bundle=bundle, runtime=runtime)
        }

    included: set[int] = set()
    stack = list(root_pids)
    while stack:
        pid = stack.pop()
        if pid in included or pid not in by_pid:
            continue
        sample = by_pid[pid]
        if not _path_in_roots(sample.exe_path, bundle=bundle, runtime=runtime):
            continue
        included.add(pid)
        for child in by_pid.values():
            if child.ppid == pid and child.pid not in included:
                stack.append(child.pid)

    return [by_pid[pid] for pid in sorted(included)]


def _path_in_roots(
    path: Path | None,
    *,
    bundle: Path,
    runtime: Path | None,
) -> bool:
    if path is None:
        return False
    text = str(path)
    for root in (bundle, runtime):
        if root is None:
            continue
        try:
            root_s = str(root.resolve())
        except OSError:
            root_s = str(root)
        if text == root_s or text.startswith(root_s + os.sep):
            return True
        # Spaced volume paths may not exist to resolve; still match textual prefix.
        if text.startswith(str(root) + os.sep) or text == str(root):
            return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved == bundle or resolved.is_relative_to(bundle):
        return True
    if runtime is not None and (resolved == runtime or resolved.is_relative_to(runtime)):
        return True
    return False
