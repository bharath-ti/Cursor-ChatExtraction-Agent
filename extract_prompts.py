"""
extract_prompts.py
------------------
Extract chat prompts and responses from Cursor's globalStorage/state.vscdb.

Key findings from debug_sample analysis:
  - tokenCount is always 0 on individual bubbles; only composer-level total exists
  - model name is NOT stored on bubbles; only inferred from latestConversationSummary
  - Each user turn is followed by MULTIPLE assistant bubbles (tool-call splits)
  - Assistant bubbles WITH serverBubbleId = actual LLM response
  - Assistant bubbles WITHOUT serverBubbleId = local tool/edit/apply bubbles
  - workspace is derived from allAttachedFileCodeChunksUris on the composer
  - composer.name gives the conversation title

Outputs:
  - prompt_logs.json     : flat list, one entry per user prompt
  - conversations.json   : grouped by composer/session
  - debug_sample.json    : raw dump when --debug flag is passed

Usage:
  python extract_prompts.py                  # writes to ./cursor_export
  python extract_prompts.py out_dir          # custom output dir
  python extract_prompts.py out_dir --debug  # also write debug_sample.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


BUBBLE_TYPE_USER = 1
BUBBLE_TYPE_ASSISTANT = 2


# ---------------------------------------------------------------------------
# DB paths
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# JSON / time helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

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
    # 1. plain text
    text = bubble.get("text") or ""
    if text.strip():
        return text.strip()

    # 2. richText
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

    # 3. codeBlocks (last resort)
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


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------

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
            r.get("url")
            for r in (bubble.get("webReferences") or [])
            if isinstance(r, dict) and r.get("url")
        ],
        "docs_references": [
            (r.get("name") or r.get("url"))
            for r in (bubble.get("docsReferences") or [])
            if isinstance(r, dict) and (r.get("name") or r.get("url"))
        ],
        "is_agentic": bool(bubble.get("isAgentic")),
    }


# ---------------------------------------------------------------------------
# Workspace + model
# ---------------------------------------------------------------------------

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
    # latestConversationSummary is most reliable
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


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_all(conn: sqlite3.Connection):
    cur = conn.cursor()

    composers: dict[str, dict] = {}
    for key, value in cur.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ):
        data = safe_json(value)
        if not data:
            continue
        composer_id = key.split(":", 1)[1]
        composers[composer_id] = data

    bubbles_by_composer: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for key, value in cur.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
    ):
        parts = key.split(":")
        if len(parts) < 3:
            continue
        composer_id, bubble_id = parts[1], parts[2]
        data = safe_json(value)
        if not data:
            continue
        bubbles_by_composer[composer_id].append((bubble_id, data))

    item_table: dict[str, object] = {}
    try:
        for key, value in cur.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE '%composer%' "
            "OR key LIKE '%aichat%' OR key LIKE '%aiService%'"
        ):
            data = safe_json(value)
            if data is not None:
                item_table[key] = data
    except sqlite3.OperationalError:
        pass

    return composers, bubbles_by_composer, item_table


# ---------------------------------------------------------------------------
# Build outputs
# ---------------------------------------------------------------------------

def _sort_bubbles(bubbles: list[tuple[str, dict]], composer: dict) -> list[tuple[str, dict]]:
    order = composer.get("fullConversationHeadersOnly") or []
    order_index: dict[str, int] = {}
    for i, h in enumerate(order):
        if isinstance(h, dict):
            bid = h.get("bubbleId")
            if bid:
                order_index[bid] = i

    return sorted(bubbles, key=lambda item: order_index.get(item[0], 100_000))


def build_outputs(composers: dict, bubbles_by_composer: dict):
    prompt_logs: list[dict] = []
    conversations: list[dict] = []

    for composer_id, composer in composers.items():
        raw_bubbles = bubbles_by_composer.get(composer_id, [])
        if not raw_bubbles:
            continue

        bubbles = _sort_bubbles(raw_bubbles, composer)

        # Build set of bubble IDs that have serverBubbleId (real LLM responses)
        # from the fullConversationHeadersOnly list — most reliable signal
        server_bubble_ids: set[str] = set()
        for h in (composer.get("fullConversationHeadersOnly") or []):
            if isinstance(h, dict) and h.get("serverBubbleId"):
                bid = h.get("bubbleId")
                if bid:
                    server_bubble_ids.add(bid)

        workspace = detect_workspace(composer)
        model = detect_model(composer)
        composer_created = ms_to_iso(composer.get("createdAt"))
        composer_name = composer.get("name") or ""
        total_tokens = composer.get("tokenCount") or 0

        conv_messages: list[dict] = []
        pending_user: dict | None = None
        pending_assistant_parts: list[str] = []

        def flush_pending():
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
            bubble_ts = ms_to_iso(
                timing.get("clientRpcSendTime")
                or timing.get("clientSettleTime")
                or bubble.get("createdAt")
                or bubble.get("timestamp")
            ) or composer_created

            is_server_bubble = bubble_id in server_bubble_ids
            is_tool_only = (
                btype == BUBBLE_TYPE_ASSISTANT
                and not text
            )

            role = (
                "user" if btype == BUBBLE_TYPE_USER
                else "assistant" if btype == BUBBLE_TYPE_ASSISTANT
                else f"other_{btype}"
            )

            conv_messages.append({
                "bubble_id": bubble_id,
                "role": role,
                "type": btype,
                "text": text,
                "is_server_bubble": is_server_bubble,
                "is_tool_only": is_tool_only,
                "created_at": bubble_ts,
            })

            if btype == BUBBLE_TYPE_USER:
                flush_pending()
                pending_user = {
                    "composer_id": composer_id,
                    "composer_name": composer_name,
                    "bubble_id": bubble_id,
                    "workspace": workspace,
                    "prompt": text,
                    "response": None,
                    "model": model,
                    # Per-turn tokens not available; total for whole conversation:
                    "total_conversation_tokens": total_tokens,
                    "context": context,
                    "created_at": bubble_ts,
                    "unified_mode": composer.get("unifiedMode"),
                    "force_mode": composer.get("forceMode"),
                    "is_agentic": bool(composer.get("isAgentic")),
                }
                pending_assistant_parts = []

            elif btype == BUBBLE_TYPE_ASSISTANT:
                # Collect text from real LLM bubbles; skip empty tool-only intermediates
                if text and (is_server_bubble or not is_tool_only):
                    pending_assistant_parts.append(text)

        flush_pending()

        conversations.append({
            "composer_id": composer_id,
            "composer_name": composer_name,
            "workspace": workspace,
            "model": model,
            "created_at": composer_created,
            "total_tokens": total_tokens,
            "unified_mode": composer.get("unifiedMode"),
            "force_mode": composer.get("forceMode"),
            "is_agentic": bool(composer.get("isAgentic")),
            "message_count": len(conv_messages),
            "messages": conv_messages,
        })

    prompt_logs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    conversations.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return prompt_logs, conversations


# ---------------------------------------------------------------------------
# Debug dump
# ---------------------------------------------------------------------------

def dump_debug_sample(composers, bubbles_by_composer, item_table, out_dir: Path):
    sample = {}
    for cid, comp in composers.items():
        bubs = bubbles_by_composer.get(cid, [])
        if bubs:
            sample = {
                "composer_id": cid,
                "composer_raw_keys": sorted(comp.keys()),
                "composer_raw": comp,
                "bubble_count": len(bubs),
                "user_bubble_keys": None,
                "assistant_bubble_keys": None,
                "user_bubble_raw": None,
                "assistant_bubble_raw": None,
            }
            for bid, b in bubs:
                if b.get("type") == BUBBLE_TYPE_USER and sample["user_bubble_raw"] is None:
                    sample["user_bubble_keys"] = sorted(b.keys())
                    sample["user_bubble_raw"] = b
                elif b.get("type") == BUBBLE_TYPE_ASSISTANT and sample["assistant_bubble_raw"] is None:
                    sample["assistant_bubble_keys"] = sorted(b.keys())
                    sample["assistant_bubble_raw"] = b
                if sample["user_bubble_raw"] and sample["assistant_bubble_raw"]:
                    break
            break
    sample["item_table_keys"] = sorted(item_table.keys())
    (out_dir / "debug_sample.json").write_text(
        json.dumps(sample, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"  wrote debug_sample.json")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]
    out_dir = Path(args[0]) if args else Path("cursor_export")

    db = cursor_global_db()
    print(f"Reading: {db}")
    conn = open_ro(db)
    try:
        composers, bubbles_by_composer, item_table = load_all(conn)
    finally:
        conn.close()

    print(f"  composers total:        {len(composers)}")
    print(f"  composers with bubbles: {sum(1 for c in composers if bubbles_by_composer.get(c))}")
    print(f"  total bubbles:          {sum(len(v) for v in bubbles_by_composer.values())}")

    prompt_logs, conversations = build_outputs(composers, bubbles_by_composer)

    with_response  = sum(1 for p in prompt_logs if p.get("response"))
    with_model     = sum(1 for p in prompt_logs if p.get("model"))
    with_workspace = sum(1 for p in prompt_logs if p.get("workspace"))
    with_name      = sum(1 for p in prompt_logs if p.get("composer_name"))
    print(f"  user prompts extracted:   {len(prompt_logs)}")
    print(f"    with response text:     {with_response} / {len(prompt_logs)}")
    print(f"    with model name:        {with_model} / {len(prompt_logs)}")
    print(f"    with workspace path:    {with_workspace} / {len(prompt_logs)}")
    print(f"    with conversation name: {with_name} / {len(prompt_logs)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prompt_logs.json").write_text(
        json.dumps(prompt_logs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "conversations.json").write_text(
        json.dumps(conversations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote → {out_dir / 'prompt_logs.json'}")
    print(f"Wrote → {out_dir / 'conversations.json'}")

    if debug:
        dump_debug_sample(composers, bubbles_by_composer, item_table, out_dir)


if __name__ == "__main__":
    main()