"""
Vercel Cron Job — runs every minute.
For every user with autoclick_enabled=True, connects their Telethon client,
finds the latest DropmailBot message with Restore buttons, and clicks all of them.
"""
from http.server import BaseHTTPRequestHandler
import asyncio
import json
import logging
import os

import psycopg2
import requests as req

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cron")

BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_ID        = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH      = os.environ.get("TELEGRAM_API_HASH", "")
NEON_DSN      = os.environ.get("NEON_DATABASE_URL", "")
BOT_API       = f"https://api.telegram.org/bot{BOT_TOKEN}"
DROPMAIL_USER = "DropmailBot"
TRIGGER_TEXT  = "restore"
CRON_SECRET   = os.environ.get("CRON_SECRET", "")   # optional guard


# ────────────────────── DB helpers ──────────────────────

def get_db():
    return psycopg2.connect(NEON_DSN)


def get_all_autoclick_users() -> list[dict]:
    """Return list of {uid, session} for users with autoclick_enabled."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.uid, s.session
                    FROM   user_sessions s
                    JOIN   user_configs  c ON c.uid = s.uid
                    WHERE  c.autoclick_enabled = TRUE
                """)
                return [{"uid": row[0], "session": row[1]} for row in cur.fetchall()]
    except Exception as e:
        log.warning(f"get_all_autoclick_users error: {e}")
        return []


def get_clicked_ids(uid: int) -> set:
    """Return set of message IDs already clicked for this user."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(clicked_ids, '[]'::jsonb)
                    FROM   user_configs
                    WHERE  uid = %s
                """, (uid,))
                row = cur.fetchone()
                return set(row[0]) if row else set()
    except Exception as e:
        log.warning(f"get_clicked_ids error: {e}")
        return set()


def save_clicked_id(uid: int, msg_id: int):
    """Append msg_id to the clicked_ids array, keep last 100 only."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE user_configs
                    SET    clicked_ids = (
                               COALESCE(clicked_ids, '[]'::jsonb) || to_jsonb(%s::bigint)
                           ) -> -100::int   -- keep last 100
                    WHERE  uid = %s
                """, (msg_id, uid))
            conn.commit()
    except Exception as e:
        log.warning(f"save_clicked_id error: {e}")


def send_msg(chat_id: int, text: str):
    try:
        req.post(f"{BOT_API}/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
        }, timeout=8)
    except Exception:
        pass


# ────────────────────── Auto-click logic ──────────────────────

async def autoclick_user(uid: int, session_str: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.warning(f"uid={uid} not authorized")
            return

        dropmail    = await client.get_entity(DROPMAIL_USER)
        clicked_ids = get_clicked_ids(uid)
        found       = False

        async for msg in client.iter_messages(dropmail, limit=30):
            if not msg.buttons:
                continue
            if TRIGGER_TEXT not in (msg.message or "").lower():
                continue
            if msg.id in clicked_ids:
                continue

            # Found a new Restore message — click every button
            flat   = [b for row in msg.buttons for b in row]
            labels = []
            for i, btn in enumerate(flat):
                try:
                    await msg.click(i)
                    labels.append(getattr(btn, "text", f"#{i+1}"))
                    await asyncio.sleep(0.5)
                except Exception as e:
                    log.warning(f"uid={uid} click {i} failed: {e}")

            save_clicked_id(uid, msg.id)
            found = True

            if labels:
                summary = "\n".join(f"  ✅ {l}" for l in labels)
                send_msg(uid, f"🤖 Auto-clicked **{len(labels)}** button(s):\n{summary}")
            log.info(f"uid={uid} clicked {len(labels)} buttons on msg {msg.id}")

        if not found:
            log.info(f"uid={uid} no new Restore messages")

    except Exception as e:
        log.warning(f"uid={uid} autoclick error: {e}")
    finally:
        await client.disconnect()


async def run_all():
    users = get_all_autoclick_users()
    log.info(f"Cron: processing {len(users)} autoclick user(s)")
    for u in users:
        await autoclick_user(u["uid"], u["session"])


# ────────────────────── Vercel handler ──────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def do_GET(self):
        # Vercel calls cron as GET with Authorization header
        auth = self.headers.get("Authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        asyncio.run(run_all())

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self):
        self.do_GET()
