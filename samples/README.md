# Demo dataset

Seven curated CSV files for demonstrating the Enquesta Data Quality Agent.

Six are **real production files** picked from a 206-file archive (Jun 2025 – May 2026).
One is a **synthetic showcase** that triggers every rule in one file.

Recommended demo order: drop them into `data/inbox/` from top to bottom, so the
reviewer experiences the agent's capability building up.

---

## `demo_01_clean.csv` — the "happy path"

| Property | Value |
|---|---|
| Origin | Real: `LIbillprint01262026.182512.csv` |
| Rows | 18 |
| Issues | None — completely clean |
| Agent says | "18 rows processed, 0 issues, all clean" |

**Why first:** establishes that the agent isn't a false-alarm machine. Most files
*are* clean; the agent only acts when needed.

---

## `demo_02_rule2_only.csv` — auto-correction in action

| Property | Value |
|---|---|
| Origin | Real: `LIbillprint03022026.182525.csv` |
| Rows | 16 |
| Issues | 1× trailing-negative amount (`CIBL_AMT_5 = "50.00-"` on line 2) |
| Agent says | "16 rows processed, 1 auto-corrected (`50.00-` → `-50.00`), 0 flagged" |

**Why second:** shows **Rule 2** — the demo-friendly auto-correction. The reviewer
sees the agent doing real work without bothering humans.

---

## `demo_03_rule3_freeform.csv` — LLM judgment + email alert

| Property | Value |
|---|---|
| Origin | Real: `LIbillprint01052026.182456.csv` |
| Rows | 11 |
| Issues | 2× freeform CIBIL Comments (`VP BILL #: 27007839`, `VP BILL #: 27014042`) <br> 2× trailing-negative amounts (auto-corrected) |
| Agent says | "11 rows processed, 2 auto-corrected, 2 flagged + emailed to billing team" |

**Why third:** shows **Rule 3** — the LLM-judgment showpiece. After processing,
an email is sent to `pavan.gali@accelance.io` with the LLM's reasoning for each
flagged row. This is the "agentic AI" moment.

---

## `demo_04_multi_issue.csv` — multiple rules firing together

| Property | Value |
|---|---|
| Origin | Real: `LIbillprint01062026.182524.csv` |
| Rows | 10 |
| Issues | 3× trailing-negative amounts <br> 1× duplicate row (account `000228357200`, invoice `28401044` appears twice) |
| Agent says | "10 rows processed, 3 auto-corrected, 1 duplicate flagged" |

**Why fourth:** shows the agent handling **multiple rule types in one file** with
distinct outcomes per finding. The duplicate isn't fixed automatically — it's
quarantined because billing the same customer twice is a real harm only humans
should resolve.

> **Note:** duplicates are in the rules.yaml roadmap (Rule 4) but **not enabled
> in the v1 demo**. The agent will detect them via anomaly detection in v1 and
> handle them deterministically in v1.1.

---

## `demo_05_edge_empty.csv` — graceful edge-case handling

| Property | Value |
|---|---|
| Origin | Real: `LIbillprint02272026.124817.csv` |
| Rows | 0 (zero-byte file) |
| Issues | No data to process |
| Agent says | "Empty file, skipped. Logged to audit DB." |

**Why fifth:** shows the agent is **defensive, not brittle**. 6% of production
files in the archive are empty or header-only. A naive script would crash; the
agent logs and moves on.

---

## `demo_06_duplicates.csv` — duplicates without other noise

| Property | Value |
|---|---|
| Origin | Real: `07162025.210414.csv` |
| Rows | 16 |
| Issues | 2× duplicate row pairs (accounts `000221460200` and `000221523200`) |
| Agent says | "16 rows processed, 2 duplicates flagged for review" |

**Why sixth:** focused demonstration of duplicate detection without confounding
issues. Useful for the conversation "what about duplicate detection?" because
duplicates are the **2nd most frequent** issue in the production archive (36%
of files have at least one).

---

## `demo_07_showcase_synthetic.csv` — every rule, one file

| Property | Value |
|---|---|
| Origin | Synthetic — built deliberately for the demo |
| Rows | 10 |

Each row demonstrates one specific scenario:

| Line | Row content | Triggers | Action |
|---|---|---|---|
| 2 | `IT26000001`, normal amount | — | ACCEPT |
| 3 | `VO-20001`, normal amount | — | ACCEPT |
| 4 | `PM-30001`, normal amount | — | ACCEPT |
| 5 | `IT26000004`, amount `2000.00-` | **Rule 2** | AUTO-CORRECT |
| 6 | `VAC-40001`, amount `50.00-` | **Rule 2** | AUTO-CORRECT |
| 7 | `VP BILL #: 27099999`, normal amount | **Rule 3** | FLAG + EMAIL |
| 8 | `penalty removal` (freeform English), normal amount | **Rule 3** | FLAG + EMAIL |
| 9 | `IT26000008`, normal amount | — | ACCEPT |
| 10 | Same as line 9 (exact duplicate) | **Rule 4** (roadmap) | LLM anomaly flag |
| 11 | `IT26000010`, normal amount | — | ACCEPT |

**Expected agent output:** 10 rows processed, 2 auto-corrected, 2 flagged with
email, 1 anomaly noted. A single file proves every code path in the agent.

**Why last:** the strong finale. After seeing the agent handle six real files
naturally, the synthetic file lets you show "and here's everything together,
in a controlled scenario."

---

## Statistics behind these picks

The 6 real files were chosen from a 206-file archive containing 4,457 rows over
316 days of production data:

- Trailing-negative amounts → 48% of files affected
- Internal duplicate rows → 36% of files affected
- Freeform CIBIL Comments → 20% of files affected
- Empty / header-only files → 6% of files

Issue frequencies match what the demo files exercise.
