"""
File Loader — Layer 1 (Ingestion)

Reads a CSV from disk with extra care for Rule 1 (column count anomalies).

Why we don't just call pandas.read_csv():
  pandas, by default, is *helpful*: when it sees a row with too many or too
  few fields it may either error out or silently quietly adjust. Neither is
  what we want — we need to KEEP the broken row so the schema validator can
  produce a Finding for it.

Approach:
  1. Open the file with Python's csv module first
  2. Walk every line, count fields per row
  3. Capture file metadata (hash, line count, column count distribution)
  4. Build a pandas DataFrame from the well-formed rows for downstream use
  5. Return ALL the per-row column counts so the validator can produce
     findings for off-spec rows

Three real-world classes of column-count anomaly we distinguish:
  * NORMAL          : column count == expected
  * TRAILING_COMMA  : column count == expected + 1, last cell is empty
                      (96% of "extra column" cases in the 206-file archive)
                      Cosmetic only — auto-correct by dropping the empty cell
  * REAL_EXTRA      : column count > expected, last cell non-empty
                      (Real Rule 1 hit — quarantine)
  * FEWER           : column count < expected (missing fields)

Run this file directly to self-test against samples/:
    python -m src.ingestion.file_loader samples/demo_07_showcase_synthetic.csv
"""
from __future__ import annotations

import csv
import hashlib
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd


class RowShape(str, Enum):
    """Per-row column-count classification."""
    NORMAL = "normal"
    TRAILING_COMMA = "trailing_comma"
    REAL_EXTRA = "real_extra"
    FEWER = "fewer"
    EMPTY = "empty"


@dataclass
class RowAnomaly:
    """One row that didn't match the expected column count."""
    row_index: int             # 1-based row number in the source file (line N - 1 because header is line 1)
    line_number: int           # 1-based actual line number in the file
    column_count: int          # actual columns found
    shape: RowShape
    raw_line: str              # original text of the row, for the audit log


@dataclass
class LoadResult:
    """Everything the loader produces for one file."""
    file_path: Path
    file_name: str
    file_hash: str
    is_empty: bool
    header: list[str]
    header_matches_schema: bool
    expected_column_count: int
    total_rows: int            # total non-blank data rows in file
    dataframe: pd.DataFrame    # only the well-formed rows, ready for the rule engine
    anomalies: list[RowAnomaly] = field(default_factory=list)


def _compute_file_hash(path: Path, block_size: int = 65536) -> str:
    """SHA-256 of the file, for the audit log (proves which file was processed)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(block_size):
            h.update(chunk)
    return h.hexdigest()


def load_csv(
    file_path: str | Path,
    expected_columns: list[str],
) -> LoadResult:
    """
    Load a CSV with full schema awareness.

    Args:
        file_path: Path to the CSV file.
        expected_columns: Ordered list of column names expected in the header.

    Returns:
        LoadResult with the parsed DataFrame, header info, and per-row anomalies.
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")

    file_hash = _compute_file_hash(p)
    expected_count = len(expected_columns)

    # Empty-file fast path (real production hits: 13 of 206 archive files)
    if p.stat().st_size == 0:
        return LoadResult(
            file_path=p,
            file_name=p.name,
            file_hash=file_hash,
            is_empty=True,
            header=[],
            header_matches_schema=False,
            expected_column_count=expected_count,
            total_rows=0,
            dataframe=pd.DataFrame(columns=expected_columns),
            anomalies=[],
        )

    # Pass 1: parse every line with csv.reader, classify each row's shape
    well_formed_rows: list[list[str]] = []
    anomalies: list[RowAnomaly] = []
    header: list[str] = []

    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for line_no, row in enumerate(reader, start=1):
            # Skip completely blank lines (no fields and not even an empty string)
            if not row:
                continue

            if line_no == 1:
                # Header. Drop a trailing empty cell if present (cosmetic only)
                header = [c.strip() for c in row]
                while header and header[-1] == "":
                    header.pop()
                continue

            # Data row — classify
            ncols = len(row)
            if ncols == expected_count:
                shape = RowShape.NORMAL
                well_formed_rows.append(row)
            elif ncols == expected_count + 1 and row[-1] == "":
                # Cosmetic trailing comma → strip and treat as normal
                shape = RowShape.TRAILING_COMMA
                well_formed_rows.append(row[:-1])
                anomalies.append(RowAnomaly(
                    row_index=line_no - 1,
                    line_number=line_no,
                    column_count=ncols,
                    shape=shape,
                    raw_line=",".join(row),
                ))
            elif ncols > expected_count:
                shape = RowShape.REAL_EXTRA
                anomalies.append(RowAnomaly(
                    row_index=line_no - 1,
                    line_number=line_no,
                    column_count=ncols,
                    shape=shape,
                    raw_line=",".join(row),
                ))
                # Don't add to well_formed_rows — schema-broken
            else:
                shape = RowShape.FEWER
                anomalies.append(RowAnomaly(
                    row_index=line_no - 1,
                    line_number=line_no,
                    column_count=ncols,
                    shape=shape,
                    raw_line=",".join(row),
                ))
                # Don't add to well_formed_rows

    header_matches = header == expected_columns

    # Pass 2: build the DataFrame from well-formed rows
    if well_formed_rows:
        df = pd.DataFrame(well_formed_rows, columns=expected_columns)
    else:
        df = pd.DataFrame(columns=expected_columns)

    return LoadResult(
        file_path=p,
        file_name=p.name,
        file_hash=file_hash,
        is_empty=False,
        header=header,
        header_matches_schema=header_matches,
        expected_column_count=expected_count,
        total_rows=len(well_formed_rows) + len(anomalies),
        dataframe=df,
        anomalies=anomalies,
    )


# ---------------------------------------------------------------------------
# Self-test — run with:
#   python -m src.ingestion.file_loader samples/demo_07_showcase_synthetic.csv
# ---------------------------------------------------------------------------
def _self_test(target: Optional[str] = None) -> int:
    import yaml

    config_path = Path("config/rules.yaml")
    if not config_path.exists():
        print(f"FAIL: {config_path} not found. Run from project root.")
        return 1

    with config_path.open() as f:
        rules = yaml.safe_load(f)
    expected_cols = rules["schema"]["expected_columns"]

    # If user passed a specific file, just test that one. Otherwise sweep all samples.
    if target:
        files_to_test = [Path(target)]
    else:
        files_to_test = sorted(Path("samples").glob("demo_*.csv"))

    print("=" * 70)
    print(f"FileLoader self-test  —  testing {len(files_to_test)} file(s)")
    print("=" * 70)
    for fp in files_to_test:
        print()
        print(f"  File: {fp.name}")
        try:
            result = load_csv(fp, expected_cols)
        except Exception as e:
            print(f"    ERROR: {e}")
            continue
        print(f"    is_empty:               {result.is_empty}")
        print(f"    file_hash:              {result.file_hash[:16]}...")
        print(f"    header_matches_schema:  {result.header_matches_schema}")
        print(f"    total_rows:             {result.total_rows}")
        print(f"    well-formed rows:       {len(result.dataframe)}")
        print(f"    anomalies:              {len(result.anomalies)}")
        if result.anomalies:
            shape_counts: dict[str, int] = {}
            for a in result.anomalies:
                shape_counts[a.shape.value] = shape_counts.get(a.shape.value, 0) + 1
            for shape, count in shape_counts.items():
                print(f"      {shape:18s} {count}")
    print()
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(_self_test(arg))
