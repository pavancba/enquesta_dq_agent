"""
Enquesta Data Quality Agent — Streamlit single-page UI

Run with:
    streamlit run ui/app.py

What this page does:
  1. Loads rules.yaml / settings.yaml once and initializes AuditLogger.
  2. Lets the reviewer drop an Enquesta CSV into the drop zone.
  3. Saves it to data/inbox/ and triggers the agent graph (Module 10).
  4. Streams per-node progress via st.status while the graph runs.
  5. Renders the report (Markdown + metrics + download buttons + email
     inspection) once complete.
  6. Shows a Recent Runs table backed by the audit DB, with drill-in
     for any past run.

Critical: the agent runs at most ONCE per uploaded file. Streamlit
re-runs this script on every UI interaction — we guard against
re-execution by stashing a hash of the uploaded bytes in session_state
and only invoking run_agent when the hash changes.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

# Make src/ importable when launched via `streamlit run ui/app.py`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.audit.audit_logger import AuditLogger  # noqa: E402
from src.audit.report_generator import Report  # noqa: E402
from src.graph.agent_graph import run_agent  # noqa: E402


# ---------------------------------------------------------------------------
# Page config — call once, before anything else renders.
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Enquesta DQ Agent",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Config loading (cached so reruns don't re-parse YAML)
# ---------------------------------------------------------------------------
@st.cache_data
def load_rules() -> dict:
    return yaml.safe_load((_PROJECT_ROOT / "config" / "rules.yaml").read_text())


@st.cache_data
def load_settings() -> dict:
    return yaml.safe_load((_PROJECT_ROOT / "config" / "settings.yaml").read_text())


@st.cache_resource
def get_audit_logger() -> AuditLogger:
    return AuditLogger(_PROJECT_ROOT / "audit.db")


RULES = load_rules()
SETTINGS = load_settings()
AUDIT = get_audit_logger()

INBOX_DIR = _PROJECT_ROOT / "data" / "inbox"
INBOX_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Sidebar — environment summary
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Environment")
    llm_cfg = SETTINGS.get("llm", {}) or {}
    email_cfg = SETTINGS.get("email", {}) or {}
    st.write(f"**Model:** `{llm_cfg.get('model', '?')}`")
    st.write(f"**LLM mode:** "
             f"`{'live' if llm_cfg.get('enabled') else 'mock'}`")
    recipients = email_cfg.get("recipients") or []
    if recipients:
        st.write(f"**Email recipient:** {recipients[0]}")
    else:
        st.write("**Email recipient:** _(none configured)_")
    st.write(f"**Email mode:** `{email_cfg.get('mode', 'smtp')}`")
    st.divider()
    st.caption(
        "Audit DB: `audit.db`  \n"
        "Config: `config/rules.yaml`, `config/settings.yaml`"
    )


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("Enquesta Data Quality Agent")
st.caption("On-prem agentic AI for billing data quality")


# ---------------------------------------------------------------------------
# Drop zone
# ---------------------------------------------------------------------------
uploaded = st.file_uploader(
    "Drag and drop an Enquesta CSV here, or click to browse",
    type=["csv"],
    accept_multiple_files=False,
)


def _bytes_hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _status_color(status: str):
    """Return the matching st.* banner function for a verdict status."""
    if status == "ok":
        return st.success
    if status == "elevated":
        return st.warning
    if status == "held_for_hitl":
        return st.error
    return st.info


def _read_email_body(path_str: str | None) -> str | None:
    """Read an .eml file's text body for the expander."""
    if not path_str:
        return None
    p = Path(path_str)
    if not p.exists():
        return None
    try:
        from email import policy
        from email.parser import BytesParser
        with p.open("rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
        body = msg.get_body(preferencelist=("plain",))
        return body.get_content() if body is not None else p.read_text(errors="replace")
    except Exception as e:
        return f"(could not parse .eml: {e})"


def _run_agent_with_progress(saved_path: Path) -> Report:
    """Invoke run_agent and surface a status block. Returns the Report."""
    # Streamlit 1.39+ supports st.status as a context manager with .update().
    with st.status(f"Processing {saved_path.name}…", expanded=True) as status:
        status.write("• Loading + schema validation")
        status.write("• Rule engine (Rule 2) + anomaly detector (Rule 3)")
        status.write("• Router + corrector + quarantine split")
        status.write("• Supervisor + notifier + report")
        report = run_agent(
            file_path=str(saved_path),
            settings=SETTINGS,
            rules_config=RULES,
            audit_logger=AUDIT,
        )
        status.update(
            label=f"Processing complete — status: {report.verdict['status']}",
            state="complete",
            expanded=False,
        )
    return report


# Session-state cache so reruns don't re-invoke the agent.
if "last_upload_hash" not in st.session_state:
    st.session_state.last_upload_hash = None
if "last_report" not in st.session_state:
    st.session_state.last_report = None
if "last_saved_path" not in st.session_state:
    st.session_state.last_saved_path = None

if uploaded is not None:
    file_bytes = uploaded.getvalue()
    file_hash = _bytes_hash(file_bytes)

    if file_hash != st.session_state.last_upload_hash:
        saved_path = INBOX_DIR / uploaded.name
        saved_path.write_bytes(file_bytes)
        try:
            report = _run_agent_with_progress(saved_path)
            st.session_state.last_upload_hash = file_hash
            st.session_state.last_report = report
            st.session_state.last_saved_path = saved_path
        except Exception as e:
            print(f"[ui] run_agent failed: {e}", file=sys.stderr)
            traceback.print_exc()
            st.error(f"Agent run failed: {e}")
            with st.expander("Full traceback"):
                st.code(traceback.format_exc(), language="text")


# ---------------------------------------------------------------------------
# Result block — render whatever is cached in session_state
# ---------------------------------------------------------------------------
report: Report | None = st.session_state.last_report
if report is not None:
    v = report.verdict
    s = report.summary

    # a) Status banner
    banner_fn = _status_color(v["status"])
    quick = (
        f"{s['flagged']} flagged · {s['quarantined']} quarantined · "
        f"{s['clean']} clean of {s['total_rows']} total rows"
    )
    banner_fn(f"Status: {v['status']} — {quick}")

    # b) Metric tiles
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total rows", s["total_rows"])
    col2.metric("Clean", s["clean"])
    col3.metric(
        "Quarantined",
        s["quarantined"],
        delta=(f"{v['quarantine_ratio'] * 100:.1f}% of file"
               if s["quarantined"] else None),
        delta_color="inverse",
    )
    col4.metric(
        "Flagged",
        s["flagged"],
        delta=(f"{v['flag_ratio'] * 100:.1f}% of file"
               if s["flagged"] else None),
        delta_color="off",
    )

    # c) Full report rendered as Markdown
    st.markdown(report.as_markdown)

    # d) Download buttons — show one per existing output file
    split_paths_cfg = (SETTINGS.get("paths") or {})
    saved_path = st.session_state.last_saved_path
    if saved_path is not None:
        stem = saved_path.stem
        suffix = (report.run_id or "")[:8]
        candidates = [
            ("Download clean.csv",
             Path(split_paths_cfg.get("clean", "data/clean"))
             / f"{stem}_clean_{suffix}.csv",
             "text/csv"),
            ("Download quarantine.csv",
             Path(split_paths_cfg.get("quarantine", "data/quarantine"))
             / f"{stem}_quarantine_{suffix}.csv",
             "text/csv"),
            ("Download flagged.csv",
             Path(split_paths_cfg.get("flagged", "data/flagged"))
             / f"{stem}_flagged_{suffix}.csv",
             "text/csv"),
        ]
        present = [(label, p, mime) for label, p, mime in candidates if p.exists()]
        if present:
            cols = st.columns(len(present))
            for col, (label, p, mime) in zip(cols, present):
                col.download_button(
                    label=label,
                    data=p.read_bytes(),
                    file_name=p.name,
                    mime=mime,
                )

    # e) Email notification expander
    with st.expander("Email notification"):
        notif = report.notification
        st.write(f"**Email mode:** `{notif['email_mode']}`")
        st.write(f"**Email sent:** {notif['email_sent']}")
        if notif.get("error"):
            st.warning(f"Notes: {notif['error']}")
        path = notif.get("email_path")
        recipients = (SETTINGS.get("email") or {}).get("recipients") or []
        if notif["email_mode"] == "mock" and path:
            st.write(f"**.eml file:** `{path}`")
            body = _read_email_body(path)
            if body and st.button("View email body", key="view_eml"):
                st.code(body, language="text")
        elif notif["email_mode"] == "smtp":
            st.write(
                "**Sent to:** "
                + (", ".join(recipients) if recipients else "(none configured)")
            )
        else:
            st.write("**Sent to:** _(skipped — no notification required)_")


# ---------------------------------------------------------------------------
# Audit history
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Recent runs")

recent = AUDIT.list_recent_runs(limit=20)
if not recent:
    st.info("No runs recorded yet. Upload a file above to get started.")
else:
    rows = []
    for r in recent:
        rows.append({
            "run_id": (r.get("run_id") or "")[:8],
            "file_name": r.get("file_name"),
            "started_at": r.get("started_at"),
            "status": r.get("status"),
            "total_rows": r.get("total_rows"),
            "clean": (
                (r.get("total_rows") or 0)
                - (r.get("quarantined") or 0)
                - (r.get("flagged") or 0)
            ),
            "quarantined": r.get("quarantined"),
            "flagged": r.get("flagged"),
        })
    df_recent = pd.DataFrame(rows)

    # Color the status column. Streamlit's Styler renders inline for dataframes.
    def _style_status(val):
        if val == "ok":
            return "background-color: #d4edda; color: #155724"
        if val == "elevated":
            return "background-color: #fff3cd; color: #856404"
        if val == "held_for_hitl":
            return "background-color: #f8d7da; color: #721c24"
        if val == "in_progress":
            return "background-color: #d1ecf1; color: #0c5460"
        return ""

    styled = df_recent.style.map(_style_status, subset=["status"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Drill-in
    choices = {
        f"{r['run_id'][:8]} — {r['file_name']} ({r['status']})": r["run_id"]
        for r in recent
    }
    selected_label = st.selectbox(
        "View details for run:",
        options=["(select a run)"] + list(choices.keys()),
    )
    if selected_label != "(select a run)":
        sel_run_id = choices[selected_label]
        summary = AUDIT.get_run_summary(sel_run_id)
        findings = AUDIT.get_findings_summary(sel_run_id)
        corrections = AUDIT.get_corrections_summary(sel_run_id)
        llm = AUDIT.get_llm_summary(sel_run_id)

        st.markdown(f"### Run `{sel_run_id[:8]}` — {summary.get('file_name')}")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total rows", summary.get("total_rows", 0))
        col2.metric("Auto-corrected", summary.get("auto_corrected", 0))
        col3.metric("Quarantined", summary.get("quarantined", 0))
        col4.metric("Flagged", summary.get("flagged", 0))

        st.write(f"**Status:** `{summary.get('status')}`")
        st.write(f"**Started:** {summary.get('started_at')}")
        st.write(f"**Finished:** {summary.get('finished_at')}")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Findings**")
            if findings:
                st.dataframe(pd.DataFrame(findings),
                             use_container_width=True, hide_index=True)
            else:
                st.caption("_No findings recorded._")
        with c2:
            st.markdown("**Corrections**")
            if corrections:
                st.dataframe(pd.DataFrame(corrections),
                             use_container_width=True, hide_index=True)
            else:
                st.caption("_No corrections recorded._")

        st.markdown("**LLM activity**")
        if llm.get("count", 0) > 0:
            lc1, lc2, lc3 = st.columns(3)
            lc1.metric("LLM calls", llm["count"])
            lc2.metric("Total latency (ms)", llm["total_ms"])
            lc3.metric("Avg latency (ms)", f"{llm['avg_ms']:.0f}")
        else:
            st.caption("_No LLM calls for this run._")

        # Optional drill into raw LLM call rows — kept compact.
        with st.expander("Raw LLM call rows"):
            with sqlite3.connect(_PROJECT_ROOT / "audit.db") as conn:
                conn.row_factory = sqlite3.Row
                raw = [dict(r) for r in conn.execute(
                    "SELECT call_id, model, latency_ms, "
                    "       substr(prompt, 1, 80) AS prompt_preview, "
                    "       substr(response, 1, 120) AS response_preview "
                    "FROM llm_calls WHERE run_id = ? ORDER BY call_id DESC",
                    (sel_run_id,),
                ).fetchall()]
            if raw:
                st.dataframe(pd.DataFrame(raw),
                             use_container_width=True, hide_index=True)
            else:
                st.caption("_No LLM rows for this run._")
