"""
Supervisor Agent — Layer 4 (Supervision)

Watches the run-level metrics and decides if human intervention
is needed. Specifically:
  - If bad-row ratio exceeds settings.supervisor.max_bad_row_ratio
  - If quarantined count exceeds settings.supervisor.max_quarantined_per_file
  - If repeated failures suggest an upstream Enquesta issue

When triggered, pauses the run, packages a context summary, and
hands off to the Notifier for HITL alert.

TODO (next milestone): implement.
"""
