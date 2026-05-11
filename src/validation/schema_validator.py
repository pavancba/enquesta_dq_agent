"""
Schema Validator — Layer 2 (Validation)

Implements Rule 1 (column-count anomalies).

Takes a LoadResult from file_loader and produces Finding objects for each
row whose column count doesn't match the schema.

Crucially distinguishes between two real-world cases:

  * TRAILING_COMMA  — row ends with an empty cell (96% of "extra column"
                      cases in production). Cosmetic only. Auto-correctable
                      by dropping the empty trailing cell. Produces a
                      Finding so the audit log records what happened, but
                      severity is LOW.

  * REAL_EXTRA      — row has more cells than expected with non-empty content
                      in the overflow (4% of cases — typically a comma in
                      ADDR_ADDRESS_1). NOT auto-correctable. Severity HIGH.
                      Action: quarantine.

  * FEWER           — row has fewer cells than expected. Missing field(s).
                      NOT auto-correctable. Severity HIGH. Action: quarantine.

Empty/header-only files don't produce a Finding here — they're handled
upstream by the agent graph as a "skipped, logged" outcome.

Run this file directly to self-test:
    python -m src.validation.schema_validator
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from src.ingestion.file_loader import LoadResult, RowShape, load_csv
    from src.models.schemas import Finding, Severity
except ImportError:  # pragma: no cover
    from ingestion.file_loader import LoadResult, RowShape, load_csv  # type: ignore
    from models.schemas import Finding, Severity  # type: ignore

if TYPE_CHECKING:
    from src.ingestion.file_loader import RowAnomaly


# Rule 1 metadata — kept here (not in YAML) because it's an intrinsic schema
# rule, not a tunable data rule. Editable in one place if needed.
RULE_ID = "R001"
RULE_NAME = "schema_integrity"


def validate_schema(load_result: LoadResult, run_id: str) -> list[Finding]:
    """
    Produce Finding objects for any rows whose column count doesn't match.

    Args:
        load_result: Output from file_loader.load_csv().
        run_id: Active run_id from the audit logger.

    Returns:
        List of Findings, possibly empty.
    """
    findings: list[Finding] = []
    for anomaly in load_result.anomalies:
        finding = _finding_for_anomaly(anomaly, run_id, load_result)
        if finding is not None:
            findings.append(finding)
    return findings


def _finding_for_anomaly(anomaly, run_id: str, lr: LoadResult) -> Finding | None:
    """Map one RowAnomaly to one Finding. Returns None if no finding warranted."""
    expected = lr.expected_column_count

    if anomaly.shape == RowShape.TRAILING_COMMA:
        return Finding(
            run_id=run_id,
            rule_id=RULE_ID,
            rule_name="trailing_comma_cosmetic",
            row_index=anomaly.row_index,
            column=None,
            raw_value=anomaly.raw_line[:200],   # truncate noisy long lines
            severity=Severity.LOW,
            description=(
                f"Row has {anomaly.column_count} columns (expected {expected}) "
                "but the extra cell is empty — cosmetic trailing comma. "
                "Auto-corrected by dropping the empty cell."
            ),
            confidence=1.0,
        )

    if anomaly.shape == RowShape.REAL_EXTRA:
        return Finding(
            run_id=run_id,
            rule_id=RULE_ID,
            rule_name="extra_comma_breaks_schema",
            row_index=anomaly.row_index,
            column=None,
            raw_value=anomaly.raw_line[:200],
            severity=Severity.HIGH,
            description=(
                f"Row has {anomaly.column_count} columns (expected {expected}). "
                "Likely an embedded comma inside a data value (commonly the "
                "address field). Field positions are no longer reliable — row "
                "quarantined for human review."
            ),
            confidence=1.0,
        )

    if anomaly.shape == RowShape.FEWER:
        return Finding(
            run_id=run_id,
            rule_id=RULE_ID,
            rule_name="fewer_columns_missing_fields",
            row_index=anomaly.row_index,
            column=None,
            raw_value=anomaly.raw_line[:200],
            severity=Severity.HIGH,
            description=(
                f"Row has only {anomaly.column_count} columns (expected {expected}). "
                "One or more fields are missing — row quarantined for review."
            ),
            confidence=1.0,
        )

    return None  # NORMAL or EMPTY shapes don't produce findings


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.validation.schema_validator
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import yaml

    config_path = Path("config/rules.yaml")
    if not config_path.exists():
        print(f"FAIL: {config_path} not found. Run from project root.")
        return 1
    with config_path.open() as f:
        rules = yaml.safe_load(f)
    expected_cols = rules["schema"]["expected_columns"]

    samples = sorted(Path("samples").glob("demo_*.csv"))

    print("=" * 70)
    print(f"SchemaValidator self-test  —  scanning {len(samples)} sample files")
    print("=" * 70)
    total_findings = 0
    for fp in samples:
        load_result = load_csv(fp, expected_cols)
        findings = validate_schema(load_result, run_id="self-test-run")
        total_findings += len(findings)
        flag = " <-- findings!" if findings else ""
        empty_note = " (empty file, skipped cleanly)" if load_result.is_empty else ""
        print(f"  {fp.name:42s}  rows={load_result.total_rows:3d}  "
              f"findings={len(findings)}{flag}{empty_note}")
        for f in findings:
            sev = f.severity.value
            print(f"      [{sev:6s}] row {f.row_index}: {f.rule_name}")
            print(f"               {f.description[:100]}")
    print()
    print(f"Total findings across all samples: {total_findings}")
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
