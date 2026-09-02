from pathlib import Path
import os
import duckdb
import zipfile
import shutil
import requests
import time
import traceback
import logging

# Base directory (module location) and config constants
# Use the script's directory so the pipeline works when invoked
# from the repo root or other CWDs.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PARQUET_DIR = BASE_DIR / "parquet_store"
DUCKDB_PATH = BASE_DIR / "credit_risk.db"

# Domain constants
SIC_FILTER = (3711, 3713, 3714, 3715, 3716, 3510, 3523, 3531, 3537)
FORM_FILTER = ("10-K", "10-Q")

SUB_COLS = [
    "adsh",
    "cik",
    "name",
    "sic",
    "changed",
    "afs",
    "wksi",
    "fye",
    "form",
    "period",
    "fy",
    "fp",
    "filed",
    "accepted",
    "prevrpt",
    "nciks",
    "aciks",
    "pubfloatusd",
]

NUM_COLS = [
    "adsh",
    "tag",
    "version",
    "ddate",
    "qtrs",
    "uom",
    "dimh",
    "iprx",
    "value",
    "tag_key",
]

TAG_COLS = None  # keep all, then filter by (tag,version)
DIM_COLS = None
CAL_COLS = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)


def _log_paths():
    logging.info("Base dir: %s", BASE_DIR)
    logging.info("Working dir: %s", Path.cwd())
    logging.info("DATA_DIR: %s", DATA_DIR)
    logging.info("PARQUET_DIR: %s", PARQUET_DIR)
    logging.info("DUCKDB_PATH: %s", DUCKDB_PATH)


def download_quarter(url: str, dest: Path = None, timeout: int = 60) -> Path:
    """Download a quarter ZIP to DATA_DIR if not already present.

    Returns path to downloaded zip.
    """
    _ensure_dirs()
    dest = dest or DATA_DIR
    dest.mkdir(parents=True, exist_ok=True)
    filename = url.rstrip("/\n").split("/")[-1]
    out = dest / filename
    if out.exists():
        logging.info("Zip already exists: %s", out)
        return out
    logging.info("Downloading %s -> %s", url, out)
    r = requests.get(url, stream=True, timeout=timeout)
    r.raise_for_status()
    with open(out, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)
    return out


def _derive_quarter_from_name(zip_path: Path) -> str:
    name = zip_path.name
    lower = name.lower()
    # Prefer stripping known suffixes so monthly files like 2025_08_notes.zip
    # yield '2025_08' while quarterly names like '2019q1.zip' yield '2019q1'.
    if lower.endswith('_notes.zip'):
        return name[:-10]
    if lower.endswith('notes.zip'):
        return name[:-9]
    if lower.endswith('.zip'):
        return name[:-4]
    return name


def extract_quarter(zip_path: Path, scratch_parent: Path = None) -> Path:
    """Extract only the 5 target txt files into data/<quarter>/ and return that path."""
    _ensure_dirs()
    scratch_parent = scratch_parent or DATA_DIR
    quarter = _derive_quarter_from_name(zip_path)
    outdir = scratch_parent / quarter
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.namelist():
            lower = member.lower()
            # accept both .txt and .tsv tab-delimited files
            if any(lower.endswith(f"{base}.txt") or lower.endswith(f"{base}.tsv") for base in ("sub", "num", "tag", "dim", "cal")):
                try:
                    z.extract(member, outdir)
                except Exception:
                    # fallback: read and write stream
                    with z.open(member) as src, open(outdir / Path(member).name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    return outdir


def _read_columns(conn: duckdb.DuckDBPyConnection, temp_table: str):
    res = conn.execute(f"PRAGMA table_info('{temp_table}')").fetchall()
    # pragma returns rows like (cid, name, type, notnull, dflt_value, pk)
    return [r[1] for r in res]


def _create_temp_from_file(conn, file_path: Path, temp_name: str):
    file_sql = file_path.as_posix().replace("'", "''")
    # Try UTF-8 then latin1
    for enc in ("utf8", "latin1"):
        try:
            conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE {temp_name} AS SELECT * FROM read_csv_auto('{file_sql}', delim='\\t', header=TRUE, encoding='{enc}')")
            logging.info("Loaded %s using encoding %s", file_path.name, enc)
            return
        except Exception as e:
            logging.debug("Failed to read %s with encoding %s: %s", file_path, enc, e)
    # If both encodings fail, try a pandas fallback with permissive parsing
    logging.info("Attempting pandas fallback for %s", file_path.name)
    try:
        import pandas as pd
        for penc in ("utf8", "latin1", "cp1252"):
            try:
                df = pd.read_csv(file_path, sep='\t', encoding=penc, dtype=str, on_bad_lines='skip')
                # register dataframe into DuckDB and create a temp table from it
                conn.register("__pandas_tmp__", df)
                conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE {temp_name} AS SELECT * FROM __pandas_tmp__")
                logging.info("Loaded %s with pandas using encoding %s (rows=%d)", file_path.name, penc, len(df))
                return
            except Exception as e:
                logging.debug("Pandas failed for %s with encoding %s: %s", file_path, penc, e)
    except Exception as e:
        logging.debug("Pandas fallback not available or failed: %s", e)

    raise RuntimeError(f"Unable to read {file_path} with UTF-8 or Latin-1 and pandas fallback failed")


def filter_and_load(quarter_dir: Path, conn: duckdb.DuckDBPyConnection):
    """Run the filtering cascade on extracted txt files in quarter_dir and return row counts.

    All work is performed in the provided DuckDB connection.
    """
    counts = {"sub": 0, "num": 0, "tag": 0, "dim": 0, "cal": 0}
    start = time.time()
    # Locate files
    files = {p.name.lower(): p for p in quarter_dir.glob("**/*") if p.is_file()}

    def path_for(base_name):
        for k, p in files.items():
            if k.endswith(f"{base_name}.txt") or k.endswith(f"{base_name}.tsv"):
                return p
        return None

    sub_f = path_for("sub")
    num_f = path_for("num")
    tag_f = path_for("tag")
    dim_f = path_for("dim")
    cal_f = path_for("cal")

    if not sub_f or not num_f:
        raise FileNotFoundError("Required files sub.txt or num.txt not found in %s" % quarter_dir)

    # Load SUB
    _create_temp_from_file(conn, sub_f, "tmp_sub_raw")
    cols = _read_columns(conn, "tmp_sub_raw")
    select_cols = []
    for c in SUB_COLS:
        if c in cols:
            select_cols.append(c)
        else:
            select_cols.append(f"CAST(NULL AS VARCHAR) AS {c}")
    select_clause = ", ".join(select_cols)
    conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE tmp_sub AS SELECT {select_clause} FROM tmp_sub_raw WHERE CAST(sic AS INTEGER) IN ({', '.join(str(s) for s in SIC_FILTER)}) AND form IN ({', '.join("'"+f+"'" for f in FORM_FILTER)})")

    # collect adsh set for this quarter
    adsh_rows = conn.execute("SELECT DISTINCT adsh FROM tmp_sub").fetchall()
    adsh_list = [r[0] for r in adsh_rows]
    if not adsh_list:
        logging.info("No matching SUB rows for quarter %s", quarter_dir.name)
        return counts

    # NUM
    _create_temp_from_file(conn, num_f, "tmp_num_raw")
    num_cols = _read_columns(conn, "tmp_num_raw")
    select_num_cols = []
    for c in NUM_COLS:
        if c in num_cols:
            select_num_cols.append(c)
        elif c == 'dimh' and 'dimhash' in num_cols:
            # map dimhash -> dimh for downstream compatibility
            select_num_cols.append(f"dimhash AS dimh")
        else:
            select_num_cols.append(f"CAST(NULL AS VARCHAR) AS {c}")
    select_clause_num = ", ".join(select_num_cols)
    # Use IN list; for very large lists this could be slow but quarter-level ADSh lists are modest
    adsh_in = ", ".join("'" + str(a).replace("'", "''") + "'" for a in adsh_list)
    conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE tmp_num AS SELECT {select_clause_num} FROM tmp_num_raw WHERE adsh IN ({adsh_in})")

    # TAG
    if tag_f:
        _create_temp_from_file(conn, tag_f, "tmp_tag_raw")
        # determine distinct (tag,version) in tmp_num
        tag_pairs = conn.execute("SELECT DISTINCT tag, version FROM tmp_num").fetchall()
        if tag_pairs:
            pairs_list = ", ".join("('" + p[0].replace("'", "''") + "','" + p[1].replace("'", "''") + "')" for p in tag_pairs)
            conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE tmp_tag AS SELECT * FROM tmp_tag_raw t WHERE (t.tag, t.version) IN ({pairs_list})")
        else:
            conn.execute("CREATE OR REPLACE TEMPORARY TABLE tmp_tag AS SELECT * FROM tmp_tag_raw WHERE 1=0")
    else:
        conn.execute("CREATE OR REPLACE TEMPORARY TABLE tmp_tag AS SELECT * FROM tmp_num WHERE 1=0")

    # DIM
    if dim_f:
        _create_temp_from_file(conn, dim_f, "tmp_dim_raw")
        dim_vals = conn.execute("SELECT DISTINCT dimh FROM tmp_num WHERE dimh IS NOT NULL").fetchall()
        if dim_vals:
            vals = ", ".join("'" + str(v[0]).replace("'", "''") + "'" for v in dim_vals)
            # tmp_dim_raw may use 'dimhash' instead of 'dimh'
            dim_raw_cols = _read_columns(conn, "tmp_dim_raw")
            dim_col = 'dimh' if 'dimh' in dim_raw_cols else ('dimhash' if 'dimhash' in dim_raw_cols else None)
            if dim_col:
                # select everything but ensure we provide a consistent dimh column name
                if dim_col == 'dimhash':
                    conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE tmp_dim AS SELECT *, dimhash AS dimh FROM tmp_dim_raw WHERE dimhash IN ({vals})")
                else:
                    conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE tmp_dim AS SELECT * FROM tmp_dim_raw WHERE dimh IN ({vals})")
            else:
                conn.execute("CREATE OR REPLACE TEMPORARY TABLE tmp_dim AS SELECT * FROM tmp_dim_raw WHERE 1=0")
        else:
            conn.execute("CREATE OR REPLACE TEMPORARY TABLE tmp_dim AS SELECT * FROM tmp_dim_raw WHERE 1=0")
    else:
        conn.execute("CREATE OR REPLACE TEMPORARY TABLE tmp_dim AS SELECT * FROM tmp_num WHERE 1=0")

    # CAL
    if cal_f:
        _create_temp_from_file(conn, cal_f, "tmp_cal_raw")
        conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE tmp_cal AS SELECT * FROM tmp_cal_raw WHERE adsh IN ({adsh_in})")
    else:
        conn.execute("CREATE OR REPLACE TEMPORARY TABLE tmp_cal AS SELECT * FROM tmp_num WHERE 1=0")

    # counts
    counts["sub"] = conn.execute("SELECT COUNT(*) FROM tmp_sub").fetchone()[0]
    counts["num"] = conn.execute("SELECT COUNT(*) FROM tmp_num").fetchone()[0]
    counts["tag"] = conn.execute("SELECT COUNT(*) FROM tmp_tag").fetchone()[0]
    counts["dim"] = conn.execute("SELECT COUNT(*) FROM tmp_dim").fetchone()[0]
    counts["cal"] = conn.execute("SELECT COUNT(*) FROM tmp_cal").fetchone()[0]

    elapsed = time.time() - start
    logging.info("Filtered quarter %s: sub=%d num=%d tag=%d dim=%d cal=%d (%.1fs)", quarter_dir.name, counts['sub'], counts['num'], counts['tag'], counts['dim'], counts['cal'], elapsed)
    return counts


def _table_exists(conn, table_name: str) -> bool:
    r = conn.execute(f"SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '{table_name.lower()}'").fetchone()
    return bool(r and r[0])


def append_to_consolidated(conn: duckdb.DuckDBPyConnection, temp_table: str, consolidated_table: str):
    """Append rows from a temp_table into consolidated_table inside the same DuckDB connection."""
    try:
        # Try to insert - if table missing this will raise
        conn.execute(f"INSERT INTO {consolidated_table} SELECT * FROM {temp_table}")
    except Exception:
        # Create table from the temp table (first time)
        conn.execute(f"CREATE TABLE {consolidated_table} AS SELECT * FROM {temp_table}")


def export_parquets(conn: duckdb.DuckDBPyConnection):
    for t in ("sub", "num", "tag", "dim", "cal"):
        out = (PARQUET_DIR / f"{t}.parquet").as_posix()
        try:
            conn.execute(f"COPY {t} TO '{out}' (FORMAT PARQUET)")
            logging.info("Exported %s -> %s", t, out)
        except Exception as e:
            logging.error("Failed to export %s: %s", t, e)


def cleanup(quarter_dir: Path, zip_path: Path):
    try:
        if quarter_dir.exists():
            shutil.rmtree(quarter_dir)
        if zip_path.exists():
            zip_path.unlink()
    except Exception:
        logging.warning("Cleanup failed for %s or %s", quarter_dir, zip_path)


def run_pipeline(download_sources: dict = None, dedup: bool = False, export_every_quarter: bool = True):
    """Main entrypoint: discover zips in DATA_DIR, process each quarter, append into DUCKDB, and export Parquet files.

    Args:
        download_sources: optional mapping quarter->url to download if zip missing.
        dedup: if True, run a deduplication step on `sub` after each quarter (configurable)
        export_every_quarter: if True, export Parquet after each quarter (recommended)
    """
    _ensure_dirs()
    _log_paths()
    persistent_db = True
    try:
        conn = duckdb.connect(database=DUCKDB_PATH.as_posix(), read_only=False)
    except Exception as e:
        logging.error("Unable to open DuckDB file %s: %s", DUCKDB_PATH, e)
        if DUCKDB_PATH.exists() and not os.access(DUCKDB_PATH, os.W_OK):
            logging.error("File exists but is not writable. Check file permissions and close other programs that may be locking the file.")
        logging.warning("Falling back to an in-memory DuckDB instance. Results will NOT be persisted to %s.", DUCKDB_PATH)
        conn = duckdb.connect()
        persistent_db = False
    # ensure processed_quarters table
    conn.execute("CREATE TABLE IF NOT EXISTS processed_quarters(quarter VARCHAR PRIMARY KEY, status VARCHAR, processed_at TIMESTAMP, sub_rows BIGINT, num_rows BIGINT, tag_rows BIGINT, dim_rows BIGINT, cal_rows BIGINT, notes VARCHAR)")

    # find zips
    zips = sorted(DATA_DIR.glob("*.zip"))
    logging.info("Found zip files in %s: %s", DATA_DIR, [p.name for p in zips])
    processed = []
    failed = []

    for z in zips:
        quarter = _derive_quarter_from_name(z)
        already = conn.execute("SELECT COUNT(*) FROM processed_quarters WHERE quarter = '%s' AND status = 'done'" % quarter).fetchone()[0]
        if already:
            logging.info("Skipping already processed quarter %s", quarter)
            continue

        logging.info("Processing quarter %s from %s", quarter, z)
        try:
            qdir = extract_quarter(z)
            counts = filter_and_load(qdir, conn)
            # append each tmp to consolidated
            append_to_consolidated(conn, "tmp_sub", "sub")
            append_to_consolidated(conn, "tmp_num", "num")
            append_to_consolidated(conn, "tmp_tag", "tag")
            append_to_consolidated(conn, "tmp_dim", "dim")
            append_to_consolidated(conn, "tmp_cal", "cal")

            if dedup:
                # Keep latest per adsh by filed/accepted
                try:
                    conn.execute("CREATE OR REPLACE TABLE sub AS SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY adsh ORDER BY filed DESC NULLS LAST, accepted DESC NULLS LAST) rn FROM sub) WHERE rn = 1")
                    logging.info("Dedup applied on sub table")
                except Exception as e:
                    logging.warning("Dedup failed: %s", e)

            if export_every_quarter:
                export_parquets(conn)

            conn.execute("INSERT OR REPLACE INTO processed_quarters(quarter, status, processed_at, sub_rows, num_rows, tag_rows, dim_rows, cal_rows, notes) VALUES (?, 'done', CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)", [quarter, counts['sub'], counts['num'], counts['tag'], counts['dim'], counts['cal'], ''])
            cleanup(qdir, z)
            processed.append(quarter)
        except Exception as e:
            logging.error("Failed quarter %s: %s", quarter, traceback.format_exc())
            conn.execute("INSERT OR REPLACE INTO processed_quarters(quarter, status, processed_at, notes) VALUES (?, 'failed', CURRENT_TIMESTAMP, ?)", [quarter, str(e)[:2000]])
            failed.append(quarter)
            # continue to next quarter without raising

    # final export (if not exported after each quarter)
    if not export_every_quarter:
        export_parquets(conn)

    # final summary
    totals = {}
    for t in ("sub", "num", "tag", "dim", "cal"):
        try:
            totals[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            totals[t] = 0

    logging.info("Pipeline finished. Processed: %s Failed: %s Totals: %s", processed, failed, totals)
    return {"processed": processed, "failed": failed, "totals": totals}


def process_quarter(zip_path: Path | str, dedup: bool = False, export: bool = True):
    """Process a single quarter ZIP: extract, filter, append to credit_risk.db, export parquets, cleanup.

    Raises RuntimeError if `credit_risk.db` cannot be opened for writing.
    """
    _ensure_dirs()
    _log_paths()
    zip_path = Path(zip_path)
    if not zip_path.is_absolute():
        # allow relative paths under BASE_DIR or from cwd
        candidate = BASE_DIR / zip_path
        if candidate.exists():
            zip_path = candidate
        else:
            zip_path = Path.cwd() / zip_path

    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {zip_path}")

    quarter = _derive_quarter_from_name(zip_path)

    # Open persistent DB only — abort if not writable (we require persistence for append)
    try:
        conn = duckdb.connect(database=DUCKDB_PATH.as_posix(), read_only=False)
    except Exception as e:
        raise RuntimeError(f"Cannot open persistent DuckDB at {DUCKDB_PATH}: {e}\nFix file permissions or remove the file lock before retrying.")

    conn.execute("CREATE TABLE IF NOT EXISTS processed_quarters(quarter VARCHAR PRIMARY KEY, status VARCHAR, processed_at TIMESTAMP, sub_rows BIGINT, num_rows BIGINT, tag_rows BIGINT, dim_rows BIGINT, cal_rows BIGINT, notes VARCHAR)")
    already = conn.execute("SELECT COUNT(*) FROM processed_quarters WHERE quarter = ? AND status = 'done'", [quarter]).fetchone()[0]
    if already:
        logging.info("Quarter %s already processed; skipping.", quarter)
        return {"skipped": True, "quarter": quarter}

    # record before counts
    before = {}
    for t in ("sub", "num", "tag", "dim", "cal"):
        try:
            before[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            before[t] = 0

    # process
    try:
        qdir = extract_quarter(zip_path)
        counts = filter_and_load(qdir, conn)

        # append each tmp to consolidated
        append_to_consolidated(conn, "tmp_sub", "sub")
        append_to_consolidated(conn, "tmp_num", "num")
        append_to_consolidated(conn, "tmp_tag", "tag")
        append_to_consolidated(conn, "tmp_dim", "dim")
        append_to_consolidated(conn, "tmp_cal", "cal")

        if dedup:
            try:
                conn.execute("CREATE OR REPLACE TABLE sub AS SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY adsh ORDER BY filed DESC NULLS LAST, accepted DESC NULLS LAST) rn FROM sub) WHERE rn = 1")
                logging.info("Dedup applied on sub table")
            except Exception as e:
                logging.warning("Dedup failed: %s", e)

        if export:
            export_parquets(conn)

        # after counts
        after = {}
        for t in ("sub", "num", "tag", "dim", "cal"):
            try:
                after[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                after[t] = 0

        # insert processed_quarters
        conn.execute("INSERT OR REPLACE INTO processed_quarters(quarter, status, processed_at, sub_rows, num_rows, tag_rows, dim_rows, cal_rows, notes) VALUES (?, 'done', CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)", [quarter, counts['sub'], counts['num'], counts['tag'], counts['dim'], counts['cal'], 'processed via process_quarter'])

        # cleanup
        cleanup(qdir, zip_path)

        added = {t: after[t] - before.get(t, 0) for t in after}
        summary = {
            "quarter": quarter,
            "counts_this_quarter": counts,
            "before": before,
            "after": after,
            "added": added,
        }
        logging.info("Processed quarter %s: added %s", quarter, added)
        return summary
    except Exception as e:
        conn.execute("INSERT OR REPLACE INTO processed_quarters(quarter, status, processed_at, notes) VALUES (?, 'failed', CURRENT_TIMESTAMP, ?)", [quarter, str(e)[:2000]])
        logging.error("Processing quarter %s failed: %s", quarter, e)
        raise



if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # process a single zip passed as first arg
        try:
            res = process_quarter(sys.argv[1])
            print(res)
        except Exception as e:
            logging.error("Error: %s", e)
            raise
    else:
        run_pipeline()
