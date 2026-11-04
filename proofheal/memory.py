"""Success-run memory: what each step touched and what outcome it must preserve.

Stored in SQLite at .proofheal/memory.db (gitignored). Only fully green test runs
are written, so memory always describes known-good behavior.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
  test_id      TEXT NOT NULL,
  step_name    TEXT NOT NULL,
  step_index   INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  locator      TEXT,
  value        TEXT,
  fingerprint  TEXT,
  crop_path    TEXT,
  url_before   TEXT,
  url_after    TEXT,
  dom_changed  INTEGER,
  outcome      TEXT,
  variant      TEXT,
  recorded_at  REAL,
  PRIMARY KEY (test_id, step_name)
);
"""


@dataclass
class StepMemory:
    test_id: str
    step_name: str
    step_index: int
    kind: str
    locator: str | None
    value: str | None
    fingerprint: dict[str, Any] | None
    crop_path: str | None
    url_before: str | None
    url_after: str | None
    dom_changed: bool | None
    # For actions: assertions that held right after this step in the green run.
    outcome: list[dict[str, Any]] = field(default_factory=list)
    variant: str | None = None
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryStore:
    def __init__(self, path: str | Path = ".proofheal/memory.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    def save_test(self, records: list[StepMemory]) -> None:
        if not records:
            return
        with self.db:
            self.db.execute("DELETE FROM steps WHERE test_id = ?", (records[0].test_id,))
            for r in records:
                self.db.execute(
                    "INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r.test_id, r.step_name, r.step_index, r.kind, r.locator, r.value,
                        json.dumps(r.fingerprint) if r.fingerprint else None, r.crop_path,
                        r.url_before, r.url_after,
                        None if r.dom_changed is None else int(r.dom_changed),
                        json.dumps(r.outcome), r.variant, r.recorded_at,
                    ),
                )

    def _row(self, row: tuple) -> StepMemory:
        return StepMemory(
            test_id=row[0], step_name=row[1], step_index=row[2], kind=row[3], locator=row[4], value=row[5],
            fingerprint=json.loads(row[6]) if row[6] else None, crop_path=row[7],
            url_before=row[8], url_after=row[9], dom_changed=None if row[10] is None else bool(row[10]),
            outcome=json.loads(row[11] or "[]"), variant=row[12], recorded_at=row[13],
        )

    def get(self, test_id: str, step_name: str) -> StepMemory | None:
        cur = self.db.execute("SELECT * FROM steps WHERE test_id = ? AND step_name = ?", (test_id, step_name))
        row = cur.fetchone()
        return self._row(row) if row else None

    def for_test(self, test_id: str) -> list[StepMemory]:
        cur = self.db.execute("SELECT * FROM steps WHERE test_id = ? ORDER BY step_index", (test_id,))
        return [self._row(r) for r in cur.fetchall()]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM steps").fetchone()[0]

    def close(self) -> None:
        self.db.close()


def attach_outcomes(records: list[StepMemory]) -> None:
    """For each action step, collect the assertions that follow it until the next action."""
    for i, rec in enumerate(records):
        if rec.kind not in ("click", "goto"):
            continue
        outcome = []
        for nxt in records[i + 1 :]:
            if nxt.kind in ("click", "goto"):
                break
            if nxt.kind == "expect_text":
                outcome.append({"step": nxt.step_name, "locator": nxt.locator, "expected_text": nxt.value})
        rec.outcome = outcome
