"""
Rule Engine — Layer 2 (Validation)

Runs the deterministic rules defined in config/rules.yaml against a loaded
DataFrame. Currently handles:

  RULE 2 — trailing_negative_amount
    Detects values like "50.00-" or "2000.00-" in CIBL_AMT_5 / CIBL_AMT_95.
    Auto-correctable; the corrector module will move the minus to the front.

The engine is built rule-table-style so adding future deterministic rules
(state codes, class format, etc.) means adding a YAML entry plus one
detector function — no other code changes.

Distinct from anomaly_detector.py: rules here are pure regex/lookup,
no LLM, no judgment. Fast, cheap, 100% reproducible.

Run this file directly to self-test:
    python -m src.validation.rule_engine
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

import pandas as pd

try:
    from src.models.schemas import Finding, Severity
except ImportError:  # pragma: no cover
    from models.schemas import Finding, Severity  # type: ignore


# ---------------------------------------------------------------------------
# Compiled regex patterns. These are used both by detection and by the
# corrector — keeping them in one place keeps them in sync.
# ---------------------------------------------------------------------------
TRAILING_NEG_RE = re.compile(r'^\d+(\.\d+)?-$')


# ---------------------------------------------------------------------------
# Detectors — one function per rule. Each takes (df, rule_cfg, run_id) and
# returns a list of Findings.
# ---------------------------------------------------------------------------
def _detect_trailing_negative(
    df: pd.DataFrame,
    rule_cfg: dict,
    run_id: str,
) -> list[Finding]:
    """RULE 2 — trailing-negative amount values in monetary columns."""
    rule_id = rule_cfg["rule_id"]
    rule_name = rule_cfg["name"]
    columns = rule_cfg.get("applies_to_columns", [])
    severity_str = rule_cfg.get("severity", "medium")
    severity = Severity(severity_str)

    findings: list[Finding] = []
    for col in columns:
        if col not in df.columns:
            continue
        for row_idx, value in df[col].items():
            sval = "" if value is None else str(value).strip()
            if TRAILING_NEG_RE.match(sval):
                corrected = "-" + sval[:-1]
                findings.append(Finding(
                    run_id=run_id,
                    rule_id=rule_id,
                    rule_name=rule_name,
                    row_index=int(row_idx),
                    column=col,
                    raw_value=sval,
                    severity=severity,
                    description=(
                        f"Trailing-negative amount in {col}: {sval!r} should "
                        f"be {corrected!r} (legacy mainframe format)."
                    ),
                    confidence=1.0,
                ))
    return findings


# Registry: rule type -> detector function.
# Add a new deterministic rule by registering its type and detector here.
DETECTORS: dict[str, Callable[[pd.DataFrame, dict, str], list[Finding]]] = {
    "format_anomaly": _detect_trailing_negative,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_rules(
    df: pd.DataFrame,
    rules_config: dict,
    run_id: str,
) -> list[Finding]:
    """
    Run all deterministic rules from rules.yaml against the DataFrame.

    Args:
        df: DataFrame of well-formed rows (output of file_loader).
        rules_config: Parsed config/rules.yaml.
        run_id: Active run_id from the audit logger.

    Returns:
        Aggregated list of Findings from all applicable deterministic rules.
        LLM-judgment rules (type=llm_judgment) are skipped here — they're
        handled by anomaly_detector.
    """
    findings: list[Finding] = []
    for rule_cfg in rules_config.get("rules", []):
        rule_type = rule_cfg.get("type")
        detector = DETECTORS.get(rule_type)
        if detector is None:
            # Either an LLM rule (handled elsewhere) or a schema rule
            # (handled by schema_validator). Either way: skip.
            continue
        findings.extend(detector(df, rule_cfg, run_id))
    return findings


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.validation.rule_engine
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import yaml

    try:
        from src.ingestion.file_loader import load_csv
    except ImportError:
        from ingestion.file_loader import load_csv  # type: ignore

    config_path = Path("config/rules.yaml")
    with config_path.open() as f:
        rules = yaml.safe_load(f)
    expected_cols = rules["schema"]["expected_columns"]

    samples = sorted(Path("samples").glob("demo_*.csv"))

    print("=" * 70)
    print(f"RuleEngine self-test  —  scanning {len(samples)} sample files")
    print("=" * 70)
    total = 0
    for fp in samples:
        load_result = load_csv(fp, expected_cols)
        findings = run_rules(load_result.dataframe, rules, run_id="self-test")
        total += len(findings)
        flag = " <-- Rule 2 hits!" if findings else ""
        print(f"  {fp.name:42s}  rows={load_result.total_rows:3d}  "
              f"findings={len(findings)}{flag}")
        for f in findings:
            corrected = "-" + f.raw_value[:-1] if f.raw_value else "?"
            print(f"      row {f.row_index:2d}  {f.column}: "
                  f"{f.raw_value!r} -> {corrected!r}")
    print()
    print(f"Total Rule 2 findings across all samples: {total}")
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
