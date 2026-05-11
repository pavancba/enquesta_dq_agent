"""
File Loader — Layer 1 (Ingestion)

Reads a CSV from disk into memory with special care for Rule 1
(extra-comma detection). Standard pandas.read_csv() can silently
swallow extra commas, so this module:

  1. Opens the file via Python's csv module first to count fields
     per line — this catches Rule 1 even when pandas would hide it
  2. Then loads into a pandas DataFrame for the rule engine
  3. Captures file metadata: row count, column count, file hash

TODO (next milestone): implement.
"""
