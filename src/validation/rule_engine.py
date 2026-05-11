"""
Rule Engine — Layer 2 (Validation)

Runs the deterministic rules (currently Rule 2: trailing-negative
amounts) against each row in the loaded DataFrame. Rules are
declarative — defined in config/rules.yaml — and loaded at startup.

Each rule produces zero or more Findings. The router agent later
decides what to do with them.

TODO (next milestone): implement Rule 2 (regex match on CIBL_AMT_5
and CIBL_AMT_95, auto-correct logic).
"""
