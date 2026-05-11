"""
Audit Logger — Layer 5 (Audit & Output)

Writes every event, finding, decision, and correction to a SQLite
database (audit.db). Single source of truth for "what happened to
this file" — answers the City auditor's questions.

Tables:
  - file_runs        (one row per file processed)
  - findings         (one row per data quality issue)
  - decisions        (one row per router verdict)
  - corrections      (one row per auto-correction with before/after)
  - llm_calls        (one row per Ollama invocation with prompt/response)

TODO (next milestone): implement schema creation + write methods.
"""
