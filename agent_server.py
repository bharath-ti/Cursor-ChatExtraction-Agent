"""
agent_server.py  — Cloud version with Google OAuth
---------------------------------------------------
Deployed to Railway.

New endpoints:
  GET /auth/google           → redirect to Google consent screen
  GET /auth/google/callback  → handle token, create user, send welcome email
  GET /auth/success          → success page shown after OAuth
  GET /download/extension    → redirect to .vsix download

Existing endpoints:
  GET  /health
  GET  /status
  POST /ingest
  GET  /user/me
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.parse
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
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import httpx   # for Google API calls

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud_agent")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://cursor-chatextraction-agent-production.up.railway.app/auth/google/callback"
)
SENDER_EMAIL         = os.environ.get("SENDER_EMAIL", "")      # your Gmail address
ALLOWED_DOMAIN       = os.environ.get("ALLOWED_DOMAIN", "")    # e.g. "trilogy.com" or "" for any
EXTENSION_DOWNLOAD_URL = os.environ.get(
    "EXTENSION_DOWNLOAD_URL",
    "https://github.com/bharath-ti/Cursor-ProfileAgent-Extension/releases/download/v1.0.0/cursor-chat-sync-0.0.1.vsix"
)

# Google OAuth scopes
# openid + email + profile  → identity
# gmail.send                → send welcome email on their behalf (uses sender's token, not recipient's)
# documents + drive.file    → portfolio agent writes Google Docs
GOOGLE_SCOPES = " ".join([
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
])

GOOGLE_AUTH_URL     = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL    = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# In-memory state store for OAuth (state param → nonce)
# For production you'd use Redis, but this works fine for low traffic
_oauth_states: dict[str, str] = {}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    url = os.environ.get("PA_SOURCE_DB_URL", "")
    if not url:
        raise RuntimeError("PA_SOURCE_DB_URL not set")
    url = url.replace("postgresql+psycopg://", "postgresql://") \
             .replace("postgresql+psycopg2://", "postgresql://")
    return psycopg2.connect(url)


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

CREATE TABLE IF NOT EXISTS portfolio_agent.user_google_config (
    user_email        TEXT        PRIMARY KEY,
    google_folder_id  TEXT        NOT NULL DEFAULT '',
    google_token_json TEXT,
    configured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    token_updated_at  TIMESTAMPTZ
) ;
"""


def bootstrap_schema():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            # Ensure portfolio_agent schema exists
            cur.execute("CREATE SCHEMA IF NOT EXISTS portfolio_agent")
            cur.execute(SCHEMA_SQL)
        conn.commit()
        conn.close()
        logger.info("Schema bootstrap OK")
    except Exception as e:
        logger.error(f"Schema bootstrap failed: {e}")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def validate_api_key(x_api_key: str = Header(...)) -> dict:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-Api-Key header required")
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, email, display_name, is_active "
                "FROM public.users WHERE api_key = %s",
                (x_api_key,)
            )
            user = cur.fetchone()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database error")

    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account inactive")
    return dict(user)


# ---------------------------------------------------------------------------
# Gmail sender (uses OAuth token stored for the SENDER account)
# ---------------------------------------------------------------------------

def send_welcome_email(
    to_email: str,
    to_name: str,
    api_key: str,
    sender_access_token: str,
) -> bool:
    """
    Sends a welcome email via Gmail API using the authenticated user's access token.
    The sender must be the same account that completed OAuth (SENDER_EMAIL).
    """
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    subject = "You're set up on Cursor Chat Sync 🚀"

    html_body = f"""
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333;">

  <h2 style="color: #4A90E2;">Welcome to Cursor Chat Sync, {to_name}! 👋</h2>

  <p>You've successfully authenticated. Your Cursor chat sessions will now be
  automatically synced to the team's portfolio database.</p>

  <hr style="border: 1px solid #eee; margin: 20px 0;">

  <h3>Your Setup Details</h3>

  <table style="background: #f5f5f5; padding: 15px; border-radius: 8px; width: 100%;">
    <tr>
      <td><strong>Email:</strong></td>
      <td>{to_email}</td>
    </tr>
    <tr>
      <td><strong>API Key:</strong></td>
      <td style="font-family: monospace; color: #e74c3c;">{api_key}</td>
    </tr>
  </table>

  <hr style="border: 1px solid #eee; margin: 20px 0;">

  <h3>3 Steps to Get Started</h3>

  <ol style="line-height: 2;">
    <li>
      <strong>Download the Cursor extension</strong><br>
      <a href="{EXTENSION_DOWNLOAD_URL}" style="color: #4A90E2;">
        Download cursor-chat-sync.vsix
      </a>
    </li>
    <li>
      <strong>Install in Cursor</strong><br>
      <code style="background: #f0f0f0; padding: 3px 6px; border-radius: 3px;">
        Ctrl+Shift+P → Extensions: Install from VSIX → select the file → Reload
      </code>
    </li>
    <li>
      <strong>Configure with your API key</strong><br>
      Click the sync icon in the left sidebar → click "show" under Configuration
      → paste your API key above → click Save &amp; Connect
    </li>
  </ol>

  <p style="background: #e8f4fd; padding: 12px; border-radius: 6px; border-left: 4px solid #4A90E2;">
    💡 <strong>Keep your API key secret.</strong> It gives write access to your
    chat data. Don't share it or commit it to git.
  </p>

  <hr style="border: 1px solid #eee; margin: 20px 0;">

  <p style="color: #888; font-size: 12px;">
    Sent by Cursor Chat Sync · Trilogy Innovations<br>
    If you didn't sign up for this, you can ignore this email.
  </p>

</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL or to_email
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        import httpx as _httpx
        resp = _httpx.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {sender_access_token}"},
            json={"raw": raw},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info(f"Welcome email sent to {to_email}")
            return True
        else:
            logger.error(f"Gmail API error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role:      str
    content:   str
    timestamp: Optional[str] = None


class ChatRecord(BaseModel):
    chat_id:    str
    project_id: Optional[str] = None
    started_at: str
    ended_at:   str
    messages:   list[Message]
    metadata:   Optional[dict] = None


class IngestRequest(BaseModel):
    chats:           list[ChatRecord]
    client_version:  Optional[str] = None
    extracted_at:    Optional[str] = None


class IngestResponse(BaseModel):
    ok:       bool
    inserted: int
    updated:  int
    total:    int
    message:  str


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Cursor Chat Sync",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Google OAuth endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/google")
def auth_google():
    """
    Step 1: Redirect user to Google's OAuth consent screen.
    Anyone can visit this URL to register themselves.
    """
    if not GOOGLE_CLIENT_ID:
        return HTMLResponse(
            "<h2>OAuth not configured.</h2>"
            "<p>GOOGLE_CLIENT_ID is not set in Railway Variables.</p>",
            status_code=500
        )

    # Generate a random state token to prevent CSRF
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = datetime.now(timezone.utc).isoformat()

    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         GOOGLE_SCOPES,
        "state":         state,
        "access_type":   "offline",   # get refresh_token
        "prompt":        "consent",   # always show consent so we get refresh_token
    }

    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    """
    Step 2: Google redirects here after user consents.
    We exchange the code for tokens, create the user, send welcome email.
    """
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(_error_page(f"Google OAuth error: {error}"), status_code=400)

    if not code:
        return HTMLResponse(_error_page("No authorization code received from Google."), status_code=400)

    # Validate state (CSRF protection)
    if state not in _oauth_states:
        return HTMLResponse(_error_page("Invalid state parameter. Please try again."), status_code=400)
    del _oauth_states[state]

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        })

    if token_resp.status_code != 200:
        logger.error(f"Token exchange failed: {token_resp.text}")
        return HTMLResponse(_error_page("Failed to exchange token with Google."), status_code=500)

    tokens = token_resp.json()
    access_token  = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token:
        return HTMLResponse(_error_page("No access token received."), status_code=500)

    # Get user profile from Google
    async with httpx.AsyncClient() as client:
        profile_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if profile_resp.status_code != 200:
        return HTMLResponse(_error_page("Failed to get user profile from Google."), status_code=500)

    profile      = profile_resp.json()
    email        = profile.get("email", "").lower()
    display_name = profile.get("name", email.split("@")[0])
    google_id    = profile.get("sub", "")

    # Domain restriction check
    if ALLOWED_DOMAIN and not email.endswith(f"@{ALLOWED_DOMAIN}"):
        return HTMLResponse(
            _error_page(
                f"Access restricted to @{ALLOWED_DOMAIN} accounts. "
                f"You signed in with {email}."
            ),
            status_code=403
        )

    # Create or update user in DB
    user_id = email.split("@")[0].replace(".", "_")   # e.g. bharath_kumar
    api_key = "ccs_" + secrets.token_urlsafe(32)

    try:
        conn = get_db()

        # Check if user already exists
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT user_id, api_key FROM public.users WHERE email = %s", (email,))
            existing = cur.fetchone()

        is_new_user = existing is None

        if is_new_user:
            # Insert new user
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.users
                        (user_id, email, display_name, api_key, is_active)
                    VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (email) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        is_active    = TRUE
                    RETURNING api_key
                """, (user_id, email, display_name, api_key))
                row = cur.fetchone()
                api_key = row[0]
        else:
            # Use existing API key
            api_key = existing["api_key"]
            user_id = existing["user_id"]
            # Update display name
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.users SET display_name = %s WHERE email = %s",
                    (display_name, email)
                )

        # Store Google OAuth tokens for portfolio agent
        token_json = json.dumps({
            "token":         access_token,
            "refresh_token": refresh_token,
            "token_uri":     "https://oauth2.googleapis.com/token",
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "scopes":        GOOGLE_SCOPES.split(),
        })

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO portfolio_agent.user_google_config
                    (user_email, google_folder_id, google_token_json, token_updated_at)
                VALUES (%s, '', %s, NOW())
                ON CONFLICT (user_email) DO UPDATE SET
                    google_token_json = EXCLUDED.google_token_json,
                    token_updated_at  = NOW()
            """, (email, token_json))

        conn.commit()
        conn.close()

        logger.info(f"{'New' if is_new_user else 'Returning'} user: {email} ({user_id})")

    except Exception as e:
        logger.error(f"DB error during OAuth callback: {e}", exc_info=True)
        return HTMLResponse(_error_page(f"Database error: {str(e)}"), status_code=500)

    # Send welcome email (only to new users — returning users already know their key)
    email_sent = False
    if is_new_user and access_token:
        email_sent = send_welcome_email(email, display_name, api_key, access_token)

    # Redirect to success page
    params = urllib.parse.urlencode({
        "name":     display_name,
        "email":    email,
        "new":      "1" if is_new_user else "0",
        "emailed":  "1" if email_sent else "0",
        "api_key":  api_key,
    })
    return RedirectResponse(f"/auth/success?{params}")


@app.get("/auth/success", response_class=HTMLResponse)
def auth_success(
    name:    str = "",
    email:   str = "",
    new:     str = "1",
    emailed: str = "0",
    api_key: str = "",
):
    """Success page shown after OAuth completes."""
    is_new    = new == "1"
    was_mailed = emailed == "1"

    email_note = (
        "✅ A welcome email with your API key and setup instructions has been sent."
        if was_mailed else
        f"⚠️ Email could not be sent. Your API key is shown below — save it now."
    )

    returning_note = "Welcome back! Your Google tokens have been refreshed."

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Cursor Chat Sync — Connected!</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0f1117;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }}
    .card {{
      background: #1a1f2e;
      border-radius: 16px;
      padding: 40px;
      max-width: 520px;
      width: 100%;
      box-shadow: 0 20px 60px rgba(0,0,0,0.5);
      border: 1px solid #2d3748;
    }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 8px; color: #fff; }}
    .subtitle {{ color: #94a3b8; margin-bottom: 24px; font-size: 15px; }}
    .note {{
      background: #1e293b;
      border-left: 4px solid #3b82f6;
      padding: 12px 16px;
      border-radius: 6px;
      margin-bottom: 20px;
      font-size: 14px;
      color: #cbd5e1;
    }}
    .api-box {{
      background: #0f1117;
      border: 1px solid #2d3748;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 20px;
    }}
    .api-label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 6px; }}
    .api-key {{
      font-family: 'Courier New', monospace;
      font-size: 14px;
      color: #f472b6;
      word-break: break-all;
      cursor: pointer;
    }}
    .copy-hint {{ font-size: 11px; color: #475569; margin-top: 6px; }}
    .steps {{ margin-bottom: 24px; }}
    .steps h3 {{ font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px; }}
    .step {{
      display: flex;
      gap: 12px;
      margin-bottom: 12px;
      align-items: flex-start;
    }}
    .step-num {{
      background: #3b82f6;
      color: white;
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 700;
      flex-shrink: 0;
      margin-top: 1px;
    }}
    .step-text {{ font-size: 14px; color: #cbd5e1; line-height: 1.5; }}
    .download-btn {{
      display: block;
      background: #3b82f6;
      color: white;
      text-decoration: none;
      text-align: center;
      padding: 14px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 15px;
      transition: background 0.2s;
    }}
    .download-btn:hover {{ background: #2563eb; }}
    code {{
      background: #0f1117;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
      color: #94a3b8;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{"🎉" if is_new else "✅"}</div>
    <h1>{"You're all set, " + name + "!" if is_new else "Welcome back, " + name + "!"}</h1>
    <p class="subtitle">{email}</p>

    <div class="note">
      {email_note if is_new else returning_note}
    </div>

    {"" if not is_new else f'''
    <div class="api-box">
      <div class="api-label">Your API Key</div>
      <div class="api-key" onclick="navigator.clipboard.writeText(this.textContent).then(()=>this.style.color='#4ade80')">{api_key}</div>
      <div class="copy-hint">Click to copy</div>
    </div>
    '''}

    <div class="steps">
      <h3>Next Steps</h3>
      <div class="step">
        <div class="step-num">1</div>
        <div class="step-text">Download and install the Cursor extension below</div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <div class="step-text">
          <code>Ctrl+Shift+P</code> → <code>Extensions: Install from VSIX</code> → Reload
        </div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <div class="step-text">Click the sync icon in the sidebar → enter your API key → Save & Connect</div>
      </div>
    </div>

    <a href="{EXTENSION_DOWNLOAD_URL}" class="download-btn">
      ⬇️ Download Cursor Extension (.vsix)
    </a>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/download/extension")
def download_extension():
    """Redirect to the .vsix download."""
    return RedirectResponse(EXTENSION_DOWNLOAD_URL)


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Error — Cursor Chat Sync</title>
  <style>
    body {{ font-family: sans-serif; background: #0f1117; color: #e2e8f0;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; padding: 20px; }}
    .card {{ background: #1a1f2e; border-radius: 12px; padding: 32px;
             max-width: 480px; border: 1px solid #ef4444; }}
    h2 {{ color: #ef4444; margin-bottom: 12px; }}
    p {{ color: #94a3b8; margin-bottom: 20px; }}
    a {{ color: #3b82f6; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Something went wrong</h2>
    <p>{message}</p>
    <a href="/auth/google">← Try again</a>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    db_configured = bool(os.environ.get("PA_SOURCE_DB_URL"))
    oauth_configured = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    return {
        "ok": True,
        "version": "1.0.0",
        "time": datetime.now(timezone.utc).isoformat(),
        "db_configured": db_configured,
        "oauth_configured": oauth_configured,
    }


@app.get("/status")
def status(user: dict = Depends(validate_api_key)):
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
        conn.close()
        return {
            "user_id":      user["user_id"],
            "email":        user["email"],
            "total_chats":  total_chats,
            "last_sync_at": last_sync.isoformat() if last_sync else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/user/me")
def get_me(user: dict = Depends(validate_api_key)):
    return {
        "ok":           True,
        "user_id":      user["user_id"],
        "email":        user["email"],
        "display_name": user["display_name"],
    }




class FolderRequest(BaseModel):
    folder_id: str


@app.post("/user/folder")
def set_folder(req: FolderRequest, user: dict = Depends(validate_api_key)):
    """
    Extension POSTs the user's Google Drive folder ID here.
    Stored in portfolio_agent.user_google_config so the portfolio
    agent knows where to create monthly docs.
    """
    folder_id = req.folder_id.strip()
    if not folder_id:
        raise HTTPException(status_code=400, detail="folder_id is required")
    email = user["email"]
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO portfolio_agent.user_google_config
                    (user_email, google_folder_id, configured_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_email) DO UPDATE SET
                    google_folder_id = EXCLUDED.google_folder_id,
                    configured_at    = NOW()
            """, (email, folder_id))
        conn.commit()
        conn.close()
        logger.info(f"Folder set for {email}: {folder_id}")
        return {"ok": True, "message": f"Folder ID saved for {email}"}
    except Exception as e:
        logger.error(f"Folder set failed for {email}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/user/folder")
def get_folder(user: dict = Depends(validate_api_key)):
    """Returns the stored folder ID for the authenticated user."""
    email = user["email"]
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT google_folder_id FROM portfolio_agent.user_google_config "
                "WHERE user_email = %s", (email,)
            )
            row = cur.fetchone()
        conn.close()
        return {"ok": True, "folder_id": row[0] if row else ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest, user: dict = Depends(validate_api_key)):
    if not req.chats:
        return IngestResponse(ok=True, inserted=0, updated=0, total=0, message="No chats")

    user_id = user["user_id"]
    try:
        conn = get_db()
        all_ids = [c.chat_id for c in req.chats]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id FROM public.cursor_chats "
                "WHERE user_id = %s AND chat_id = ANY(%s)",
                (user_id, all_ids)
            )
            existing = {row[0] for row in cur.fetchall()}

        rows = [{
            "chat_id":        c.chat_id,
            "user_id":        user_id,
            "project_id":     c.project_id,
            "started_at":     c.started_at,
            "ended_at":       c.ended_at,
            "messages_jsonb": json.dumps([m.model_dump() for m in c.messages]),
            "metadata_jsonb": json.dumps(c.metadata or {}),
        } for c in req.chats]

        psycopg2.extras.execute_batch(conn.cursor(), """
            INSERT INTO public.cursor_chats
                (chat_id, user_id, project_id, started_at, ended_at,
                 messages_jsonb, metadata_jsonb, synced_at)
            VALUES
                (%(chat_id)s, %(user_id)s, %(project_id)s,
                 %(started_at)s, %(ended_at)s,
                 %(messages_jsonb)s::jsonb, %(metadata_jsonb)s::jsonb, NOW())
            ON CONFLICT (chat_id) DO UPDATE SET
                ended_at       = EXCLUDED.ended_at,
                messages_jsonb = EXCLUDED.messages_jsonb,
                metadata_jsonb = EXCLUDED.metadata_jsonb,
                synced_at      = NOW()
        """, rows, page_size=50)
        conn.commit()
        conn.close()

        inserted = len([r for r in rows if r["chat_id"] not in existing])
        updated  = len(rows) - inserted

        logger.info(f"Ingest: {user_id} inserted={inserted} updated={updated}")
        return IngestResponse(
            ok=True, inserted=inserted, updated=updated,
            total=len(rows),
            message=f"Synced {len(rows)} conversations ({inserted} new, {updated} updated)",
        )
    except Exception as e:
        logger.error(f"Ingest error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Startup & entry point
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    bootstrap_schema()
    logger.info("Server ready")
    if not GOOGLE_CLIENT_ID:
        logger.warning("GOOGLE_CLIENT_ID not set — OAuth will not work")
    if not SENDER_EMAIL:
        logger.warning("SENDER_EMAIL not set — welcome emails will not be sent")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")