"""
Quarantine Handler — Layer 3 (Decision)

Splits the post-corrector DataFrame into three output buckets and writes
each non-empty bucket to its own CSV. One row goes to exactly one bucket;
precedence is QUARANTINE > FLAG_FOR_REVIEW > clean.

Row-assignment rules
--------------------
  Rows with no decisions                                    -> clean
  Rows with only ACCEPT and/or AUTO_CORRECT decisions       -> clean
  Rows with at least one FLAG_FOR_REVIEW decision           -> flagged
  Rows with at least one QUARANTINE decision                -> quarantine
    (QUARANTINE wins even if the same row also has a FLAG)

File-writing rules
------------------
  * Buckets with zero rows produce no file (path returned as None).
  * Filename pattern:  <original_stem>_<bucket>_<run_id_first_8>.csv
        e.g. demo_07_showcase_synthetic_quarantine_eedae511.csv
  * UTF-8, no pandas index, all original columns preserved.

The handler returns a SplitResult that includes a row_to_decisions map so
the notifier can build a per-row email body without re-traversing the
decision list.

Run this file directly to self-test (end-to-end pipeline on demo_07):
    python -m src.decision.quarantine_handler
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    from src.models.schemas import Decision, Action
except ImportError:  # pragma: no cover
    from models.schemas import Decision, Action  # type: ignore


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class SplitResult:
    """Outcome of one split-and-write pass."""
    clean_rows: list[int] = field(default_factory=list)
    quarantined_rows: list[int] = field(default_factory=list)
    flagged_rows: list[int] = field(default_factory=list)
    clean_path: Optional[Path] = None
    quarantine_path: Optional[Path] = None
    flagged_path: Optional[Path] = None
    # row_index -> all decisions that touched that row (in input order)
    row_to_decisions: dict[int, list[Decision]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def split_and_write(
    df: pd.DataFrame,
    decisions: list[Decision],
    run_id: str,
    original_filename: str,
    output_dirs: dict,
) -> SplitResult:
    """
    Bucket rows into clean / quarantine / flagged and write each non-empty
    bucket to its own CSV.

    Args:
        df:                Post-corrector DataFrame (auto-corrections already
                           applied in place by corrector.apply_corrections).
        decisions:         Router output. Each Decision points at one Finding,
                           which carries the row_index.
        run_id:            Active run_id (used in the output filename suffix).
        original_filename: Original source filename, e.g. "demo_07.csv".
                           Used to derive the stem of each output filename.
        output_dirs:       Resolved paths for each bucket. Required keys:
                             "clean", "quarantine", "flagged".

    Returns:
        SplitResult with row-bucket lists, output paths (None where empty),
        and a row_to_decisions map.
    """
    row_to_decisions = _group_by_row(decisions)
    clean_rows, quar_rows, flag_rows = _assign_rows(df, row_to_decisions)

    stem = Path(original_filename).stem
    suffix = (run_id or "norunid")[:8]

    clean_path = _write_bucket(
        df, clean_rows,
        Path(output_dirs["clean"]) / f"{stem}_clean_{suffix}.csv",
    )
    quar_path = _write_bucket(
        df, quar_rows,
        Path(output_dirs["quarantine"]) / f"{stem}_quarantine_{suffix}.csv",
    )
    flag_path = _write_bucket(
        df, flag_rows,
        Path(output_dirs["flagged"]) / f"{stem}_flagged_{suffix}.csv",
    )

    return SplitResult(
        clean_rows=clean_rows,
        quarantined_rows=quar_rows,
        flagged_rows=flag_rows,
        clean_path=clean_path,
        quarantine_path=quar_path,
        flagged_path=flag_path,
        row_to_decisions=row_to_decisions,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _group_by_row(decisions: list[Decision]) -> dict[int, list[Decision]]:
    """row_index -> decisions list, preserving input order per row."""
    grouped: dict[int, list[Decision]] = {}
    for d in decisions:
        grouped.setdefault(d.finding.row_index, []).append(d)
    return grouped


def _assign_rows(
    df: pd.DataFrame,
    row_to_decisions: dict[int, list[Decision]],
) -> tuple[list[int], list[int], list[int]]:
    """Apply precedence QUARANTINE > FLAG > clean over every row of df."""
    clean: list[int] = []
    quar: list[int] = []
    flag: list[int] = []
    for row_idx in df.index:
        ds = row_to_decisions.get(int(row_idx), [])
        actions = {d.action for d in ds}
        if Action.QUARANTINE in actions:
            quar.append(int(row_idx))
        elif Action.FLAG_FOR_REVIEW in actions:
            flag.append(int(row_idx))
        else:
            # No decisions, or only ACCEPT / AUTO_CORRECT
            clean.append(int(row_idx))
    return clean, quar, flag


def _write_bucket(
    df: pd.DataFrame,
    row_indices: list[int],
    out_path: Path,
) -> Optional[Path]:
    """Write the given row subset to CSV. Returns None if the bucket is empty."""
    if not row_indices:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subset = df.loc[row_indices]
    subset.to_csv(out_path, index=False, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.decision.quarantine_handler
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile
    import yaml

    try:
        from src.ingestion.file_loader import load_csv
        from src.validation.schema_validator import validate_schema
        from src.validation.rule_engine import run_rules
        from src.validation.anomaly_detector import detect_cibil_anomalies
        from src.decision.router_agent import route_findings
        from src.decision.corrector import apply_corrections
        from src.audit.audit_logger import AuditLogger
    except ImportError:  # pragma: no cover
        from ingestion.file_loader import load_csv  # type: ignore
        from validation.schema_validator import validate_schema  # type: ignore
        from validation.rule_engine import run_rules  # type: ignore
        from validation.anomaly_detector import detect_cibil_anomalies  # type: ignore
        from decision.router_agent import route_findings  # type: ignore
        from decision.corrector import apply_corrections  # type: ignore
        from audit.audit_logger import AuditLogger  # type: ignore

    rules = yaml.safe_load(Path("config/rules.yaml").read_text())
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    settings_mock = {**settings, "llm": {**settings.get("llm", {}), "enabled": False}}

    expected_cols = rules["schema"]["expected_columns"]
    fp = Path("samples/demo_07_showcase_synthetic.csv")
    if not fp.exists():
        print(f"FAIL: {fp} not found. Run from project root.")
        return 1

    print("=" * 72)
    print("QuarantineHandler self-test  —  end-to-end pipeline on demo_07")
    print("=" * 72)

    # Use tempdir so the real data/ folders stay clean.
    tmp = tempfile.TemporaryDirectory()
    out_dirs = {
        "clean": Path(tmp.name) / "clean",
        "quarantine": Path(tmp.name) / "quarantine",
        "flagged": Path(tmp.name) / "flagged",
    }

    # Tempdir audit DB — quarantine handler doesn't touch audit, but
    # anomaly_detector still requires a logger for LLM-call logging.
    audit = AuditLogger(Path(tmp.name) / "self_test_audit.db")
    run_id = "qh-self-test-12345678"

    load_result = load_csv(fp, expected_cols)
    print(f"  loaded {fp.name}: {load_result.total_rows} rows, "
          f"{len(load_result.dataframe)} well-formed")

    schema_findings = validate_schema(load_result, run_id=run_id)
    rule_findings = run_rules(load_result.dataframe, rules, run_id=run_id)
    anomaly_findings, anomaly_stats = detect_cibil_anomalies(
        df=load_result.dataframe,
        rules_config=rules,
        audit_logger=audit,
        run_id=run_id,
        settings=settings_mock,
    )
    findings = [*schema_findings, *rule_findings, *anomaly_findings]
    print(f"  findings: R001={len(schema_findings)}  "
          f"R002={len(rule_findings)}  R003={len(anomaly_findings)}  "
          f"total={len(findings)}")

    decisions = route_findings(findings, settings_mock)
    print(f"  decisions: {len(decisions)}")

    corrected_df, corrections = apply_corrections(load_result.dataframe, findings)
    print(f"  auto-corrections applied: {len(corrections)}")

    result = split_and_write(
        df=corrected_df,
        decisions=decisions,
        run_id=run_id,
        original_filename=fp.name,
        output_dirs=out_dirs,
    )

    print()
    print("  SplitResult:")
    print(f"    clean_rows       ({len(result.clean_rows):2d}): {result.clean_rows}")
    print(f"    quarantined_rows ({len(result.quarantined_rows):2d}): "
          f"{result.quarantined_rows}")
    print(f"    flagged_rows     ({len(result.flagged_rows):2d}): {result.flagged_rows}")
    print(f"    clean_path:      {result.clean_path}")
    print(f"    quarantine_path: {result.quarantine_path}")
    print(f"    flagged_path:    {result.flagged_path}")

    # ---- invariant 1: every row landed in exactly one bucket ----
    total_in = len(corrected_df)
    total_out = (len(result.clean_rows)
                 + len(result.quarantined_rows)
                 + len(result.flagged_rows))
    print()
    print("  Invariant checks:")
    print(f"    rows in ({total_in}) == rows out ({total_out}): "
          f"{'OK' if total_in == total_out else 'FAIL'}")
    assert total_in == total_out, "row totals must match"
    assert len(set(result.clean_rows)
               & set(result.quarantined_rows)
               & set(result.flagged_rows)) == 0, "buckets must be disjoint"
    print("    buckets disjoint: OK")

    # ---- invariant 2: empty buckets produced no file ----
    for name, rows, path in [
        ("clean", result.clean_rows, result.clean_path),
        ("quarantine", result.quarantined_rows, result.quarantine_path),
        ("flagged", result.flagged_rows, result.flagged_path),
    ]:
        if rows:
            assert path is not None and path.exists(), f"{name} should have a file"
        else:
            assert path is None, f"{name} bucket empty -> path must be None"
    print("    empty buckets -> no file written: OK")

    # ---- invariant 3: auto-corrected values landed in clean output ----
    # demo_07 has R002 trailing-negatives at rows 3 and 4: 2000.00- and 50.00-
    if result.clean_path is not None:
        re_read = pd.read_csv(result.clean_path, dtype=str, keep_default_na=False)
        # Find the two corrected values by their account numbers (rows 3,4)
        amts_in_clean = list(re_read["CIBL_AMT_5"]) + list(re_read["CIBL_AMT_95"])
        has_neg2000 = any(a == "-2000.00" for a in amts_in_clean)
        has_neg50 = any(a == "-50.00" for a in amts_in_clean)
        # Confirm the trailing-minus form did NOT survive
        no_trailing_form = not any(
            a.endswith("-") and a[:-1].replace(".", "", 1).isdigit()
            for a in amts_in_clean
        )
        print(f"    '-2000.00' present in clean output: "
              f"{'OK' if has_neg2000 else 'FAIL'}")
        print(f"    '-50.00' present in clean output:   "
              f"{'OK' if has_neg50 else 'FAIL'}")
        print(f"    no trailing-minus form survived:    "
              f"{'OK' if no_trailing_form else 'FAIL'}")
        assert has_neg2000 and has_neg50 and no_trailing_form

    # ---- invariant 4: flagged rows are exactly the FLAG decisions' rows ----
    flag_rows_from_decisions = sorted({
        d.finding.row_index for d in decisions
        if d.action == Action.FLAG_FOR_REVIEW
    })
    # ...minus any that were upgraded to quarantine on the same row
    quar_rows_from_decisions = {
        d.finding.row_index for d in decisions
        if d.action == Action.QUARANTINE
    }
    expected_flag = [r for r in flag_rows_from_decisions
                    if r not in quar_rows_from_decisions]
    print(f"    flagged rows == FLAG decisions: "
          f"{'OK' if sorted(result.flagged_rows) == expected_flag else 'FAIL'}")
    assert sorted(result.flagged_rows) == expected_flag

    # ---- row_to_decisions is usable by the notifier ----
    print()
    print("  row_to_decisions (for notifier email body):")
    for row_idx in result.flagged_rows:
        ds = result.row_to_decisions[row_idx]
        for d in ds:
            f = d.finding
            print(f"    row {row_idx:2d}  {f.rule_id}  conf={f.confidence:.2f}  "
                  f"raw={f.raw_value!r:30s}  -> {d.action.value}")

    tmp.cleanup()
    print()
    print("Self-test complete. Output files cleaned up.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
