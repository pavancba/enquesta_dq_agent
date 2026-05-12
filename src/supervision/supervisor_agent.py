"""
Supervisor Agent — Layer 4 (Supervision)

Evaluates a finished run against trip-wires defined in
config/settings.yaml -> supervisor. Quarantined rows (data integrity
breaks) and flagged rows (LLM judgments awaiting human review) are
tracked as SEPARATE signals — they have different operational meanings
and should not be mixed into a single "bad-row" ratio.

Trip-wires
----------
  Quarantine signals (data integrity issues):
    * quarantine_ratio > supervisor.max_quarantine_ratio  (default 0.15)
    * quarantined_count > supervisor.max_quarantined_per_file (default 50)

  Flag signals (LLM workload — reviewable, not broken):
    * flag_ratio > supervisor.max_flag_ratio              (default 0.30)
    * flagged_count > supervisor.max_flagged_per_file     (default 30)

Each fired trip-wire escalates status to "elevated".

HITL hold (status = "held_for_hitl")
-----------------------------------
  Triggered when EITHER:
    * quarantine_ratio > supervisor.hitl_quarantine_ratio (default 0.50), OR
    * any quarantined row is present (quarantined_count > 0) AND at least
      one flag trip-wire fires.

  Rationale for the second clause: even a small data-integrity break,
  combined with a heavy LLM review workload on the same run, suggests a
  systemic upstream problem worth pausing for. Flag-only excess (no
  quarantined rows at all) never triggers HITL — those rows are by design
  routed to a human via email, not blocked.

should_notify
-------------
  * Always True if any R003 flagged rows exist — business rule.
  * True if status is "elevated" or "held_for_hitl" AND
    supervisor.notify_on_threshold_breach is true.
  * Otherwise False.

Edge cases
----------
  * total_rows == 0 -> status="ok", both ratios=0, should_notify=False.
  * settings.supervisor missing -> defaults above apply.

Pure function, no I/O. Run this file directly to self-test:
    python -m src.supervision.supervisor_agent
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# Sensible defaults if settings.yaml -> supervisor is missing/partial.
DEFAULT_MAX_QUARANTINE_RATIO = 0.15
DEFAULT_MAX_QUARANTINED_PER_FILE = 50
DEFAULT_MAX_FLAG_RATIO = 0.30
DEFAULT_MAX_FLAGGED_PER_FILE = 30
DEFAULT_HITL_QUARANTINE_RATIO = 0.50
DEFAULT_NOTIFY_ON_BREACH = True

Status = Literal["ok", "elevated", "held_for_hitl"]


@dataclass
class SupervisorVerdict:
    """Run-level verdict produced by the supervisor."""
    status: Status
    reasons: list[str] = field(default_factory=list)
    quarantine_ratio: float = 0.0
    flag_ratio: float = 0.0
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
    Apply quarantine + flag trip-wires to one run's bucket counts.

    Args:
        split_result: Output of quarantine_handler.split_and_write().
                      We only read .quarantined_rows / .flagged_rows.
        total_rows:   Row count seen by the loader for this run.
        settings:     Parsed config/settings.yaml.

    Returns:
        SupervisorVerdict with status, reasons, and should_notify.
    """
    sup_cfg = (settings or {}).get("supervisor") or {}
    max_q_ratio = float(sup_cfg.get(
        "max_quarantine_ratio", DEFAULT_MAX_QUARANTINE_RATIO))
    max_q_count = int(sup_cfg.get(
        "max_quarantined_per_file", DEFAULT_MAX_QUARANTINED_PER_FILE))
    max_f_ratio = float(sup_cfg.get(
        "max_flag_ratio", DEFAULT_MAX_FLAG_RATIO))
    max_f_count = int(sup_cfg.get(
        "max_flagged_per_file", DEFAULT_MAX_FLAGGED_PER_FILE))
    hitl_ratio = float(sup_cfg.get(
        "hitl_quarantine_ratio", DEFAULT_HITL_QUARANTINE_RATIO))
    notify_on_breach = bool(sup_cfg.get(
        "notify_on_threshold_breach", DEFAULT_NOTIFY_ON_BREACH))

    quarantined = len(getattr(split_result, "quarantined_rows", []) or [])
    flagged = len(getattr(split_result, "flagged_rows", []) or [])

    # Empty-file fast path: trip-wires don't fire on zero rows.
    if total_rows <= 0:
        return SupervisorVerdict(
            status="ok",
            reasons=[],
            quarantine_ratio=0.0,
            flag_ratio=0.0,
            quarantined_count=quarantined,
            flagged_count=flagged,
            total_rows=0,
            should_notify=False,
        )

    q_ratio = quarantined / total_rows
    f_ratio = flagged / total_rows

    reasons: list[str] = []
    flag_wire = False

    if q_ratio > max_q_ratio:
        reasons.append(
            f"Quarantine ratio {q_ratio:.2f} exceeds threshold {max_q_ratio:.2f}"
        )
    if quarantined > max_q_count:
        reasons.append(
            f"Quarantined count {quarantined} exceeds ceiling {max_q_count}"
        )
    if f_ratio > max_f_ratio:
        reasons.append(
            f"Flag ratio {f_ratio:.2f} exceeds threshold {max_f_ratio:.2f}"
        )
        flag_wire = True
    if flagged > max_f_count:
        reasons.append(
            f"Flagged count {flagged} exceeds ceiling {max_f_count}"
        )
        flag_wire = True

    # HITL hold:
    #   * catastrophic quarantine ratio, OR
    #   * any quarantine presence combined with a flag trip-wire firing
    #     (even one quarantined row + heavy flag volume = systemic issue).
    if q_ratio > hitl_ratio or (quarantined > 0 and flag_wire):
        status: Status = "held_for_hitl"
    elif reasons:
        status = "elevated"
    else:
        status = "ok"

    # Notify rules:
    #   * Always on any R003 flagged row (business rule — humans must see).
    #   * On status escalation, gated by notify_on_threshold_breach.
    should_notify = False
    if flagged > 0:
        should_notify = True
    elif status != "ok" and notify_on_breach:
        should_notify = True

    return SupervisorVerdict(
        status=status,
        reasons=reasons,
        quarantine_ratio=q_ratio,
        flag_ratio=f_ratio,
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

    sup_cfg = settings.get("supervisor", {})
    print("=" * 72)
    print("SupervisorAgent self-test  —  separate quarantine + flag thresholds")
    print(f"  max_quarantine_ratio:     {sup_cfg.get('max_quarantine_ratio')}")
    print(f"  max_quarantined_per_file: {sup_cfg.get('max_quarantined_per_file')}")
    print(f"  max_flag_ratio:           {sup_cfg.get('max_flag_ratio')}")
    print(f"  max_flagged_per_file:     {sup_cfg.get('max_flagged_per_file')}")
    print(f"  hitl_quarantine_ratio:    {sup_cfg.get('hitl_quarantine_ratio')}")
    print("=" * 72)

    # ---- Test 1: real demo_07 run ---------------------------------------
    print()
    print("  Test 1: real pipeline on demo_07 (0Q / 2F / 10)")
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
    # 0/10 = 0.0 quarantine, 2/10 = 0.20 flag. Both below thresholds.
    assert v1.status == "ok", f"expected ok, got {v1.status}"
    assert v1.should_notify is True, "flagged>0 must force should_notify"
    assert v1.quarantined_count == 0
    assert v1.flagged_count == 2
    assert v1.reasons == []
    print("    PASS")

    # ---- Test 2: 4Q/1F/10 -> elevated (quarantine ratio breach) ---------
    print()
    print("  Test 2: synthetic 4Q / 1F / 10 (40% quarantine, 10% flag)")
    syn = SplitResult(
        clean_rows=list(range(5)),
        quarantined_rows=[5, 6, 7, 8],
        flagged_rows=[9],
    )
    v2 = evaluate_run(syn, total_rows=10, settings=settings)
    _print_verdict(v2)
    # 0.40 quarantine > 0.15 -> wire fires. 0.10 flag < 0.30 -> no flag wire.
    # Quarantine ratio 0.40 < hitl 0.50, and no flag wire -> elevated.
    assert v2.status == "elevated", f"expected elevated, got {v2.status}"
    assert v2.quarantine_ratio == 0.4
    assert v2.flag_ratio == 0.1
    assert v2.should_notify is True
    assert len(v2.reasons) == 1
    print("    PASS")

    # ---- Test 3: 60Q/0F/100 -> held_for_hitl (q ratio > 0.5) ------------
    print()
    print("  Test 3: synthetic 60Q / 0F / 100 (60% quarantine)")
    syn = SplitResult(
        clean_rows=list(range(40)),
        quarantined_rows=list(range(40, 100)),
        flagged_rows=[],
    )
    v3 = evaluate_run(syn, total_rows=100, settings=settings)
    _print_verdict(v3)
    # 0.60 > 0.50 hitl threshold -> held_for_hitl. Also count 60 > 50.
    assert v3.status == "held_for_hitl", f"expected held_for_hitl, got {v3.status}"
    assert v3.should_notify is True  # status != ok and notify_on_breach=True
    assert len(v3.reasons) == 2  # ratio + count both fire
    print("    PASS")

    # ---- Test 4: empty file ---------------------------------------------
    print()
    print("  Test 4: empty file (0 rows)")
    empty = SplitResult()
    v4 = evaluate_run(empty, total_rows=0, settings=settings)
    _print_verdict(v4)
    assert v4.status == "ok"
    assert v4.quarantine_ratio == 0.0
    assert v4.flag_ratio == 0.0
    assert v4.should_notify is False, "empty file must not notify"
    assert v4.reasons == []
    print("    PASS")

    # ---- Test 5: 0Q/40F/100 -> elevated (flag ratio + count) ------------
    print()
    print("  Test 5: synthetic 0Q / 40F / 100 (40% flag)")
    syn = SplitResult(
        clean_rows=list(range(60)),
        quarantined_rows=[],
        flagged_rows=list(range(60, 100)),
    )
    v5 = evaluate_run(syn, total_rows=100, settings=settings)
    _print_verdict(v5)
    # 0.40 flag > 0.30 -> flag ratio wire. Also 40 > 30 count ceiling.
    # No quarantine wire -> NOT held_for_hitl. status -> elevated.
    assert v5.status == "elevated", f"expected elevated, got {v5.status}"
    assert v5.quarantine_ratio == 0.0
    assert v5.flag_ratio == 0.4
    assert v5.should_notify is True
    # Both flag-side wires fire (ratio AND count) but no quarantine wire
    assert len(v5.reasons) == 2
    print("    PASS")

    # ---- Test 6: 5Q/35F/100 -> held_for_hitl (any quar presence + flag wire) -
    print()
    print("  Test 6: synthetic 5Q / 35F / 100 (any quar presence + flag wire)")
    syn = SplitResult(
        clean_rows=list(range(60)),
        quarantined_rows=list(range(60, 65)),
        flagged_rows=list(range(65, 100)),
    )
    v6 = evaluate_run(syn, total_rows=100, settings=settings)
    _print_verdict(v6)
    # quarantined_count=5 > 0 AND flag wires fire (35% > 30% ratio, 35 > 30
    # count) -> HITL hold. Even one quarantined row plus heavy LLM review
    # workload is a signal worth pausing for.
    assert v6.status == "held_for_hitl", f"expected held_for_hitl, got {v6.status}"
    assert v6.quarantine_ratio == 0.05
    assert v6.flag_ratio == 0.35
    assert v6.should_notify is True
    print("    PASS")

    # ---- Test 7: 14Q/0F/100 -> ok (just below quarantine threshold) -----
    print()
    print("  Test 7: synthetic 14Q / 0F / 100 (just below quar threshold)")
    syn = SplitResult(
        clean_rows=list(range(86)),
        quarantined_rows=list(range(86, 100)),
        flagged_rows=[],
    )
    v7 = evaluate_run(syn, total_rows=100, settings=settings)
    _print_verdict(v7)
    # 14% < 15% ratio, 14 < 50 count, no flagged rows -> no wires fire.
    # Even though 14 quarantined rows are present, the HITL "both fire"
    # clause needs a flag wire too — flag wire absent -> not HITL.
    assert v7.status == "ok", f"expected ok, got {v7.status}"
    assert v7.quarantine_ratio == 0.14
    assert v7.flag_ratio == 0.0
    # No flagged rows; status==ok; notify_on_breach moot -> no notification
    assert v7.should_notify is False
    assert v7.reasons == []
    print("    PASS")

    tmp.cleanup()
    print()
    print("All supervisor scenarios passed.")
    return 0


def _print_verdict(v: SupervisorVerdict) -> None:
    print(f"    status:           {v.status}")
    print(f"    quarantine_ratio: {v.quarantine_ratio:.3f}")
    print(f"    flag_ratio:       {v.flag_ratio:.3f}")
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
