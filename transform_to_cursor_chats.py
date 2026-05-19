"""
transform_to_cursor_chats.py
-----------------------------
Transforms the output of extract_and_sync.py into the shape that
portfolio_agent expects in public.cursor_chats.

Input:  cursor_export/prompt_logs_all.json  (or any prompt_logs_*.json)
Output: public.users + public.cursor_chats rows, pushed to Neon Postgres

Schema expected (from DB_SCHEMA.md):

  public.users:
    user_id      TEXT PRIMARY KEY
    email        TEXT NOT NULL
    display_name TEXT
    role         TEXT
    is_active    BOOLEAN DEFAULT TRUE

  public.cursor_chats:
    chat_id        TEXT PRIMARY KEY          ← composer_id
    user_id        TEXT REFERENCES users     ← hardcoded from USER_ID env var
    project_id     TEXT                      ← workspace (derived path)
    started_at     TIMESTAMPTZ               ← created_at of first message
    ended_at       TIMESTAMPTZ               ← created_at of last message
    messages_jsonb JSONB                     ← [{role, content, timestamp}, ...]
    metadata_jsonb JSONB                     ← composer_name, model, is_agentic, etc.

Key transformation:
  Your prompt_logs are FLAT (one row per prompt).
  cursor_chats wants one row per CONVERSATION with all messages as a JSON array.
  This script groups prompt_logs by composer_id and rebuilds the messages array.

Usage:
  python transform_to_cursor_chats.py --input cursor_export/prompt_logs_all.json

  # First run: also creates the users row
  python transform_to_cursor_chats.py --input cursor_export/prompt_logs_all.json --init-user

  # Preview without writing
  python transform_to_cursor_chats.py --input cursor_export/prompt_logs_all.json --dry-run

Environment variables (in .env or exported):
  PA_SOURCE_DB_URL   postgresql+psycopg://user:password@host/db?sslmode=require
  CURSOR_USER_ID     your unique user ID (e.g. "bharath" or your email)
  CURSOR_USER_EMAIL  your email address (used in public.users)
  CURSOR_USER_NAME   your display name  (used in public.users)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

def get_config() -> dict:
    db_url = os.environ.get("PA_SOURCE_DB_URL", "")
    user_id = os.environ.get("CURSOR_USER_ID", "")
    user_email = os.environ.get("CURSOR_USER_EMAIL", "")
    user_name = os.environ.get("CURSOR_USER_NAME", "")

    missing = []
    if not db_url:
        missing.append("PA_SOURCE_DB_URL")
    if not user_id:
        missing.append("CURSOR_USER_ID")
    if not user_email:
        missing.append("CURSOR_USER_EMAIL")

    return {
        "db_url": db_url,
        "user_id": user_id,
        "user_email": user_email,
        "user_name": user_name,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Transformation logic
# ---------------------------------------------------------------------------

def parse_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def group_by_conversation(prompt_logs: list[dict]) -> dict[str, list[dict]]:
    """Group flat prompt rows by composer_id, preserving order."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in prompt_logs:
        cid = row.get("composer_id")
        if cid:
            groups[cid].append(row)
    return groups


def build_messages_jsonb(rows: list[dict]) -> list[dict]:
    """
    Convert flat prompt/response pairs into the messages array format
    the agent expects:
      [
        {"role": "user",      "content": "...", "timestamp": "..."},
        {"role": "assistant", "content": "...", "timestamp": "..."},
        ...
      ]

    Each prompt_log row represents one user turn + one assistant turn.
    We interleave them in order.
    """
    messages = []
    for row in rows:
        prompt = (row.get("prompt") or "").strip()
        response = (row.get("response") or "").strip()
        ts = row.get("created_at")

        if prompt:
            messages.append({
                "role": "user",
                "content": prompt,
                "timestamp": ts,
            })
        if response:
            messages.append({
                "role": "assistant",
                "content": response,
                "timestamp": ts,
            })

    return messages


def build_metadata_jsonb(rows: list[dict]) -> dict:
    """
    Pack conversation-level metadata into metadata_jsonb.
    The agent doesn't use this for scoring, but it's useful for debugging.
    """
    first = rows[0] if rows else {}
    return {
        "composer_name":       first.get("composer_name"),
        "model":               first.get("model"),
        "unified_mode":        first.get("unified_mode"),
        "force_mode":          first.get("force_mode"),
        "is_agentic":          first.get("is_agentic", False),
        "total_tokens":        first.get("total_conversation_tokens", 0),
        "prompt_count":        len(rows),
        "has_attached_files":  any(
            bool((r.get("context") or {}).get("attached_files"))
            for r in rows
        ),
    }


def transform(
    prompt_logs: list[dict],
    user_id: str,
) -> list[dict]:
    """
    Transform flat prompt_logs into cursor_chats rows.
    Returns list of dicts ready for INSERT.
    """
    groups = group_by_conversation(prompt_logs)
    chat_rows = []

    for composer_id, rows in groups.items():
        # Sort rows within conversation by created_at
        rows.sort(key=lambda r: r.get("created_at") or "")

        messages = build_messages_jsonb(rows)
        if not messages:
            continue  # Skip empty conversations

        metadata = build_metadata_jsonb(rows)

        # Timestamps: use first/last message timestamps
        timestamps = [r.get("created_at") for r in rows if r.get("created_at")]
        started_at = timestamps[0] if timestamps else None
        ended_at = timestamps[-1] if timestamps else None

        # started_at is NOT NULL in the schema.
        # Many composers have no createdAt stored in Cursor's DB.
        # Fallback chain:
        #   1. created_at from prompt rows (from composer.createdAt)
        #   2. last_updated_at from prompt rows (from composer.lastUpdatedAt)
        #   3. Current UTC time as last resort (better than skipping)
        if not started_at:
            # Try lastUpdatedAt stored in metadata
            last_updated = rows[0].get("last_updated_at")
            if last_updated:
                started_at = last_updated
                ended_at = last_updated
            else:
                # Use current time — marks it as "timestamp unknown, synced now"
                now = datetime.now(timezone.utc).isoformat()
                started_at = now
                ended_at = now
                print(f"  WARN (no timestamp, using now): {composer_id[:8]}... "
                      f"'{rows[0].get('composer_name') or ''}'")

        # ended_at must also never be null
        if not ended_at:
            ended_at = started_at
        # project_id from workspace — take the last path component for readability
        workspace = rows[0].get("workspace")
        project_id = None
        if workspace:
            try:
                project_id = Path(workspace).name or workspace
            except Exception:
                project_id = workspace

        chat_rows.append({
            "chat_id":        composer_id,
            "user_id":        user_id,
            "project_id":     project_id,
            "started_at":     started_at,
            "ended_at":       ended_at,
            "messages_jsonb": json.dumps(messages),
            "metadata_jsonb": json.dumps(metadata),
        })

    return chat_rows


# ---------------------------------------------------------------------------
# PostgreSQL operations
# ---------------------------------------------------------------------------

CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS public.users (
    user_id      TEXT    PRIMARY KEY,
    email        TEXT    NOT NULL,
    display_name TEXT,
    role         TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);
"""

CREATE_CURSOR_CHATS_SQL = """
CREATE TABLE IF NOT EXISTS public.cursor_chats (
    chat_id        TEXT        PRIMARY KEY,
    user_id        TEXT        NOT NULL REFERENCES public.users(user_id),
    project_id     TEXT,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ,
    messages_jsonb JSONB       NOT NULL,
    metadata_jsonb JSONB
);

CREATE INDEX IF NOT EXISTS cursor_chats_user_started_idx
    ON public.cursor_chats(user_id, started_at);
"""

UPSERT_USER_SQL = """
INSERT INTO public.users (user_id, email, display_name, is_active)
VALUES (%(user_id)s, %(email)s, %(display_name)s, TRUE)
ON CONFLICT (user_id) DO UPDATE SET
    email        = EXCLUDED.email,
    display_name = EXCLUDED.display_name;
"""

UPSERT_CHAT_SQL = """
INSERT INTO public.cursor_chats (
    chat_id, user_id, project_id, started_at, ended_at,
    messages_jsonb, metadata_jsonb
)
VALUES (
    %(chat_id)s, %(user_id)s, %(project_id)s, %(started_at)s, %(ended_at)s,
    %(messages_jsonb)s::jsonb, %(metadata_jsonb)s::jsonb
)
ON CONFLICT (chat_id) DO UPDATE SET
    ended_at       = EXCLUDED.ended_at,
    messages_jsonb = EXCLUDED.messages_jsonb,
    metadata_jsonb = EXCLUDED.metadata_jsonb;
"""

EXISTS_CHATS_SQL = """
SELECT chat_id FROM public.cursor_chats WHERE chat_id = ANY(%s)
"""


def get_pg_conn(db_url: str):
    # Strip SQLAlchemy prefix if present — psycopg2 needs plain postgresql://
    url = db_url.replace("postgresql+psycopg://", "postgresql://") \
                .replace("postgresql+psycopg2://", "postgresql://")
    try:
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(url)
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Cannot connect to Postgres: {e}")
        sys.exit(1)


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(CREATE_USERS_SQL)
        cur.execute(CREATE_CURSOR_CHATS_SQL)
    conn.commit()
    print("  tables OK (created if not existed)")


def upsert_user(conn, user_id: str, email: str, display_name: str):
    with conn.cursor() as cur:
        cur.execute(UPSERT_USER_SQL, {
            "user_id": user_id,
            "email": email,
            "display_name": display_name or email,
        })
    conn.commit()
    print(f"  user upserted: {user_id} ({email})")


def get_existing_chat_ids(conn, chat_ids: list[str]) -> set[str]:
    if not chat_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(EXISTS_CHATS_SQL, (chat_ids,))
        return {row[0] for row in cur.fetchall()}


def upsert_chats(conn, chat_rows: list[dict], dry_run: bool = False) -> tuple[int, int]:
    import psycopg2.extras

    all_ids = [r["chat_id"] for r in chat_rows]
    existing = get_existing_chat_ids(conn, all_ids)
    new_rows = [r for r in chat_rows if r["chat_id"] not in existing]
    # Always re-upsert existing too (messages may have grown)
    # Actually: upsert all — ON CONFLICT updates messages if they changed
    skipped = 0

    if dry_run:
        print(f"  [dry-run] would upsert {len(chat_rows)} conversations "
              f"({len(new_rows)} new, {len(existing)} updates)")
        for r in new_rows[:5]:
            msgs = json.loads(r["messages_jsonb"])
            print(f"    + {r['chat_id']} | {r['started_at']} | "
                  f"{len(msgs)} messages | project={r['project_id']}")
        if len(new_rows) > 5:
            print(f"    ... and {len(new_rows) - 5} more")
        return len(new_rows), skipped

    psycopg2.extras.execute_batch(
        conn.cursor(), UPSERT_CHAT_SQL, chat_rows, page_size=50
    )
    conn.commit()
    return len(new_rows), len(existing)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Transform extracted Cursor prompts into cursor_chats schema"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to prompt_logs JSON file (e.g. cursor_export/prompt_logs_all.json)",
    )
    parser.add_argument(
        "--init-user",
        action="store_true",
        help="Create/upsert the user row in public.users before inserting chats.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be inserted without touching the DB.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Also write transformed data to this JSON file for inspection.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = get_config()

    if cfg["missing"] and not args.dry_run:
        print(f"ERROR: Missing required environment variables: {', '.join(cfg['missing'])}")
        print("\nAdd these to your .env file:")
        for var in cfg["missing"]:
            print(f"  {var}=...")
        sys.exit(1)

    # Load prompt_logs
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        print("Run extract_and_sync.py --all --json-only first to generate it.")
        sys.exit(1)

    print(f"Loading {input_path}...")
    prompt_logs = json.loads(input_path.read_text(encoding="utf-8"))
    print(f"  {len(prompt_logs)} prompt/response pairs loaded")

    # Transform
    print("Transforming to cursor_chats format...")
    chat_rows = transform(prompt_logs, cfg["user_id"] or "local_user")
    print(f"  {len(chat_rows)} conversations built")

    # Count total messages
    total_messages = sum(
        len(json.loads(r["messages_jsonb"])) for r in chat_rows
    )
    print(f"  {total_messages} total messages across all conversations")

    # Optionally write JSON for inspection
    if args.out:
        out_path = Path(args.out)
        # Make it human-readable by parsing the jsonb strings back
        readable = []
        for r in chat_rows:
            readable.append({
                **r,
                "messages_jsonb": json.loads(r["messages_jsonb"]),
                "metadata_jsonb": json.loads(r["metadata_jsonb"]),
            })
        out_path.write_text(
            json.dumps(readable, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  wrote inspection file: {out_path}")

    if args.dry_run and not cfg["db_url"]:
        print("\n[dry-run complete — no DB URL set, skipping DB preview]")
        return

    # Push to Postgres
    print(f"\nConnecting to Postgres ({cfg['db_url'][:40]}...)...")
    conn = get_pg_conn(cfg["db_url"])

    try:
        ensure_tables(conn)

        if args.init_user:
            upsert_user(
                conn,
                cfg["user_id"],
                cfg["user_email"],
                cfg["user_name"],
            )

        inserted, updated = upsert_chats(conn, chat_rows, dry_run=args.dry_run)

        if not args.dry_run:
            print(f"\nDone.")
            print(f"  new conversations inserted: {inserted}")
            print(f"  existing conversations updated: {updated}")
            print(f"\nVerify with:")
            print(f"  SELECT COUNT(*) FROM public.cursor_chats WHERE user_id = '{cfg['user_id']}';")
            print(f"  SELECT chat_id, project_id, started_at,")
            print(f"         jsonb_array_length(messages_jsonb) AS msg_count")
            print(f"  FROM public.cursor_chats")
            print(f"  WHERE user_id = '{cfg['user_id']}'")
            print(f"  ORDER BY started_at DESC LIMIT 10;")

    finally:
        conn.close()


if __name__ == "__main__":
    main()