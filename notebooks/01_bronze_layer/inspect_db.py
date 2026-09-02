from pathlib import Path
import duckdb

BASE = Path(__file__).resolve().parent
DB = BASE / "credit_risk.db"

print("Inspecting DuckDB at:", DB)
conn = duckdb.connect(DB.as_posix())

for t in ("sub", "num", "tag", "dim", "cal"):
    try:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except Exception as e:
        cnt = f"ERROR: {e}"
    print(f"{t}: {cnt}")

try:
    pq = conn.execute("SELECT quarter, status, processed_at FROM processed_quarters ORDER BY processed_at").fetchall()
    print("processed_quarters:", pq)
except Exception as e:
    print("processed_quarters: ERROR", e)
