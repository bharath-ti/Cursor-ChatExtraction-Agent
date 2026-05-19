"""
agent_server.py  — Cloud version
---------------------------------
Deployed to Railway. Receives already-extracted chat data
from the Cursor extension and stores it in Neon Postgres.

The extension reads the local Cursor SQLite DB and POSTs here.
This server never touches the user's machine.

Endpoints:
  GET  /health              → Railway health check
  GET  /status              → server stats
  POST /ingest              → extension sends extracted chats
  GET  /user/me             → extension checks if API key is valid
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2
import psycopg2.extras
import uvicorn
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("cloud_agent")


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def get_db():
    url = os.environ.get("PA_SOURCE_DB_URL", "")
    if not url:
        raise RuntimeError("PA_SOURCE_DB_URL environment variable not set")
    url = (url
           .replace("postgresql+psycopg://", "postgresql://")
           .replace("postgresql+psycopg2://", "postgresql://"))
    return psycopg2.connect(url)


# ---------------------------------------------------------------------------
# Schema bootstrap (runs once on startup)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public.users (
    user_id      TEXT    PRIMARY KEY,
    email        TEXT    NOT NULL UNIQUE,
    display_name TEXT,
    role         TEXT,
    api_key      TEXT    UNIQUE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.cursor_chats (
    chat_id        TEXT        PRIMARY KEY,
    user_id        TEXT        NOT NULL REFERENCES public.users(user_id),
    project_id     TEXT,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ NOT NULL,
    messages_jsonb JSONB       NOT NULL,
    metadata_jsonb JSONB,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cursor_chats_user_started
    ON public.cursor_chats(user_id, started_at);

CREATE INDEX IF NOT EXISTS idx_cursor_chats_synced
    ON public.cursor_chats(synced_at DESC);
"""


def bootstrap_schema():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        conn.close()
        logger.info("Schema bootstrap OK")
    except Exception as e:
        logger.error(f"Schema bootstrap failed: {e}")
        logger.error("Server will still start — check PA_SOURCE_DB_URL in Railway Variables")


# ---------------------------------------------------------------------------
# Auth — API key validation
# ---------------------------------------------------------------------------

def validate_api_key(
    x_api_key: str = Header(..., description="Your API key"),
) -> dict:
    """
    Dependency — validates the X-Api-Key header.
    Returns the user row if valid, raises 401 if not.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-Api-Key header required")

    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, email, display_name, is_active "
                "FROM public.users "
                "WHERE api_key = %s",
                (x_api_key,)
            )
            user = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.error(f"DB error during auth: {e}")
        raise HTTPException(status_code=500, detail="Database error")

    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account inactive")

    return dict(user)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str                    # "user" or "assistant"
    content: str
    timestamp: Optional[str] = None


class ChatRecord(BaseModel):
    chat_id: str                 # composer_id from Cursor
    project_id: Optional[str] = None
    started_at: str              # ISO8601 UTC
    ended_at: str                # ISO8601 UTC
    messages: list[Message]
    metadata: Optional[dict] = None


class IngestRequest(BaseModel):
    chats: list[ChatRecord]
    client_version: Optional[str] = None
    extracted_at: Optional[str] = None


class IngestResponse(BaseModel):
    ok: bool
    inserted: int
    updated: int
    total: int
    message: str


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Cursor Chat Sync — Cloud Server",
    description=(
        "Receives extracted Cursor chat data from the extension "
        "and stores it in Neon Postgres for the portfolio agent."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Railway health check — must always return 200, never touches DB."""
    db_configured = bool(os.environ.get("PA_SOURCE_DB_URL"))
    return {
        "ok": True,
        "version": "1.0.0",
        "time": datetime.now(timezone.utc).isoformat(),
        "db_configured": db_configured,
    }


@app.get("/status")
def status(user: dict = Depends(validate_api_key)):
    """Returns sync stats for the authenticated user."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.cursor_chats WHERE user_id = %s",
                (user["user_id"],)
            )
            total_chats = cur.fetchone()[0]

            cur.execute(
                "SELECT MAX(synced_at) FROM public.cursor_chats WHERE user_id = %s",
                (user["user_id"],)
            )
            last_sync = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM public.cursor_chats "
                "WHERE user_id = %s AND synced_at > NOW() - INTERVAL '24 hours'",
                (user["user_id"],)
            )
            synced_today = cur.fetchone()[0]

        conn.close()
        return {
            "user_id": user["user_id"],
            "email": user["email"],
            "total_chats": total_chats,
            "last_sync_at": last_sync.isoformat() if last_sync else None,
            "synced_last_24h": synced_today,
        }
    except Exception as e:
        logger.error(f"Status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/user/me")
def get_me(user: dict = Depends(validate_api_key)):
    """
    Extension calls this to verify the API key is valid
    and get the user's display name.
    """
    return {
        "ok": True,
        "user_id": user["user_id"],
        "email": user["email"],
        "display_name": user["display_name"],
    }


@app.post("/ingest", response_model=IngestResponse)
def ingest(
    req: IngestRequest,
    user: dict = Depends(validate_api_key),
):
    """
    Main endpoint. Extension POSTs extracted chats here.
    Upserts all conversations for the authenticated user.
    """
    if not req.chats:
        return IngestResponse(
            ok=True, inserted=0, updated=0, total=0,
            message="No chats to sync"
        )

    user_id = user["user_id"]
    logger.info(
        f"Ingest: user={user_id}, "
        f"chats={len(req.chats)}, "
        f"client_version={req.client_version}"
    )

    try:
        conn = get_db()

        # Find which chat_ids already exist
        all_ids = [c.chat_id for c in req.chats]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id FROM public.cursor_chats "
                "WHERE user_id = %s AND chat_id = ANY(%s)",
                (user_id, all_ids)
            )
            existing = {row[0] for row in cur.fetchall()}

        # Build rows for batch upsert
        rows = []
        for chat in req.chats:
            messages_list = [m.model_dump() for m in chat.messages]
            rows.append({
                "chat_id":        chat.chat_id,
                "user_id":        user_id,
                "project_id":     chat.project_id,
                "started_at":     chat.started_at,
                "ended_at":       chat.ended_at,
                "messages_jsonb": json.dumps(messages_list),
                "metadata_jsonb": json.dumps(chat.metadata or {}),
            })

        # Upsert
        psycopg2.extras.execute_batch(conn.cursor(), """
            INSERT INTO public.cursor_chats
                (chat_id, user_id, project_id, started_at, ended_at,
                 messages_jsonb, metadata_jsonb, synced_at)
            VALUES
                (%(chat_id)s, %(user_id)s, %(project_id)s,
                 %(started_at)s, %(ended_at)s,
                 %(messages_jsonb)s::jsonb, %(metadata_jsonb)s::jsonb,
                 NOW())
            ON CONFLICT (chat_id) DO UPDATE SET
                ended_at       = EXCLUDED.ended_at,
                messages_jsonb = EXCLUDED.messages_jsonb,
                metadata_jsonb = EXCLUDED.metadata_jsonb,
                synced_at      = NOW()
        """, rows, page_size=50)
        conn.commit()
        conn.close()

        inserted = len([r for r in rows if r["chat_id"] not in existing])
        updated = len(rows) - inserted

        logger.info(
            f"Ingest complete: user={user_id}, "
            f"inserted={inserted}, updated={updated}"
        )

        return IngestResponse(
            ok=True,
            inserted=inserted,
            updated=updated,
            total=len(rows),
            message=f"Synced {len(rows)} conversations "
                    f"({inserted} new, {updated} updated)",
        )

    except Exception as e:
        logger.error(f"Ingest error for {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    bootstrap_schema()
    logger.info("Cloud agent server ready")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")