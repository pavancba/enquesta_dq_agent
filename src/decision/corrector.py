"""
Corrector — Layer 3 (Decision)

Applies deterministic auto-corrections to rows the Router marked AUTO_CORRECT.
Currently handles ONE correction type:

  RULE 2 — Trailing-negative amount (legacy mainframe format)
    Transformation: move trailing minus sign to the front of the number
        "50.00-"    -> "-50.00"
        "2000.00-"  -> "-2000.00"
        "12000.00-" -> "-12000.00"
    Numeric value is unchanged — still negative two thousand etc.
    Only the representation is normalized to standard "leading minus".

The corrector receives findings, applies fixes IN PLACE on the DataFrame
(billing files are small — no memory concern), and returns a list of
CorrectionRecord objects describing every change for the audit logger.

NOT auto-correctable (handled elsewhere):
  - Rule 1 extra-column     -> quarantined, humans decide
  - Rule 3 CIBIL Comment    -> flagged + emailed, humans decide

Run this file directly to self-test:
    python -m src.decision.corrector
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

try:
    from src.models.schemas import Finding
except ImportError:  # pragma: no cover
    from models.schemas import Finding  # type: ignore


@dataclass
class CorrectionRecord:
    """One applied auto-correction. Goes straight to AuditLogger.log_correction()."""
    row_index: int
    column_name: str
    rule_id: str
    value_before: str
    value_after: str


def apply_corrections(
    df: pd.DataFrame,
    findings: list[Finding],
) -> tuple[pd.DataFrame, list[CorrectionRecord]]:
    """
    Apply auto-corrections to the DataFrame for the given findings.

    Args:
        df: DataFrame of well-formed rows.
        findings: Findings to attempt to correct. Only those for which the
                  corrector knows how to fix will actually be corrected.

    Returns:
        (corrected_df, records) — the modified DataFrame and a list of
        CorrectionRecord objects, one per actual change made.
    """
    records: list[CorrectionRecord] = []
    # Defensive copy — never mutate the caller's DataFrame
    df_out = df.copy()

    for finding in findings:
        record = _correct_one(df_out, finding)
        if record is not None:
            records.append(record)

    return df_out, records


def _correct_one(df: pd.DataFrame, finding: Finding) -> CorrectionRecord | None:
    """Apply the right correction for one finding. Returns None if not handled."""
    if finding.rule_id == "R002" and finding.column and finding.raw_value:
        return _correct_trailing_negative(df, finding)
    # Future deterministic corrections register here.
    return None


def _correct_trailing_negative(df: pd.DataFrame, finding: Finding) -> CorrectionRecord:
    """RULE 2 correction: move trailing minus to front."""
    before = finding.raw_value or ""
    after = "-" + before[:-1]   # drop the trailing "-", prepend a new one

    df.at[finding.row_index, finding.column] = after

    return CorrectionRecord(
        row_index=finding.row_index,
        column_name=finding.column,
        rule_id=finding.rule_id,
        value_before=before,
        value_after=after,
    )


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.decision.corrector
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import yaml

    try:
        from src.ingestion.file_loader import load_csv
        from src.validation.rule_engine import run_rules
    except ImportError:
        from ingestion.file_loader import load_csv  # type: ignore
        from validation.rule_engine import run_rules  # type: ignore

    config_path = Path("config/rules.yaml")
    with config_path.open() as f:
        rules = yaml.safe_load(f)
    expected_cols = rules["schema"]["expected_columns"]

    samples = sorted(Path("samples").glob("demo_*.csv"))

    print("=" * 70)
    print(f"Corrector self-test  —  end-to-end pipeline on {len(samples)} files")
    print("=" * 70)
    grand_total = 0
    for fp in samples:
        load_result = load_csv(fp, expected_cols)
        findings = run_rules(load_result.dataframe, rules, run_id="self-test")
        _, records = apply_corrections(load_result.dataframe, findings)
        grand_total += len(records)
        if records:
            print(f"\n  {fp.name}:")
            print(f"    Applied {len(records)} correction(s):")
            for r in records:
                print(f"      row {r.row_index:2d}  {r.column_name}: "
                      f"{r.value_before!r} -> {r.value_after!r}")
        else:
            print(f"  {fp.name:42s}  no corrections needed")
    print()
    print(f"Total corrections applied across all samples: {grand_total}")
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
