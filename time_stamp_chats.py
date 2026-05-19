"""
check_timestamps.py
-------------------
Checks how many composers have valid timestamps vs null.
Also shows the KV key structure to understand what's stored.
"""
import sqlite3
import os
import json
from pathlib import Path
from datetime import datetime, timezone


def main():
    db = Path(os.environ["APPDATA"]) / "Cursor/User/globalStorage/state.vscdb"
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    cur = conn.cursor()

    has_ts = 0
    no_ts = 0
    no_ts_examples = []

    for (key, val) in cur.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ):
        try:
            data = json.loads(val)
            ts = data.get("createdAt") or data.get("lastUpdatedAt")
            if ts:
                has_ts += 1
            else:
                no_ts += 1
                if len(no_ts_examples) < 3:
                    no_ts_examples.append({
                        "composer_id": key.split(":", 1)[1],
                        "keys_present": sorted(data.keys()),
                        "name": data.get("name", "(no name)"),
                    })
        except Exception as e:
            no_ts += 1

    print(f"Composers WITH valid timestamp:    {has_ts}")
    print(f"Composers WITHOUT timestamp (skipped): {no_ts}")
    print(f"Total: {has_ts + no_ts}")
    print()

    if no_ts_examples:
        print("Sample composers with no timestamp — keys they DO have:")
        for ex in no_ts_examples:
            print(f"  {ex['composer_id'][:20]}...  name='{ex['name']}'")
            print(f"    keys: {ex['keys_present']}")
        print()

    # Also check the agentKv prefix — this might hold more chat data
    agent_kv_count = cur.execute(
        "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'agentKv%'"
    ).fetchone()[0]
    print(f"agentKv entries: {agent_kv_count}")

    if agent_kv_count > 0:
        print("Sample agentKv keys:")
        for (key,) in cur.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE 'agentKv%' LIMIT 10"
        ):
            print(f"  {key}")

    # Show ALL unique key prefixes in the global DB
    print("\nAll key prefixes in global DB:")
    all_keys = cur.execute("SELECT key FROM cursorDiskKV").fetchall()
    prefixes = {}
    for (key,) in all_keys:
        prefix = key.split(":")[0]
        prefixes[prefix] = prefixes.get(prefix, 0) + 1
    for prefix, count in sorted(prefixes.items(), key=lambda x: -x[1]):
        print(f"  {prefix:<30} {count:>6} entries")

    conn.close()


if __name__ == "__main__":
    main()