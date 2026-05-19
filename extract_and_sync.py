"""
extract_and_sync.py
-------------------
Extract Cursor prompts and sync new ones to PostgreSQL.

Two modes:
  --date YYYY-MM-DD   Filter to prompts from a specific date (midnight to midnight local time).
                      Defaults to TODAY if --date is not given.
  --all               Skip date filtering; sync everything not yet in the DB.
  --dry-run           Print what would be inserted without touching the DB.
  --json-only         Extract and write JSON only, skip DB sync.

Usage examples:
  python extract_and_sync.py                        # today's new prompts → DB
  python extract_and_sync.py --date 2026-05-16      # specific date → DB
  python extract_and_sync.py --all                  # everything not yet in DB
  python extract_and_sync.py --json-only            # just update JSON files
  python extract_and_sync.py --dry-run              # preview without writing

Environment variables (put in .env or export before running):
  DATABASE_URL   postgresql://user:password@host:5432/dbname
                 If not set, --json-only mode is forced automatically.

Filtering logic
---------------
The timestamp problem: composer.createdAt is when a CONVERSATION started,
not when each individual prompt was sent. A conversation started on May 14th
can have prompts added through May 18th — all showing the same created_at.

We handle this with a TWO-PASS approach:
  Pass 1: Use created_at as a COARSE filter.
           Any conversation created_at on or before the target date window
           is a candidate — it MIGHT contain prompts from that day.
  Pass 2 (DB mode): Use bubble_id as the deduplication key.
           We only insert rows whose bubble_id doesn't exist in the DB yet.
           This means you safely run the script multiple times — it's idempotent.

For --json-only mode we apply a date filter on created_at (best-effort).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional


# ---------------------------------------------------------------------------
# Re-use extraction logic (same functions as extract_prompts.py)
# ---------------------------------------------------------------------------

BUBBLE_TYPE_USER = 1
BUBBLE_TYPE_ASSISTANT = 2


def cursor_global_db() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ["APPDATA"]) / "Cursor"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Cursor"
    else:
        base = Path.home() / ".config" / "Cursor"
    return base / "User" / "globalStorage" / "state.vscdb"


def open_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Cursor DB not found at {db_path}")
    uri = f"file:{db_path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def safe_json(blob):
    if blob is None:
        return None
    if isinstance(blob, (bytes, bytearray)):
        try:
            blob = blob.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None


def ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


def ms_to_dt(ms) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, OSError, TypeError):
        return None


def _flatten_rich_text(node) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            return node["text"]
        if node.get("type") == "linebreak":
            return "\n"
        parts = []
        if "root" in node:
            parts.append(_flatten_rich_text(node["root"]))
        for child in node.get("children", []) or []:
            parts.append(_flatten_rich_text(child))
        return "".join(parts)
    if isinstance(node, list):
        return "".join(_flatten_rich_text(c) for c in node)
    return ""


def extract_text(bubble: dict) -> str:
    text = bubble.get("text") or ""
    if text.strip():
        return text.strip()
    rich = bubble.get("richText")
    if rich:
        if isinstance(rich, str):
            parsed = safe_json(rich)
            if parsed:
                flat = _flatten_rich_text(parsed).strip()
                if flat:
                    return flat
            elif rich.strip():
                return rich.strip()
        elif isinstance(rich, dict):
            flat = _flatten_rich_text(rich).strip()
            if flat:
                return flat
    blocks = bubble.get("codeBlocks") or []
    if blocks:
        chunks = []
        for b in blocks:
            if isinstance(b, dict):
                content = b.get("content") or ""
                if content:
                    lang = b.get("languageId") or ""
                    chunks.append(f"```{lang}\n{content}\n```")
        if chunks:
            return "\n\n".join(chunks)
    return ""


def _uri_to_path(obj) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj or None
    if isinstance(obj, dict):
        for key in ("fsPath", "_fsPath", "path"):
            v = obj.get(key)
            if v and isinstance(v, str):
                return v
        inner = obj.get("uri")
        if isinstance(inner, dict):
            for key in ("fsPath", "_fsPath", "path"):
                v = inner.get(key)
                if v and isinstance(v, str):
                    return v
    return None


def extract_context(bubble: dict) -> dict:
    files: list[str] = []
    for attach in bubble.get("attachedFileCodeChunksUris", []) or []:
        path = _uri_to_path(attach)
        if path:
            files.append(path)
    ctx = bubble.get("context") or {}
    for sel in ctx.get("fileSelections", []) or []:
        if isinstance(sel, dict):
            path = _uri_to_path(sel.get("uri"))
            if path:
                files.append(path)
    seen: set[str] = set()
    unique_files = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)
    folders: list[str] = []
    for folder in bubble.get("attachedFolders", []) or []:
        path = _uri_to_path(folder) if isinstance(folder, dict) else (folder if isinstance(folder, str) else None)
        if path:
            folders.append(path)
    return {
        "attached_files": unique_files,
        "attached_folders": folders,
        "images": len(bubble.get("images", []) or []),
        "web_references": [
            r.get("url") for r in (bubble.get("webReferences") or [])
            if isinstance(r, dict) and r.get("url")
        ],
        "docs_references": [
            (r.get("name") or r.get("url"))
            for r in (bubble.get("docsReferences") or [])
            if isinstance(r, dict) and (r.get("name") or r.get("url"))
        ],
        "is_agentic": bool(bubble.get("isAgentic")),
    }


def detect_workspace(composer: dict) -> str | None:
    for key in ("workspaceRootPath", "rootPath", "projectPath", "workspaceId"):
        v = composer.get(key)
        if v and isinstance(v, str):
            return v
    uris = composer.get("allAttachedFileCodeChunksUris") or []
    paths = []
    for uri in uris:
        if isinstance(uri, str):
            decoded = uri.replace("file:///", "").replace("%3A", ":")
            paths.append(decoded)
        elif isinstance(uri, dict):
            p = _uri_to_path(uri)
            if p:
                paths.append(p)
    if not paths:
        return None
    try:
        dirs = [str(Path(p).parent) for p in paths]
        common = dirs[0]
        for d in dirs[1:]:
            while common and not d.startswith(common):
                common = str(Path(common).parent)
        return common or None
    except Exception:
        return None


def detect_model(composer: dict) -> str | None:
    summary = composer.get("latestConversationSummary")
    if isinstance(summary, dict):
        inner = summary.get("summary")
        if isinstance(inner, dict):
            m = inner.get("modelType") or inner.get("model")
            if m:
                return m
        m = summary.get("modelType") or summary.get("model")
        if m:
            return m
    for key in ("lastUsedModel", "modelType", "model"):
        v = composer.get(key)
        if v and isinstance(v, str):
            return v
    return None


def load_cursor_data(conn: sqlite3.Connection):
    cur = conn.cursor()
    composers: dict[str, dict] = {}
    for key, value in cur.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ):
        data = safe_json(value)
        if not data:
            continue
        composers[key.split(":", 1)[1]] = data

    bubbles_by_composer: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for key, value in cur.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
    ):
        parts = key.split(":")
        if len(parts) < 3:
            continue
        composer_id, bubble_id = parts[1], parts[2]
        data = safe_json(value)
        if data:
            bubbles_by_composer[composer_id].append((bubble_id, data))

    return composers, bubbles_by_composer


def sort_bubbles(bubbles, composer):
    order = composer.get("fullConversationHeadersOnly") or []
    order_index = {
        h.get("bubbleId"): i
        for i, h in enumerate(order)
        if isinstance(h, dict) and h.get("bubbleId")
    }
    return sorted(bubbles, key=lambda item: order_index.get(item[0], 100_000))


def build_prompt_logs(composers, bubbles_by_composer) -> list[dict]:
    """Extract all prompt/response pairs from all composers."""
    prompt_logs: list[dict] = []

    for composer_id, composer in composers.items():
        raw_bubbles = bubbles_by_composer.get(composer_id, [])
        if not raw_bubbles:
            continue

        bubbles = sort_bubbles(raw_bubbles, composer)

        server_bubble_ids: set[str] = {
            h.get("bubbleId")
            for h in (composer.get("fullConversationHeadersOnly") or [])
            if isinstance(h, dict) and h.get("serverBubbleId") and h.get("bubbleId")
        }

        workspace = detect_workspace(composer)
        model = detect_model(composer)
        composer_created_ms = composer.get("createdAt")
        composer_created = ms_to_iso(composer_created_ms)
        composer_created_dt = ms_to_dt(composer_created_ms)
        composer_name = composer.get("name") or ""
        total_tokens = composer.get("tokenCount") or 0

        pending_user: dict | None = None
        pending_assistant_parts: list[str] = []

        def flush():
            nonlocal pending_user, pending_assistant_parts
            if pending_user is not None:
                response_text = "\n\n".join(p for p in pending_assistant_parts if p).strip()
                pending_user["response"] = response_text or None
                prompt_logs.append(pending_user)
            pending_user = None
            pending_assistant_parts = []

        for bubble_id, bubble in bubbles:
            btype = bubble.get("type")
            text = extract_text(bubble)
            context = extract_context(bubble)

            timing = bubble.get("timingInfo") or {}
            bubble_ts_ms = (
                timing.get("clientRpcSendTime")
                or timing.get("clientSettleTime")
                or bubble.get("createdAt")
                or bubble.get("timestamp")
                or composer_created_ms
            )
            bubble_ts = ms_to_iso(bubble_ts_ms)
            bubble_dt = ms_to_dt(bubble_ts_ms) or composer_created_dt

            if btype == BUBBLE_TYPE_USER:
                flush()
                pending_user = {
                    "composer_id": composer_id,
                    "composer_name": composer_name,
                    "bubble_id": bubble_id,
                    "workspace": workspace,
                    "prompt": text,
                    "response": None,
                    "model": model,
                    "total_conversation_tokens": total_tokens,
                    "context": context,
                    "created_at": bubble_ts,
                    # Store the datetime for filtering (not in final JSON)
                    "_created_dt": bubble_dt,
                    # Composer-level datetime for coarse filtering
                    "_composer_dt": composer_created_dt,
                    "unified_mode": composer.get("unifiedMode"),
                    "force_mode": composer.get("forceMode"),
                    "is_agentic": bool(composer.get("isAgentic")),
                }
                pending_assistant_parts = []

            elif btype == BUBBLE_TYPE_ASSISTANT:
                is_tool_only = not text
                is_server = bubble_id in server_bubble_ids
                if text and (is_server or not is_tool_only):
                    pending_assistant_parts.append(text)

        flush()

    return prompt_logs


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

def make_day_window(target_date: date) -> tuple[datetime, datetime]:
    """
    Return (start, end) UTC datetimes covering midnight-to-midnight LOCAL time
    for the given date.

    We use UTC+0 for simplicity. If you want local-time boundaries, set
    LOCAL_TZ_OFFSET_HOURS in your environment (e.g. "5.5" for IST).
    """
    offset_hours = float(os.environ.get("LOCAL_TZ_OFFSET_HOURS", "0"))
    offset = timedelta(hours=offset_hours)

    # Local midnight = UTC midnight - offset
    local_start = datetime(target_date.year, target_date.month, target_date.day,
                           0, 0, 0, tzinfo=timezone.utc) - offset
    local_end = local_start + timedelta(days=1)
    return local_start, local_end


def filter_by_date(
    prompt_logs: list[dict],
    target_date: date,
) -> list[dict]:
    """
    Return only prompts that fall within target_date (local midnight to midnight).

    Strategy:
      - Primary:  use _created_dt (bubble-level timestamp) if available and reliable
      - Fallback: use _composer_dt (conversation start) — less precise but better than nothing

    Because individual bubble timestamps are often absent, any prompt whose
    _created_dt is missing falls back to _composer_dt. This means a conversation
    started on May 14 with prompts added May 16 will only be captured correctly
    if the bubble has timing data. Without it, those later prompts will appear
    under May 14. This is a Cursor data limitation, not a bug in this script.
    """
    start, end = make_day_window(target_date)

    results = []
    for p in prompt_logs:
        dt = p.get("_created_dt") or p.get("_composer_dt")
        if dt and start <= dt < end:
            results.append(p)

    return results


def strip_internal_fields(prompt_logs: list[dict]) -> list[dict]:
    """Remove _ prefixed internal fields before writing to JSON or DB."""
    return [
        {k: v for k, v in p.items() if not k.startswith("_")}
        for p in prompt_logs
    ]


# ---------------------------------------------------------------------------
# PostgreSQL sync
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS prompt_logs (
    id                        SERIAL PRIMARY KEY,
    bubble_id                 TEXT UNIQUE NOT NULL,
    composer_id               TEXT NOT NULL,
    composer_name             TEXT,
    workspace                 TEXT,
    prompt                    TEXT NOT NULL,
    response                  TEXT,
    model                     TEXT,
    total_conversation_tokens INTEGER DEFAULT 0,
    context_attached_files    JSONB   DEFAULT '[]',
    context_attached_folders  JSONB   DEFAULT '[]',
    context_images            INTEGER DEFAULT 0,
    context_web_references    JSONB   DEFAULT '[]',
    context_docs_references   JSONB   DEFAULT '[]',
    context_is_agentic        BOOLEAN DEFAULT FALSE,
    unified_mode              TEXT,
    force_mode                TEXT,
    is_agentic                BOOLEAN DEFAULT FALSE,
    created_at                TIMESTAMPTZ,
    synced_at                 TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prompt_logs_composer_id  ON prompt_logs (composer_id);
CREATE INDEX IF NOT EXISTS idx_prompt_logs_created_at   ON prompt_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_prompt_logs_workspace    ON prompt_logs (workspace)
    WHERE workspace IS NOT NULL;
"""

UPSERT_SQL = """
INSERT INTO prompt_logs (
    bubble_id, composer_id, composer_name, workspace,
    prompt, response, model, total_conversation_tokens,
    context_attached_files, context_attached_folders,
    context_images, context_web_references, context_docs_references,
    context_is_agentic, unified_mode, force_mode, is_agentic, created_at
)
VALUES (
    %(bubble_id)s, %(composer_id)s, %(composer_name)s, %(workspace)s,
    %(prompt)s, %(response)s, %(model)s, %(total_conversation_tokens)s,
    %(context_attached_files)s, %(context_attached_folders)s,
    %(context_images)s, %(context_web_references)s, %(context_docs_references)s,
    %(context_is_agentic)s, %(unified_mode)s, %(force_mode)s, %(is_agentic)s,
    %(created_at)s
)
ON CONFLICT (bubble_id) DO UPDATE SET
    response                  = EXCLUDED.response,
    model                     = EXCLUDED.model,
    total_conversation_tokens = EXCLUDED.total_conversation_tokens,
    synced_at                 = NOW()
"""

EXISTS_SQL = "SELECT bubble_id FROM prompt_logs WHERE bubble_id = ANY(%s)"


def get_pg_conn(database_url: str):
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(database_url)
        return conn
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not connect to PostgreSQL: {e}")
        sys.exit(1)


def ensure_schema(pg_conn):
    import psycopg2.extras
    with pg_conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    pg_conn.commit()
    print("  schema OK")


def get_existing_bubble_ids(pg_conn, bubble_ids: list[str]) -> set[str]:
    """Return subset of bubble_ids that already exist in the DB."""
    if not bubble_ids:
        return set()
    with pg_conn.cursor() as cur:
        cur.execute(EXISTS_SQL, (bubble_ids,))
        return {row[0] for row in cur.fetchall()}


def sync_to_postgres(
    pg_conn,
    prompt_logs: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Upsert prompt_logs into PostgreSQL.
    Returns (inserted, skipped) counts.
    """
    import psycopg2.extras

    all_ids = [p["bubble_id"] for p in prompt_logs]
    existing = get_existing_bubble_ids(pg_conn, all_ids)

    new_rows = [p for p in prompt_logs if p["bubble_id"] not in existing]
    skipped = len(prompt_logs) - len(new_rows)

    if dry_run:
        print(f"  [dry-run] would insert {len(new_rows)}, skip {skipped} already in DB")
        for row in new_rows[:5]:
            print(f"    + {row['bubble_id']} | {row['created_at']} | {row['prompt'][:60]!r}")
        if len(new_rows) > 5:
            print(f"    ... and {len(new_rows) - 5} more")
        return len(new_rows), skipped

    if not new_rows:
        return 0, skipped

    rows_to_insert = []
    for p in new_rows:
        ctx = p.get("context") or {}
        rows_to_insert.append({
            "bubble_id":                 p["bubble_id"],
            "composer_id":               p["composer_id"],
            "composer_name":             p.get("composer_name"),
            "workspace":                 p.get("workspace"),
            "prompt":                    p["prompt"],
            "response":                  p.get("response"),
            "model":                     p.get("model"),
            "total_conversation_tokens": p.get("total_conversation_tokens") or 0,
            "context_attached_files":    json.dumps(ctx.get("attached_files", [])),
            "context_attached_folders":  json.dumps(ctx.get("attached_folders", [])),
            "context_images":            ctx.get("images", 0),
            "context_web_references":    json.dumps(ctx.get("web_references", [])),
            "context_docs_references":   json.dumps(ctx.get("docs_references", [])),
            "context_is_agentic":        bool(ctx.get("is_agentic", False)),
            "unified_mode":              p.get("unified_mode"),
            "force_mode":                p.get("force_mode"),
            "is_agentic":                bool(p.get("is_agentic", False)),
            "created_at":                p.get("created_at"),
        })

    with pg_conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows_to_insert, page_size=100)
    pg_conn.commit()

    return len(new_rows), skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Extract Cursor prompts and sync to PostgreSQL")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Filter to this date YYYY-MM-DD (default: today). Ignored if --all is set.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Sync ALL prompts not yet in the DB (no date filter).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be inserted without writing to DB.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Write JSON files only, skip DB sync.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="cursor_export",
        help="Output directory for JSON files (default: cursor_export).",
    )
    parser.add_argument(
        "--tz-offset",
        type=float,
        default=None,
        help=(
            "Your UTC offset in hours (e.g. 5.5 for IST, -5 for EST). "
            "Used to define 'midnight to midnight' in local time. "
            "Defaults to LOCAL_TZ_OFFSET_HOURS env var, or 0 (UTC) if not set."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Apply timezone offset
    if args.tz_offset is not None:
        os.environ["LOCAL_TZ_OFFSET_HOURS"] = str(args.tz_offset)

    # Resolve target date
    if args.all:
        target_date = None
    elif args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: Invalid date '{args.date}'. Use YYYY-MM-DD format.")
            sys.exit(1)
    else:
        target_date = date.today()

    out_dir = Path(args.out)
    database_url = os.environ.get("DATABASE_URL", "")

    if not database_url and not args.json_only:
        print("WARNING: DATABASE_URL not set. Switching to --json-only mode.")
        args.json_only = True

    # --- Extract ---
    db_path = cursor_global_db()
    print(f"Reading Cursor DB: {db_path}")
    conn = open_ro(db_path)
    try:
        composers, bubbles_by_composer = load_cursor_data(conn)
    finally:
        conn.close()

    print(f"  composers: {len(composers)}, with bubbles: "
          f"{sum(1 for c in composers if bubbles_by_composer.get(c))}")

    all_prompts = build_prompt_logs(composers, bubbles_by_composer)
    print(f"  total prompt/response pairs extracted: {len(all_prompts)}")

    # --- Filter ---
    if target_date is not None:
        start, end = make_day_window(target_date)
        filtered = filter_by_date(all_prompts, target_date)
        print(f"  date filter: {target_date} "
              f"(UTC {start.strftime('%H:%M')} → {end.strftime('%H:%M')})")
        print(f"  prompts matching date filter: {len(filtered)}")
    else:
        filtered = all_prompts
        print(f"  no date filter (--all mode)")

    # Strip internal _ fields before writing/inserting
    clean = strip_internal_fields(filtered)
    clean.sort(key=lambda r: r.get("created_at") or "", reverse=True)

    # --- Write JSON ---
    out_dir.mkdir(parents=True, exist_ok=True)

    if target_date:
        json_filename = f"prompt_logs_{target_date}.json"
    else:
        json_filename = "prompt_logs_all.json"

    json_path = out_dir / json_filename
    json_path.write_text(
        json.dumps(clean, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  wrote {json_path} ({len(clean)} records)")

    if args.json_only:
        print("Done (json-only mode).")
        return

    # --- Sync to PostgreSQL ---
    print(f"\nConnecting to PostgreSQL...")
    pg_conn = get_pg_conn(database_url)
    try:
        ensure_schema(pg_conn)
        inserted, skipped = sync_to_postgres(pg_conn, clean, dry_run=args.dry_run)
        if not args.dry_run:
            print(f"  inserted: {inserted}")
            print(f"  skipped (already in DB): {skipped}")
    finally:
        pg_conn.close()

    print("Done.")


if __name__ == "__main__":
    main()