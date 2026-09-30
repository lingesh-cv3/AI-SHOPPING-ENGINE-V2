"""Scaffolding shared by the Operations characterisation tests.

These tests record what the Operations code does TODAY, so later cleanup can be
shown not to have changed anything it should not. They are model-free and need no
running services: each one gets its own throwaway SQLite file and drives the real
repository / real FastAPI app in-process.

Two kinds of assertion, reported differently:

  ok / FAIL   a semantic the code currently has (or must have) and which must keep
              holding. A FAIL is a regression and fails the run.

  DEFECT      a behaviour that is WRONG today and is recorded with the value
              actually observed. It is written as the assertion that should hold
              after the fix, so it flips to `ok` by itself when the defect is
              fixed. It does not fail the run unless --strict is passed.

The database is forced, never defaulted, and the run aborts if it is not one of
these throwaway files - these tests must be incapable of touching cv3.db.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def isolate(name: str) -> Path:
    """Point the engine at a fresh throwaway database. Call before any engine import."""
    db_file = ROOT / f"_test_ops_{name}.db"
    for suffix in ("", "-journal", "-wal", "-shm"):
        leftover = Path(str(db_file) + suffix)
        if leftover.exists():
            leftover.unlink()

    # Assigned, not setdefault: a DATABASE_URL already exported in the shell (for
    # instance one pointing at cv3.db) must not win.
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return db_file


def assert_isolated() -> None:
    """Abort unless the engine really is on a throwaway database."""
    from engine.db.session import database_url

    url = database_url()
    if "_test_ops_" not in url:
        raise SystemExit(f"refusing to run: database is {url!r}, not a throwaway file")


class Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []
        self.defects: list[tuple[str, str]] = []
        self.total = 0

    def ok(self, label: str, condition: bool, detail: str = "") -> None:
        self.total += 1
        if condition:
            print(f"  ok      {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL    {label}" + (f"  [{detail}]" if detail else ""))

    def defect(self, label: str, condition: bool, observed: str) -> None:
        """A known-wrong behaviour. `condition` is what must be true once fixed."""
        self.total += 1
        if condition:
            print(f"  ok      {label}  (defect no longer reproduces)")
        else:
            self.defects.append((label, observed))
            print(f"  DEFECT  {label}  [observed: {observed}]")

    def finish(self) -> int:
        print()
        print(
            f"  {self.total} checks: {self.total - len(self.failed) - len(self.defects)} ok, "
            f"{len(self.failed)} FAIL, {len(self.defects)} DEFECT (known, reproducing)"
        )
        for label in self.failed:
            print(f"    FAIL   {label}")
        for label, observed in self.defects:
            print(f"    DEFECT {label} -> {observed}")
        strict = "--strict" in sys.argv
        return 1 if (self.failed or (strict and self.defects)) else 0
