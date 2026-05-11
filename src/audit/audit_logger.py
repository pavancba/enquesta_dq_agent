"""
Audit Logger — Layer 5 (Audit & Output)

Writes every event, finding, decision, correction, and LLM call to a SQLite
database. This is the single source of truth for "what happened to this file."
Every other module in the agent calls into this class.

Tables:
  file_runs   — one row per file processed
  findings    — one row per data quality issue detected
  decisions   — one row per router verdict
  corrections — one row per auto-correction (with before/after for reversibility)
  llm_calls   — one row per Ollama invocation (prompt, response, latency)

Design choices:
  - SQLite single-file DB (audit.db) — zero infra, easy to show, easy to query.
  - All writes are best-effort — if logging fails we print a warning but don't
    crash the agent (we'd rather lose audit detail than fail a billing run).
  - Foreign keys not strictly enforced — relations are by run_id string for
    simplicity, since we only ever query forward from a run_id.
  - No external SQL — every method takes typed inputs and produces typed output.

Run this file directly to see a self-test:
    python -m src.audit.audit_logger
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Use absolute import so this file runs both as a module and as a script.
try:
    from src.models.schemas import Finding, Decision, FileRun
except ImportError:  # pragma: no cover — only hit when run from inside the package
    from models.schemas import Finding, Decision, FileRun  # type: ignore


# ---------------------------------------------------------------------------
# Schema — embedded as SQL so the audit DB is self-describing.
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_runs (
    run_id          TEXT PRIMARY KEY,
    file_name       TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    total_rows      INTEGER DEFAULT 0,
    auto_corrected  INTEGER DEFAULT 0,
    quarantined     INTEGER DEFAULT 0,
    flagged         INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'in_progress'
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    row_index       INTEGER NOT NULL,
    column_name     TEXT,
    raw_value       TEXT,
    severity        TEXT NOT NULL,
    description     TEXT NOT NULL,
    confidence      REAL DEFAULT 1.0,
    llm_reasoning   TEXT,
    logged_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    finding_id      INTEGER,
    rule_id         TEXT NOT NULL,
    row_index       INTEGER NOT NULL,
    action          TEXT NOT NULL,
    corrected_value TEXT,
    reasoning       TEXT NOT NULL,
    logged_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    row_index       INTEGER NOT NULL,
    column_name     TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    value_before    TEXT NOT NULL,
    value_after     TEXT NOT NULL,
    logged_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_run ON corrections(run_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    row_index       INTEGER,
    model           TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    response        TEXT NOT NULL,
    latency_ms      INTEGER NOT NULL,
    logged_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);
"""


def _now() -> str:
    """UTC timestamp in ISO format — good for sorting and human-readable."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLogger:
    """SQLite-backed audit ledger for the DQ agent."""

    def __init__(self, db_path: str | Path = "audit.db") -> None:
        self.db_path = Path(db_path)
        self._ensure_schema()

    # -- internal helpers ----------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)

    @staticmethod
    def _safe(fn):
        """
        Decorator: log warning + swallow exceptions instead of crashing the agent.
        Audit failures must never abort a billing-data run.
        """
        def wrapped(self, *a, **kw):
            try:
                return fn(self, *a, **kw)
            except Exception as e:
                print(f"[audit_logger] WARN: {fn.__name__} failed: {e}", file=sys.stderr)
                return None
        return wrapped

    # -- public API ----------------------------------------------------------

    def start_run(self, file_name: str, file_path: str) -> str:
        """Register a new processing run. Returns a fresh run_id (UUID4)."""
        run_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO file_runs "
                "(run_id, file_name, file_path, started_at, status) "
                "VALUES (?, ?, ?, ?, 'in_progress')",
                (run_id, file_name, file_path, _now()),
            )
        return run_id

    @_safe
    def log_finding(self, finding: Finding) -> Optional[int]:
        """Persist one data-quality finding. Returns the new finding_id."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO findings "
                "(run_id, rule_id, rule_name, row_index, column_name, raw_value, "
                " severity, description, confidence, llm_reasoning, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finding.run_id,
                    finding.rule_id,
                    finding.rule_name,
                    finding.row_index,
                    finding.column,
                    finding.raw_value,
                    finding.severity.value,
                    finding.description,
                    finding.confidence,
                    finding.llm_reasoning,
                    _now(),
                ),
            )
            return cur.lastrowid

    @_safe
    def log_decision(self, decision: Decision, finding_id: Optional[int] = None) -> None:
        """Persist the router's verdict for one finding."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO decisions "
                "(run_id, finding_id, rule_id, row_index, action, "
                " corrected_value, reasoning, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.finding.run_id,
                    finding_id,
                    decision.finding.rule_id,
                    decision.finding.row_index,
                    decision.action.value,
                    decision.corrected_value,
                    decision.reasoning,
                    _now(),
                ),
            )

    @_safe
    def log_correction(
        self,
        run_id: str,
        row_index: int,
        column_name: str,
        rule_id: str,
        value_before: str,
        value_after: str,
    ) -> None:
        """Persist one auto-correction with full before/after for reversibility."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO corrections "
                "(run_id, row_index, column_name, rule_id, value_before, "
                " value_after, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, row_index, column_name, rule_id,
                 value_before, value_after, _now()),
            )

    @_safe
    def log_llm_call(
        self,
        run_id: str,
        model: str,
        prompt: str,
        response: str,
        latency_ms: int,
        row_index: Optional[int] = None,
    ) -> None:
        """Persist one Ollama invocation. Useful for auditing LLM judgments."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO llm_calls "
                "(run_id, row_index, model, prompt, response, latency_ms, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, row_index, model, prompt, response, latency_ms, _now()),
            )

    def finish_run(
        self,
        run_id: str,
        total_rows: int,
        auto_corrected: int,
        quarantined: int,
        flagged: int,
        status: str = "completed",
    ) -> None:
        """Mark a run complete with its final counts."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE file_runs SET "
                "finished_at = ?, total_rows = ?, auto_corrected = ?, "
                "quarantined = ?, flagged = ?, status = ? WHERE run_id = ?",
                (_now(), total_rows, auto_corrected, quarantined, flagged,
                 status, run_id),
            )

    # -- read-side -----------------------------------------------------------

    def get_run_summary(self, run_id: str) -> Optional[dict]:
        """Fetch one run + its aggregates for the UI / report."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM file_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                return None
            summary = dict(row)
            summary["finding_count"] = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            summary["correction_count"] = conn.execute(
                "SELECT COUNT(*) FROM corrections WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            summary["llm_call_count"] = conn.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            return summary

    def list_recent_runs(self, limit: int = 20) -> list[dict]:
        """Most recent runs first — for the Streamlit dashboard."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM file_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.audit.audit_logger
# ---------------------------------------------------------------------------
def _self_test() -> int:
    """End-to-end smoke test. Creates a temp DB, exercises every method."""
    import tempfile
    from src.models.schemas import Severity, Action

    print("=" * 60)
    print("AuditLogger self-test")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test_audit.db"
        logger = AuditLogger(db)
        print(f"[1] Created DB at {db}")

        # Start a run
        run_id = logger.start_run("demo_test.csv", "/fake/path/demo_test.csv")
        print(f"[2] Started run: {run_id}")

        # Log a Rule 2 finding (trailing-negative amount)
        finding = Finding(
            run_id=run_id,
            rule_id="R002",
            rule_name="trailing_negative_amount",
            row_index=5,
            column="CIBL_AMT_5",
            raw_value="2000.00-",
            severity=Severity.MEDIUM,
            description="Trailing minus sign in amount",
        )
        finding_id = logger.log_finding(finding)
        print(f"[3] Logged finding -> id={finding_id}")

        # Log the router's decision: AUTO_CORRECT
        decision = Decision(
            finding=finding,
            action=Action.AUTO_CORRECT,
            corrected_value="-2000.00",
            reasoning="Rule 2 trailing-negative is unambiguous; safe to auto-correct.",
        )
        logger.log_decision(decision, finding_id=finding_id)
        print("[4] Logged decision: AUTO_CORRECT")

        # Log the actual correction (before/after)
        logger.log_correction(
            run_id=run_id, row_index=5, column_name="CIBL_AMT_5",
            rule_id="R002", value_before="2000.00-", value_after="-2000.00",
        )
        print("[5] Logged correction: '2000.00-' -> '-2000.00'")

        # Log a Rule 3 LLM call (Ollama judging a CIBIL Comment)
        logger.log_llm_call(
            run_id=run_id, row_index=7, model="llama3.2:3b",
            prompt="Is 'VP BILL #: 27007839' a valid CIBIL Comment?",
            response='{"verdict":"suspicious","confidence":0.78}',
            latency_ms=4170,
        )
        print("[6] Logged LLM call (Rule 3 judgment)")

        # Finish the run
        logger.finish_run(
            run_id=run_id,
            total_rows=10, auto_corrected=1, quarantined=0, flagged=1,
        )
        print("[7] Finished run")

        # Read it back
        summary = logger.get_run_summary(run_id)
        print()
        print("Run summary (read back from DB):")
        print("-" * 60)
        for k, v in summary.items():
            print(f"  {k:20s} {v}")

        recent = logger.list_recent_runs()
        print()
        print(f"list_recent_runs() returned {len(recent)} run(s) - OK")

    print()
    print("All self-test steps passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
