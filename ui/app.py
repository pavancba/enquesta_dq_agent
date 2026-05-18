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
    page_title="Enquesta DQ Agent — City of Wilmington",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Visual reskin — CSS only. Streamlit defaults are kept, just overlaid with
# a restrained palette + a handful of utility classes used by the HTML
# blocks below. No external font loads; uses the system font stack.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
      #MainMenu, footer {visibility: hidden;}
      .stApp, body, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                     "Helvetica Neue", Arial, sans-serif;
        color: #1a2332;
      }
      .block-container {padding-top: 1.6rem; max-width: 1280px;}

      .brand-header {
        display: flex; align-items: center; justify-content: space-between;
        padding: 14px 18px; margin-bottom: 18px;
        background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px;
        box-shadow: 0 1px 2px rgba(15,23,42,0.04);
      }
      .brand-left {display: flex; align-items: center; gap: 14px;}
      .brand-logo {
        width: 40px; height: 40px; border-radius: 10px;
        background: #2563eb; color: #fff;
        display: flex; align-items: center; justify-content: center;
        font-weight: 700; font-size: 20px; letter-spacing: -0.5px;
      }
      .brand-title {font-size: 20px; font-weight: 600; line-height: 1.1;}
      .brand-sub {font-size: 13px; color: #64748b; margin-top: 2px;}
      .user-pill {display: flex; align-items: center; gap: 10px;}
      .user-avatar {
        width: 36px; height: 36px; border-radius: 999px;
        background: #1a2332; color: #fff;
        display: flex; align-items: center; justify-content: center;
        font-weight: 600; font-size: 13px;
      }
      .user-name {font-size: 13px; font-weight: 600;}
      .user-role {font-size: 12px; color: #64748b;}

      .file-strip {
        font-size: 14px; margin: 6px 0 12px 0;
        color: #1a2332;
      }
      .file-strip b {font-weight: 600;}
      .file-strip .muted {color: #64748b; font-weight: 400;}

      .status-banner {
        display: flex; align-items: flex-start; gap: 12px;
        padding: 14px 16px; border-radius: 10px; margin-bottom: 18px;
        border: 1px solid transparent;
      }
      .status-banner .check {
        width: 22px; height: 22px; border-radius: 999px;
        display: flex; align-items: center; justify-content: center;
        font-size: 13px; font-weight: 700; flex-shrink: 0;
        margin-top: 1px;
      }
      .status-banner .body {flex: 1;}
      .status-banner .title {font-weight: 600; font-size: 14px;}
      .status-banner .desc {font-size: 13px; margin-top: 2px;}
      .status-banner.ok       {background: #ecfdf5; border-color: #a7f3d0;}
      .status-banner.ok .check    {background: #16a34a; color: #fff;}
      .status-banner.ok .title    {color: #065f46;}
      .status-banner.ok .desc     {color: #047857;}
      .status-banner.elevated {background: #fffbeb; border-color: #fde68a;}
      .status-banner.elevated .check  {background: #d97706; color: #fff;}
      .status-banner.elevated .title  {color: #92400e;}
      .status-banner.elevated .desc   {color: #b45309;}
      .status-banner.held     {background: #fef2f2; border-color: #fecaca;}
      .status-banner.held .check  {background: #dc2626; color: #fff;}
      .status-banner.held .title  {color: #991b1b;}
      .status-banner.held .desc   {color: #b91c1c;}

      .tier-card {
        background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px;
        padding: 16px 18px; height: 100%;
        border-top: 3px solid #cbd5e1;
        box-shadow: 0 1px 2px rgba(15,23,42,0.04);
      }
      .tier-card.tier-1       {border-top-color: #2563eb;}
      .tier-card.tier-2       {border-top-color: #d97706;}
      .tier-card.tier-anomaly {border-top-color: #dc2626;}
      .tier-card .label {
        font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
        color: #64748b; font-weight: 600;
      }
      .tier-card .value {
        font-size: 32px; font-weight: 700; line-height: 1.1;
        margin-top: 4px; color: #1a2332;
      }
      .tier-card .sub {font-size: 13px; color: #64748b; margin-top: 4px;}

      .audit-strip {
        background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 10px;
        padding: 10px 14px; margin: 18px 0;
        font-size: 13px; color: #475569;
      }
      .audit-strip b {color: #1a2332; font-weight: 600;}

      .section-title {
        font-size: 15px; font-weight: 600; color: #1a2332;
        margin: 20px 0 8px 0;
      }
      .section-title .count {color: #64748b; font-weight: 400;}
    </style>
    """,
    unsafe_allow_html=True,
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

    st.subheader("Inbox watcher")
    st.toggle(
        "Auto-process files dropped in inbox",
        key="watch_enabled",
        value=st.session_state.get("watch_enabled", False),
        help=(
            "When on, any new CSV dropped into the inbox folder is "
            "automatically picked up and processed by the agent. The "
            "page polls every 3 seconds; each file is processed only "
            "once per session."
        ),
    )
    st.caption(f"Inbox: `{INBOX_DIR.relative_to(_PROJECT_ROOT)}`")
    _inbox_csvs = sorted(INBOX_DIR.glob("*.csv"))
    if _inbox_csvs:
        st.markdown(
            "\n".join(f"- `{p.name}`" for p in _inbox_csvs)
        )
    else:
        st.markdown("_Inbox is empty._")

    st.divider()
    st.caption(
        "Audit DB: `audit.db`  \n"
        "Config: `config/rules.yaml`, `config/settings.yaml`"
    )


# ---------------------------------------------------------------------------
# Brand header — left: E logo + title + subtitle.
#                right: PJ avatar + "P. Jagga · Billing Lead".
# ---------------------------------------------------------------------------
st.markdown(
    """
    <div class="brand-header">
      <div class="brand-left">
        <div class="brand-logo">E</div>
        <div>
          <div class="brand-title">Enquesta DQ Agent</div>
          <div class="brand-sub">City of Wilmington · Billing Ops</div>
        </div>
      </div>
      <div class="user-pill">
        <div>
          <div class="user-name" style="text-align:right;">P. Jagga</div>
          <div class="user-role" style="text-align:right;">Billing Lead</div>
        </div>
        <div class="user-avatar">PJ</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Top nav — three tabs. All existing page content lives in "Daily Run";
# the other two are placeholders for the roadmap.
# ---------------------------------------------------------------------------
tab_daily, tab_health, tab_quality = st.tabs(
    ["Daily Run", "Pipeline Health", "Data Quality"]
)


with tab_health:
    st.info("Coming soon — Layer 2 / Layer 3 of the roadmap.")
with tab_quality:
    st.info("Coming soon — Layer 2 / Layer 3 of the roadmap.")


# ---------------------------------------------------------------------------
# Helpers — unchanged from the previous revision. The reskin only touches
# the visual presentation; these utilities stay as-is so the agent flow,
# session-state guard, and inbox watcher behave identically.
# ---------------------------------------------------------------------------
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
if "processed_inbox_hashes" not in st.session_state:
    st.session_state.processed_inbox_hashes = set()


def _scan_inbox_and_process() -> None:
    """Scan INBOX_DIR for unseen CSVs and run the agent on each."""
    for path in sorted(INBOX_DIR.glob("*.csv")):
        try:
            content = path.read_bytes()
        except OSError:
            # File may be mid-write; skip and retry on next refresh.
            continue

        key = (str(path), _bytes_hash(content))
        if key in st.session_state.processed_inbox_hashes:
            continue

        st.info(f"New file detected in inbox: `{path.name}` — processing…")
        try:
            report = _run_agent_with_progress(path)
            st.session_state.processed_inbox_hashes.add(key)
            st.session_state.last_upload_hash = key[1]
            st.session_state.last_report = report
            st.session_state.last_saved_path = path
            v = report.verdict
            s = report.summary
            st.success(
                f"Processed `{path.name}` — status: {v['status']} · "
                f"{s['flagged']} flagged · {s['quarantined']} quarantined · "
                f"{s['clean']} clean of {s['total_rows']} rows"
            )
        except Exception as e:
            print(f"[ui] inbox watch run_agent failed for {path}: {e}",
                  file=sys.stderr)
            traceback.print_exc()
            st.error(f"Agent run failed for `{path.name}`: {e}")
            with st.expander(f"Full traceback — {path.name}"):
                st.code(traceback.format_exc(), language="text")
            # Mark as processed so we don't infinite-loop on a broken file.
            st.session_state.processed_inbox_hashes.add(key)


# Inbox watcher + auto-refresh — run OUTSIDE the tabs so it fires
# regardless of which tab the reviewer is currently looking at.
if st.session_state.get("watch_enabled"):
    _scan_inbox_and_process()
    if st.session_state.get("last_report") is None:
        # Only poll while we have nothing to show. Once a result is
        # on screen, stop reloading so the user can actually read it.
        st.markdown(
            '<meta http-equiv="refresh" content="3">',
            unsafe_allow_html=True,
        )
        st.caption("👀 Watch mode is ON — scanning inbox every 3s…")
    else:
        st.caption(
            "👀 Watch mode is ON — paused on current result. "
            "Toggle off and back on to resume scanning."
        )


# ---------------------------------------------------------------------------
# Daily Run tab — uploader, agent invocation, result rendering, recent runs.
# ---------------------------------------------------------------------------
with tab_daily:
    uploaded = st.file_uploader(
        "Drag and drop an Enquesta CSV here, or click to browse",
        type=["csv"],
        accept_multiple_files=False,
    )

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

    # -----------------------------------------------------------------------
    # Result block — render whatever is cached in session_state.
    # Visual hierarchy (per Vercel reference):
    #   1) file-name strip   2) status banner   3) three tier cards
    #   4) audit-log strip   5) quarantine table   6) corrections table
    #   7) download buttons   8) email expander
    # -----------------------------------------------------------------------
    report: Report | None = st.session_state.last_report
    if report is not None:
        v = report.verdict
        s = report.summary

        auto_count = sum(int(c.get("count", 0)) for c in report.corrections_summary)
        quar_count = int(s.get("quarantined", 0))
        flag_count = int(s.get("flagged", 0))
        clean_count = int(s.get("clean", 0))
        total_count = int(s.get("total_rows", 0))

        # 1) File-name strip
        started_at = s.get("started_at") or ""
        st.markdown(
            f'<div class="file-strip"><b>{report.file_name}</b> '
            f'<span class="muted">· arrived {started_at} · '
            f'processed in {report.duration_seconds:.2f}s</span></div>',
            unsafe_allow_html=True,
        )

        # 2) Status banner — green / amber / red depending on verdict
        status = v.get("status") or "ok"
        if status == "ok":
            banner_cls, banner_label, check_glyph = (
                "ok", "Processed", "✓"
            )
        elif status == "elevated":
            banner_cls, banner_label, check_glyph = (
                "elevated", "Elevated — review recommended", "!"
            )
        elif status == "held_for_hitl":
            banner_cls, banner_label, check_glyph = (
                "held", "Held for human-in-the-loop review", "!"
            )
        else:
            banner_cls, banner_label, check_glyph = (
                "ok", f"Status: {status}", "·"
            )
        one_liner = (
            f"{total_count} rows in → {auto_count} auto-corrected · "
            f"{quar_count} quarantined · {flag_count} flagged · "
            f"{clean_count} clean rows posted"
        )
        st.markdown(
            f'<div class="status-banner {banner_cls}">'
            f'  <div class="check">{check_glyph}</div>'
            f'  <div class="body">'
            f'    <div class="title">{banner_label} — File received and validated</div>'
            f'    <div class="desc">{one_liner}</div>'
            f'  </div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # 3) Three tier cards in a row
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(
                f'<div class="tier-card tier-1">'
                f'  <div class="label">Tier 1 — Auto-corrected</div>'
                f'  <div class="value">{auto_count}</div>'
                f'  <div class="sub">Trailing-sign fixes</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(
                f'<div class="tier-card tier-2">'
                f'  <div class="label">Tier 2 — Quarantined</div>'
                f'  <div class="value">{quar_count}</div>'
                f'  <div class="sub">Held for billing review</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with c3:
            st.markdown(
                f'<div class="tier-card tier-anomaly">'
                f'  <div class="label">Anomalies flagged</div>'
                f'  <div class="value">{flag_count}</div>'
                f'  <div class="sub">Routed for human review</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # 4) Audit-log strip — one horizontal row
        run_id_short = (report.run_id or "")[:24]
        st.markdown(
            f'<div class="audit-strip">'
            f'<b>Audit log</b> · Run ID <b>run_{run_id_short}</b> · '
            f'{auto_count} corrections logged · {quar_count} holds · '
            f'{flag_count} escalations · every action signed and timestamped'
            f'</div>',
            unsafe_allow_html=True,
        )

        # 5) Quarantined rows table — only when there is something to show
        split_paths_cfg = (SETTINGS.get("paths") or {})
        saved_path = st.session_state.last_saved_path
        run_suffix = (report.run_id or "")[:8]

        if quar_count > 0 and saved_path is not None:
            quar_csv = (
                Path(split_paths_cfg.get("quarantine", "data/quarantine"))
                / f"{saved_path.stem}_quarantine_{run_suffix}.csv"
            )
            st.markdown(
                f'<div class="section-title">Quarantined rows '
                f'<span class="count">— {quar_count} awaiting review</span></div>',
                unsafe_allow_html=True,
            )
            if quar_csv.exists():
                try:
                    df_quar = pd.read_csv(quar_csv, dtype=str, keep_default_na=False)
                    st.dataframe(df_quar, use_container_width=True, hide_index=True)
                except Exception as e:
                    st.caption(f"_Could not read quarantine CSV ({e})._")
            else:
                st.caption(f"_Quarantine file not found at_ `{quar_csv}`")

        # 6) Auto-corrections table — only when corrections exist for this run
        if auto_count > 0:
            with sqlite3.connect(_PROJECT_ROOT / "audit.db") as conn:
                conn.row_factory = sqlite3.Row
                rows_corr = [dict(r) for r in conn.execute(
                    "SELECT rule_id, row_index, column_name, "
                    "       value_before, value_after "
                    "FROM corrections WHERE run_id = ? "
                    "ORDER BY row_index, column_name",
                    (report.run_id,),
                ).fetchall()]
            st.markdown(
                f'<div class="section-title">Auto-corrections applied '
                f'<span class="count">— {auto_count} fixes</span></div>',
                unsafe_allow_html=True,
            )
            if rows_corr:
                st.dataframe(
                    pd.DataFrame(rows_corr),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("_No correction rows recorded for this run._")

        # 7) Download buttons — one per existing output file (unchanged)
        if saved_path is not None:
            stem = saved_path.stem
            candidates = [
                ("Download clean.csv",
                 Path(split_paths_cfg.get("clean", "data/clean"))
                 / f"{stem}_clean_{run_suffix}.csv",
                 "text/csv"),
                ("Download quarantine.csv",
                 Path(split_paths_cfg.get("quarantine", "data/quarantine"))
                 / f"{stem}_quarantine_{run_suffix}.csv",
                 "text/csv"),
                ("Download flagged.csv",
                 Path(split_paths_cfg.get("flagged", "data/flagged"))
                 / f"{stem}_flagged_{run_suffix}.csv",
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

        # 8) Email notification expander (unchanged behavior)
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

    # -----------------------------------------------------------------------
    # Audit history — unchanged from the previous revision.
    # -----------------------------------------------------------------------
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

        # Color the status column. Streamlit's Styler renders inline.
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
