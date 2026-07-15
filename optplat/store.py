"""Persistence: archive every evaluation to SQLite, resume, roll back.

  * archive : each evaluation (run_id, seq, stage, point, objectives, ts) is
              appended durably — attach a store to an Evaluator.
  * resume  : reopen the same db + run_id and continue; `best()` gives the best
              archived point to restart the operating point from.
  * rollback: `rollback_to_best()` physically drives the stage back to the best
              archived point (e.g. after an abort) via the evaluator.

Pure standard-library sqlite3 — no dependency, no license concern.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Optional


class SQLiteStore:
    def __init__(self, path: str = ":memory:", run_id: Optional[str] = None):
        self.conn = sqlite3.connect(path)
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS evals ("
            "run_id TEXT, seq INTEGER, stage TEXT, point TEXT, objectives TEXT, ts REAL)"
        )
        self.conn.commit()
        self._seq = self._max_seq()

    def _max_seq(self) -> int:
        cur = self.conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM evals WHERE run_id=?", (self.run_id,)
        )
        return int(cur.fetchone()[0])

    def append(self, stage: str, x: dict, y: dict) -> None:
        self._seq += 1
        self.conn.execute(
            "INSERT INTO evals VALUES (?,?,?,?,?,?)",
            (self.run_id, self._seq, stage, json.dumps(x), json.dumps(y), time.time()),
        )
        self.conn.commit()

    def history(self, run_id: Optional[str] = None) -> list[dict]:
        rid = run_id or self.run_id
        cur = self.conn.execute(
            "SELECT stage, point, objectives FROM evals WHERE run_id=? ORDER BY seq", (rid,)
        )
        return [{"stage": s, **json.loads(p), **json.loads(o)} for s, p, o in cur]

    def best(self, objective: str, mode: str = "max",
             run_id: Optional[str] = None) -> Optional[tuple[dict, dict]]:
        """Return (point, objectives) of the best archived evaluation, or None."""
        rid = run_id or self.run_id
        cur = self.conn.execute(
            "SELECT point, objectives FROM evals WHERE run_id=?", (rid,)
        )
        rows = [(json.loads(p), json.loads(o)) for p, o in cur]
        rows = [r for r in rows if objective in r[1]]
        if not rows:
            return None
        pick = max if mode == "max" else min
        return pick(rows, key=lambda r: r[1][objective])


def rollback_to_best(evaluator, store: SQLiteStore, objective: str,
                     mode: str = "max") -> Optional[dict]:
    """Drive the hardware back to the best archived point (e.g. after an abort).

    Works with any evaluator whose evaluate() moves the stage (HardwareEvaluator).
    Returns the point rolled back to, or None if nothing archived.
    """
    found = store.best(objective, mode)
    if not found:
        return None
    point, _ = found
    evaluator.evaluate(point, stage="rollback")
    return point
