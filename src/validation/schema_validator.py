"""
Schema Validator — Layer 2 (Validation)

Implements Rule 1 (extra_comma_breaks_schema): for each row in the
raw file, check that column count matches schema.expected_column_count.

Produces Findings of severity HIGH for rows that fail.
Required column names and count come from config/rules.yaml.

TODO (next milestone): implement.
"""
