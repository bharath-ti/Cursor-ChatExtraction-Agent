import sqlite3, json
from pathlib import Path
import os

db = Path(os.environ["APPDATA"]) / "Cursor/User/globalStorage/state.vscdb"
conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
cur = conn.cursor()

# Get one assistant bubble and dump ALL its keys
for key, value in cur.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' LIMIT 50"):
    data = json.loads(value)
    if data.get("type") == 2:  # assistant
        print("ASSISTANT BUBBLE KEYS:", sorted(data.keys()))
        print("Sample text field:", repr(data.get("text"))[:200])
        print("Sample tokenCount:", data.get("tokenCount"))
        print("Sample modelType:", data.get("modelType"))
        print(json.dumps(data, indent=2)[:3000])
        break

# Also check ItemTable
print("\n--- ItemTable keys with 'chat' or 'ai' ---")
for (key,) in cur.execute("SELECT key FROM ItemTable WHERE key LIKE '%chat%' OR key LIKE '%ai%' OR key LIKE '%composer%'"):
    print(key)