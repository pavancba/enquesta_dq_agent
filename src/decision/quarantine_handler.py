"""
Quarantine Handler — Layer 3 (Decision)

Separates the loaded DataFrame into three output streams:
  - clean rows -> data/clean/
  - quarantined rows -> data/quarantine/
  - flagged rows -> data/flagged/

Each output file is timestamped and tagged with the run_id so the
audit log can tie them back to the source file.

TODO (next milestone): implement.
"""
