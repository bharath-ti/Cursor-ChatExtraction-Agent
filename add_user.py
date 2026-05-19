"""
add_user.py
-----------
Admin script to add users to the DB and generate their API keys.
Run this locally — never expose it in the deployed server.

Usage:
  python add_user.py --email bharath@trilogy.com --name "Bharath Kumar" --id bharath
  python add_user.py --email intern1@trilogy.com --name "Intern One"  --id intern1
  python add_user.py --list     # show all users and their API keys
  python add_user.py --disable bharath   # deactivate a user
"""

import argparse
import os
import secrets
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2
import psycopg2.extras


def get_conn():
    url = os.environ.get("PA_SOURCE_DB_URL", "")
    if not url:
        print("ERROR: PA_SOURCE_DB_URL not set in .env")
        sys.exit(1)
    url = url.replace("postgresql+psycopg://", "postgresql://")
    return psycopg2.connect(url)


def add_user(user_id: str, email: str, name: str):
    api_key = "ccs_" + secrets.token_urlsafe(32)  # ccs = cursor chat sync
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.users
                    (user_id, email, display_name, api_key, is_active)
                VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET
                    email        = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    api_key      = EXCLUDED.api_key,
                    is_active    = TRUE
                RETURNING user_id, email, api_key
            """, (user_id, email, name, api_key))
            row = cur.fetchone()
        conn.commit()

        print(f"\n✅ User added successfully!")
        print(f"   User ID:  {row[0]}")
        print(f"   Email:    {row[1]}")
        print(f"   API Key:  {row[2]}")
        print(f"\n   Share this API key with {name}.")
        print(f"   They'll enter it in the Cursor extension settings.")
        print(f"   Keep it secret — it gives full write access for that user.\n")

    finally:
        conn.close()


def list_users():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    u.user_id,
                    u.email,
                    u.display_name,
                    u.api_key,
                    u.is_active,
                    u.created_at,
                    COUNT(c.chat_id) AS total_chats,
                    MAX(c.synced_at) AS last_sync
                FROM public.users u
                LEFT JOIN public.cursor_chats c USING (user_id)
                GROUP BY u.user_id, u.email, u.display_name,
                         u.api_key, u.is_active, u.created_at
                ORDER BY u.created_at
            """)
            users = cur.fetchall()
    finally:
        conn.close()

    if not users:
        print("No users found.")
        return

    print(f"\n{'ID':<15} {'Email':<35} {'Active':<8} {'Chats':<8} {'Last Sync':<22} API Key")
    print("-" * 110)
    for u in users:
        last = u["last_sync"].strftime("%Y-%m-%d %H:%M") if u["last_sync"] else "never"
        status = "✅" if u["is_active"] else "❌"
        print(
            f"{u['user_id']:<15} {u['email']:<35} {status:<8} "
            f"{u['total_chats']:<8} {last:<22} {u['api_key']}"
        )
    print()


def disable_user(user_id: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.users SET is_active = FALSE WHERE user_id = %s "
                "RETURNING email",
                (user_id,)
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    if row:
        print(f"✅ Disabled user {user_id} ({row[0]})")
    else:
        print(f"❌ User {user_id} not found")


def main():
    parser = argparse.ArgumentParser(description="Manage Cursor Chat Sync users")
    parser.add_argument("--email", help="User email address")
    parser.add_argument("--name",  help="Display name")
    parser.add_argument("--id",    help="User ID (short, e.g. 'bharath')")
    parser.add_argument("--list",  action="store_true", help="List all users")
    parser.add_argument("--disable", metavar="USER_ID", help="Disable a user")
    args = parser.parse_args()

    if args.list:
        list_users()
    elif args.disable:
        disable_user(args.disable)
    elif args.email:
        user_id = args.id or args.email.split("@")[0]
        name = args.name or args.email
        add_user(user_id, args.email, name)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()