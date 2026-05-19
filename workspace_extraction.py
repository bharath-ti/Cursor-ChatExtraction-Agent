"""
find_workspace_dbs.py
---------------------
Scans ALL Cursor SQLite databases (global + every workspace)
and shows what's in each one.
"""
import sqlite3
import os
import json
from pathlib import Path
from datetime import datetime, timezone


def open_ro(db_path):
    uri = f"file:{db_path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def scan_db(db_path):
    """Return summary dict for one DB file."""
    result = {
        "path": str(db_path),
        "composers": 0,
        "bubbles": 0,
        "latest_ts": None,
        "all_tables": [],
        "all_kv_prefixes": [],
        "workspace_folder": None,
    }
    try:
        conn = open_ro(db_path)
        cur = conn.cursor()

        # List all tables
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        result["all_tables"] = tables

        if "cursorDiskKV" in tables:
            result["composers"] = cur.execute(
                "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchone()[0]
            result["bubbles"] = cur.execute(
                "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
            ).fetchone()[0]

            # Get all unique key prefixes (first segment before colon)
            prefixes = set()
            for (key,) in cur.execute("SELECT key FROM cursorDiskKV LIMIT 500"):
                prefix = key.split(":")[0]
                prefixes.add(prefix)
            result["all_kv_prefixes"] = sorted(prefixes)

            # Latest timestamp
            latest = None
            for (val,) in cur.execute(
                "SELECT value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ):
                try:
                    data = json.loads(val)
                    ts = data.get("lastUpdatedAt") or data.get("createdAt")
                    if ts and (latest is None or ts > latest):
                        latest = ts
                except Exception:
                    pass
            result["latest_ts"] = latest

        if "ItemTable" in tables:
            # Try to find workspace folder path
            for key in ("folder.vscodePath", "workspace.folder", "workbench.explorer.treeViewState"):
                try:
                    row = cur.execute(
                        "SELECT value FROM ItemTable WHERE key = ?", (key,)
                    ).fetchone()
                    if row:
                        result["workspace_folder"] = f"{key}={str(row[0])[:120]}"
                        break
                except Exception:
                    pass

            # Also check for any key containing 'folder' or 'workspace'
            if not result["workspace_folder"]:
                try:
                    for (key, val) in cur.execute(
                        "SELECT key, value FROM ItemTable WHERE key LIKE '%folder%' "
                        "OR key LIKE '%workspace%' LIMIT 3"
                    ):
                        result["workspace_folder"] = f"{key}={str(val)[:100]}"
                        break
                except Exception:
                    pass

        conn.close()
    except Exception as e:
        result["error"] = str(e)

    return result


def main():
    appdata = Path(os.environ.get("APPDATA", ""))

    # 1. Global DB
    global_db = appdata / "Cursor/User/globalStorage/state.vscdb"
    print("=" * 70)
    print("GLOBAL DB")
    print("=" * 70)
    if global_db.exists():
        r = scan_db(global_db)
        print(f"  Path:      {r['path']}")
        print(f"  Tables:    {r['all_tables']}")
        print(f"  Composers: {r['composers']}")
        print(f"  Bubbles:   {r['bubbles']}")
        print(f"  KV prefixes: {r['all_kv_prefixes']}")
    else:
        print(f"  NOT FOUND at {global_db}")

    # 2. All workspace DBs
    ws_root = appdata / "Cursor/User/workspaceStorage"
    print(f"\n{'=' * 70}")
    print(f"WORKSPACE DBs  (root: {ws_root})")
    print("=" * 70)

    if not ws_root.exists():
        print("  workspaceStorage directory not found!")
        return

    all_dbs = list(ws_root.glob("*/state.vscdb"))
    print(f"  Total workspace DB files found: {len(all_dbs)}")

    results = []
    for db_path in all_dbs:
        r = scan_db(db_path)
        results.append(r)

    # Sort by latest timestamp descending
    results.sort(
        key=lambda r: r["latest_ts"] or 0,
        reverse=True
    )

    print(f"\n  {'When':>16}  {'Comp':>5}  {'Bub':>6}  {'Tables':<30}  Path")
    print("  " + "-" * 100)
    for r in results:
        ts_str = "unknown"
        if r["latest_ts"]:
            ts_str = datetime.fromtimestamp(
                r["latest_ts"] / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M")

        tables_str = ",".join(r["all_tables"])[:28]
        path_short = r["path"][-50:]
        print(f"  {ts_str:>16}  {r['composers']:>5}  {r['bubbles']:>6}  "
              f"{tables_str:<30}  ...{path_short}")

        if r.get("workspace_folder"):
            print(f"  {'':>16}  {'':>5}  {'':>6}  folder: {r['workspace_folder'][:80]}")

        if r.get("all_kv_prefixes"):
            print(f"  {'':>16}  {'':>5}  {'':>6}  kv_prefixes: {r['all_kv_prefixes']}")

        if r.get("error"):
            print(f"  {'':>16}  ERROR: {r['error']}")

    # Summary
    total_composers = sum(r["composers"] for r in results)
    total_bubbles = sum(r["bubbles"] for r in results)
    print(f"\n  TOTAL across all workspace DBs: "
          f"{total_composers} composers, {total_bubbles} bubbles")
    print(f"  Global DB: {scan_db(global_db)['composers']} composers, "
          f"{scan_db(global_db)['bubbles']} bubbles")
    print(f"\n  GRAND TOTAL: "
          f"{total_composers + scan_db(global_db)['composers']} composers, "
          f"{total_bubbles + scan_db(global_db)['bubbles']} bubbles")


if __name__ == "__main__":
    main()