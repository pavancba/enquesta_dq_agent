"""
Rule Engine — Layer 2 (Validation)

Runs the deterministic rules defined in config/rules.yaml against a loaded
DataFrame. Currently handles:

  RULE 2 — trailing_negative_amount
    Detects values like "50.00-" or "2000.00-" in CIBL_AMT_5 / CIBL_AMT_95.
    Auto-correctable; the corrector module will move the minus to the front.

  RULE 4 — duplicate_row
    Detects exact-duplicate rows by a configurable set of key columns
    (default: ACCOUNTNUMBER + CIBL_INVOICE + billing date + amounts).
    The FIRST occurrence is treated as the canonical row; every subsequent
    occurrence is flagged for quarantine.

  RULE 5 — value_in_allowed_set
    Detects values that fall outside a configured allow-list. Used for
    ADDR_STATE (must be a valid US state / DC / territory code).

The engine is built rule-table-style so adding future deterministic rules
(class format, etc.) means adding a YAML entry plus one detector function —
no other code changes.

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
# US state-code allow-list for RULE 5. 50 states + DC + 5 inhabited
# territories (PR, VI, GU, AS, MP). Used as the default when a
# value_in_allowed_set rule does not specify its own allowed_values list.
# ---------------------------------------------------------------------------
US_STATE_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
    "PR", "VI", "GU", "AS", "MP",
})


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


def _detect_duplicate_rows(
    df: pd.DataFrame,
    rule_cfg: dict,
    run_id: str,
) -> list[Finding]:
    """RULE 4 — exact-duplicate rows by a set of key columns.

    The first occurrence of any key combination is treated as the canonical
    row and is NOT flagged. Every subsequent occurrence IS flagged so the
    router can quarantine the dupes while letting the original through.
    """
    rule_id = rule_cfg["rule_id"]
    rule_name = rule_cfg["name"]
    severity = Severity(rule_cfg.get("severity", "high"))
    key_cols = rule_cfg.get("key_columns", []) or []

    # Defensive: keep only columns that actually exist in this DataFrame.
    # A misconfigured key list shouldn't crash the engine — it should just
    # narrow the duplicate-match key.
    key_cols_present = [c for c in key_cols if c in df.columns]
    if not key_cols_present:
        return []

    dup_mask = df.duplicated(subset=key_cols_present, keep="first")
    findings: list[Finding] = []
    for row_idx, is_dup in dup_mask.items():
        if not is_dup:
            continue
        summary = " | ".join(
            f"{c}={df.at[row_idx, c]}" for c in key_cols_present
        )
        findings.append(Finding(
            run_id=run_id,
            rule_id=rule_id,
            rule_name=rule_name,
            row_index=int(row_idx),
            column=None,        # row-level rule — no single offending column
            raw_value=summary,
            severity=severity,
            description=(
                f"Duplicate row: matches an earlier row on "
                f"{', '.join(key_cols_present)}. First occurrence kept; "
                f"this copy held for billing review."
            ),
            confidence=1.0,
        ))
    return findings


def _detect_invalid_state_code(
    df: pd.DataFrame,
    rule_cfg: dict,
    run_id: str,
) -> list[Finding]:
    """RULE 5 — value not in a configured allow-list (default: US states)."""
    rule_id = rule_cfg["rule_id"]
    rule_name = rule_cfg["name"]
    severity = Severity(rule_cfg.get("severity", "high"))
    columns = rule_cfg.get("applies_to_columns", ["ADDR_STATE"]) or ["ADDR_STATE"]

    allowed_cfg = rule_cfg.get("allowed_values")
    allowed: frozenset[str]
    if allowed_cfg:
        allowed = frozenset(str(v).strip().upper() for v in allowed_cfg)
    else:
        allowed = US_STATE_CODES

    findings: list[Finding] = []
    for col in columns:
        if col not in df.columns:
            continue
        for row_idx, value in df[col].items():
            sval = "" if value is None else str(value).strip()
            if not sval:
                # Blank values belong to a future missing-fields rule,
                # not to this allow-list check.
                continue
            if sval.upper() in allowed:
                continue
            findings.append(Finding(
                run_id=run_id,
                rule_id=rule_id,
                rule_name=rule_name,
                row_index=int(row_idx),
                column=col,
                raw_value=sval,
                severity=severity,
                description=(
                    f"Invalid {col}: {sval!r} is not a recognized US state "
                    f"/ territory code. Likely an upstream parsing error."
                ),
                confidence=1.0,
            ))
    return findings


# Registry: rule type -> detector function.
# Add a new deterministic rule by registering its type and detector here.
DETECTORS: dict[str, Callable[[pd.DataFrame, dict, str], list[Finding]]] = {
    "format_anomaly":       _detect_trailing_negative,
    "duplicate_row":        _detect_duplicate_rows,
    "value_in_allowed_set": _detect_invalid_state_code,
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
