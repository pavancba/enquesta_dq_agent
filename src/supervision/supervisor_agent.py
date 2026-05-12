"""
Supervisor Agent — Layer 4 (Supervision)

Evaluates a finished run's bucket counts against the trip-wires in
config/settings.yaml -> supervisor, and decides whether the run is
clean, elevated, or held for human-in-the-loop review.

Trip-wires
----------
  1. bad_row_ratio   = (quarantined + flagged) / total_rows
     fires when ratio > supervisor.max_bad_row_ratio (default 0.15).
  2. quarantine ceiling
     fires when quarantined_count > supervisor.max_quarantined_per_file
     (default 50).

Status escalation
-----------------
  0 trip-wires fired              -> "ok"
  1 trip-wire fired               -> "elevated"
  2+ fired OR ratio > 0.5         -> "held_for_hitl"

should_notify
-------------
  * Always True if any R003 flagged rows exist — flags trigger an email
    to the billing team per business rule, regardless of escalation.
  * True if status is "elevated" or "held_for_hitl".
  * Otherwise False.

Edge cases
----------
  * Empty file (total_rows == 0) -> status="ok", ratio=0, should_notify=False.
  * Missing settings.supervisor section -> safe defaults are used.

Pure function, no I/O. The agent graph wires the Verdict to the Notifier
and to AuditLogger.finish_run(status=...).

Run this file directly to self-test:
    python -m src.supervision.supervisor_agent
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# Sensible defaults if settings.yaml -> supervisor is missing/partial.
DEFAULT_MAX_BAD_ROW_RATIO = 0.15
DEFAULT_MAX_QUARANTINED_PER_FILE = 50
DEFAULT_NOTIFY_ON_BREACH = True
HITL_RATIO_THRESHOLD = 0.5   # ratio > 0.5 forces held_for_hitl regardless

Status = Literal["ok", "elevated", "held_for_hitl"]


@dataclass
class SupervisorVerdict:
    """Run-level verdict produced by the supervisor."""
    status: Status
    reasons: list[str] = field(default_factory=list)
    bad_row_ratio: float = 0.0
    quarantined_count: int = 0
    flagged_count: int = 0
    total_rows: int = 0
    should_notify: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def evaluate_run(
    split_result,       # SplitResult — typed loosely to keep imports light
    total_rows: int,
    settings: dict,
) -> SupervisorVerdict:
    """
    Apply the trip-wires to one run's bucket counts.

    Args:
        split_result: Output of quarantine_handler.split_and_write().
                      We only read .quarantined_rows / .flagged_rows.
        total_rows:   Row count seen by the loader for this run.
        settings:     Parsed config/settings.yaml.

    Returns:
        SupervisorVerdict with status, reasons, and should_notify.
    """
    sup_cfg = (settings or {}).get("supervisor") or {}
    max_ratio = float(sup_cfg.get("max_bad_row_ratio", DEFAULT_MAX_BAD_ROW_RATIO))
    max_quar = int(sup_cfg.get(
        "max_quarantined_per_file", DEFAULT_MAX_QUARANTINED_PER_FILE,
    ))
    notify_on_breach = bool(sup_cfg.get(
        "notify_on_threshold_breach", DEFAULT_NOTIFY_ON_BREACH,
    ))

    quarantined = len(getattr(split_result, "quarantined_rows", []) or [])
    flagged = len(getattr(split_result, "flagged_rows", []) or [])

    # Empty-file fast path: trip-wires don't fire on zero rows.
    if total_rows <= 0:
        return SupervisorVerdict(
            status="ok",
            reasons=[],
            bad_row_ratio=0.0,
            quarantined_count=quarantined,
            flagged_count=flagged,
            total_rows=0,
            should_notify=False,
        )

    bad = quarantined + flagged
    ratio = bad / total_rows

    reasons: list[str] = []
    if ratio > max_ratio:
        reasons.append(
            f"Bad-row ratio {ratio:.2f} exceeds threshold {max_ratio:.2f}"
        )
    if quarantined > max_quar:
        reasons.append(
            f"Quarantined count {quarantined} exceeds ceiling {max_quar}"
        )

    # Status: hold for HITL when 2+ trip-wires fire OR ratio is catastrophic.
    if len(reasons) >= 2 or ratio > HITL_RATIO_THRESHOLD:
        status: Status = "held_for_hitl"
    elif len(reasons) == 1:
        status = "elevated"
    else:
        status = "ok"

    # Notify rules:
    #  * Always on any R003 flagged row (business rule — humans must see them).
    #  * On status escalation, gated by notify_on_threshold_breach.
    should_notify = False
    if flagged > 0:
        should_notify = True
    elif status != "ok" and notify_on_breach:
        should_notify = True

    return SupervisorVerdict(
        status=status,
        reasons=reasons,
        bad_row_ratio=ratio,
        quarantined_count=quarantined,
        flagged_count=flagged,
        total_rows=total_rows,
        should_notify=should_notify,
    )


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.supervision.supervisor_agent
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
        from src.decision.quarantine_handler import split_and_write, SplitResult
        from src.audit.audit_logger import AuditLogger
    except ImportError:  # pragma: no cover
        from ingestion.file_loader import load_csv  # type: ignore
        from validation.schema_validator import validate_schema  # type: ignore
        from validation.rule_engine import run_rules  # type: ignore
        from validation.anomaly_detector import detect_cibil_anomalies  # type: ignore
        from decision.router_agent import route_findings  # type: ignore
        from decision.corrector import apply_corrections  # type: ignore
        from decision.quarantine_handler import split_and_write, SplitResult  # type: ignore
        from audit.audit_logger import AuditLogger  # type: ignore

    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    rules = yaml.safe_load(Path("config/rules.yaml").read_text())
    settings_mock = {**settings, "llm": {**settings.get("llm", {}), "enabled": False}}

    print("=" * 72)
    print("SupervisorAgent self-test")
    sup_cfg = settings.get("supervisor", {})
    print(f"  max_bad_row_ratio:        "
          f"{sup_cfg.get('max_bad_row_ratio', DEFAULT_MAX_BAD_ROW_RATIO)}")
    print(f"  max_quarantined_per_file: "
          f"{sup_cfg.get('max_quarantined_per_file', DEFAULT_MAX_QUARANTINED_PER_FILE)}")
    print("=" * 72)

    # ---- Test 1: real demo_07 run ---------------------------------------
    print()
    print("  Test 1: real pipeline on demo_07 (8 clean / 0 quar / 2 flagged)")
    expected_cols = rules["schema"]["expected_columns"]
    fp = Path("samples/demo_07_showcase_synthetic.csv")
    tmp = tempfile.TemporaryDirectory()
    audit = AuditLogger(Path(tmp.name) / "sup_audit.db")
    run_id = "sup-self-test-1"

    load_result = load_csv(fp, expected_cols)
    schema_findings = validate_schema(load_result, run_id=run_id)
    rule_findings = run_rules(load_result.dataframe, rules, run_id=run_id)
    anomaly_findings, _ = detect_cibil_anomalies(
        df=load_result.dataframe, rules_config=rules,
        audit_logger=audit, run_id=run_id, settings=settings_mock,
    )
    findings = [*schema_findings, *rule_findings, *anomaly_findings]
    decisions = route_findings(findings, settings_mock)
    corrected_df, _ = apply_corrections(load_result.dataframe, findings)
    split = split_and_write(
        df=corrected_df, decisions=decisions, run_id=run_id,
        original_filename=fp.name,
        output_dirs={
            "clean": Path(tmp.name) / "clean",
            "quarantine": Path(tmp.name) / "quarantine",
            "flagged": Path(tmp.name) / "flagged",
        },
    )
    v1 = evaluate_run(split, total_rows=load_result.total_rows, settings=settings)
    _print_verdict(v1)
    # NOTE: The original task spec asked for status="ok" here, but by the
    # spec's own formula bad_row_ratio = (0 + 2) / 10 = 0.20, which exceeds
    # the 0.15 threshold and trips one wire -> "elevated". The implementation
    # follows the formula; the spec's example expectation was off by one.
    assert v1.status == "elevated", f"expected elevated, got {v1.status}"
    assert v1.should_notify is True, "flagged>0 must force should_notify=True"
    assert v1.quarantined_count == 0
    assert v1.flagged_count == 2
    assert len(v1.reasons) == 1
    print("    PASS")

    # ---- Test 2: synthetic 4 quar / 1 flag / 5 clean -> elevated --------
    print()
    print("  Test 2: synthetic split 4 quar / 1 flag / 5 clean (50% bad)")
    syn = SplitResult(
        clean_rows=list(range(5)),
        quarantined_rows=[5, 6, 7, 8],
        flagged_rows=[9],
    )
    v2 = evaluate_run(syn, total_rows=10, settings=settings)
    _print_verdict(v2)
    # ratio = 0.5 > 0.15 -> 1 trip-wire = elevated (0.5 not > 0.5)
    assert v2.status == "elevated", f"expected elevated, got {v2.status}"
    assert v2.should_notify is True
    assert v2.bad_row_ratio == 0.5
    print("    PASS")

    # ---- Test 3: 60 quar / 0 flag / 40 clean -> held_for_hitl -----------
    print()
    print("  Test 3: synthetic split 60 quar / 0 flag / 40 clean (60% bad)")
    syn = SplitResult(
        clean_rows=list(range(40)),
        quarantined_rows=list(range(40, 100)),
        flagged_rows=[],
    )
    v3 = evaluate_run(syn, total_rows=100, settings=settings)
    _print_verdict(v3)
    # 60/100 = 0.60 > max_ratio AND 60 > max_quar=50 -> 2 trip-wires, also
    # ratio > 0.5 -> held_for_hitl
    assert v3.status == "held_for_hitl", f"expected held_for_hitl, got {v3.status}"
    assert len(v3.reasons) == 2, f"expected 2 reasons, got {len(v3.reasons)}"
    assert v3.should_notify is True
    print("    PASS")

    # ---- Test 4: empty file ---------------------------------------------
    print()
    print("  Test 4: empty file (0 rows)")
    empty = SplitResult()
    v4 = evaluate_run(empty, total_rows=0, settings=settings)
    _print_verdict(v4)
    assert v4.status == "ok"
    assert v4.bad_row_ratio == 0.0
    assert v4.should_notify is False, "empty file must not notify"
    assert v4.reasons == []
    print("    PASS")

    # ---- Test 5: missing supervisor section -> defaults apply -----------
    print()
    print("  Test 5: settings without supervisor section -> defaults")
    bare_settings: dict = {}
    syn = SplitResult(
        clean_rows=list(range(5)),
        quarantined_rows=[5, 6],
        flagged_rows=[],
    )
    v5 = evaluate_run(syn, total_rows=7, settings=bare_settings)
    _print_verdict(v5)
    # ratio = 2/7 = 0.286 > 0.15 default -> elevated
    assert v5.status == "elevated", f"expected elevated, got {v5.status}"
    assert v5.should_notify is True
    print("    PASS")

    tmp.cleanup()
    print()
    print("All 5 supervisor scenarios passed.")
    return 0


def _print_verdict(v: SupervisorVerdict) -> None:
    print(f"    status:           {v.status}")
    print(f"    bad_row_ratio:    {v.bad_row_ratio:.3f}")
    print(f"    quarantined:      {v.quarantined_count}")
    print(f"    flagged:          {v.flagged_count}")
    print(f"    total_rows:       {v.total_rows}")
    print(f"    should_notify:    {v.should_notify}")
    if v.reasons:
        print(f"    reasons:")
        for r in v.reasons:
            print(f"      - {r}")
    else:
        print(f"    reasons:          (none)")


if __name__ == "__main__":
    sys.exit(_self_test())
