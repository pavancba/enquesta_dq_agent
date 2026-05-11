"""
File Watcher — Layer 1 (Ingestion)

Watches data/inbox/ for new CSV files. When one appears:
  - Generates a unique run_id
  - Records arrival timestamp and file metadata
  - Hands the file off to the FileLoader

In the demo, this can run in two modes:
  1. Polling mode: scans inbox every N seconds (Streamlit demo)
  2. One-shot mode: process a specific file passed via CLI

TODO (next milestone): implement.
"""
