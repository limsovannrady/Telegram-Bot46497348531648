"""
Telegram Bot Webhook Handler សម្រាប់ Vercel
POST /api/webhook  ← Telegram sends updates here
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import asyncio
import logging
import requests as req
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_ID     = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH   = os.environ.get("TELEGRAM_API_HASH", "")
ADMIN_IDS  = {
    int(x)
    for x in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

BOT_API        = f"https://api.telegram.org/bot{BOT_TOKEN}"
DROPMAIL_USER  = "DropmailBot"
TRIGGER_TEXT   = "restore"
NEON_DSN       = os.environ.get("NEON_DATABASE_URL", "")


# ───────────────────────── Neon DB connection ──────────────────────

def get_db():
    return psycopg2.connect(NEON_DSN)


def init_db():
    """បង្កើត tables ប្រសិន មិនទាន់មាន។"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    uid        BIGINT PRIMARY KEY,
                    session    TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_configs (
                    uid                BIGINT PRIMARY KEY,
                    autoclick_enabled  BOOLEAN DEFAULT FALSE,
                    clicked_ids        JSONB   DEFAULT '[]'::jsonb,
                    updated_at         TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                ALTER TABLE user_configs
                ADD COLUMN IF NOT EXISTS clicked_ids JSONB DEFAULT '[]'::jsonb;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS login_states (
                    uid             BIGINT PRIMARY KEY,
                    state           JSONB NOT NULL,
                    updated_at      TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        conn.commit()


try:
    init_db()
    log.info("Neon DB initialized")
except Exception as _e:
    log.warning(f"DB init skipped: {_e}")


# ───────────────────────── Login state (Neon) ──────────────────────
# Stored in DB so multi-step login survives across serverless instances.

def get_login_state(uid: int) -> dict | None:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM login_states WHERE uid = %s", (uid,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        log.warning(f"get_login_state db error: {e}")
        return None


def set_login_state(uid: int, state: dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO login_states (uid, state, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (uid) DO UPDATE
                        SET state      = EXCLUDED.state,
                            updated_at = NOW()
                """, (uid, json.dumps(state)))
            conn.commit()
    except Exception as e:
        log.warning(f"set_login_state db error: {e}")


def delete_login_state(uid: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM login_states WHERE uid = %s", (uid,))
            conn.commit()
    except Exception as e:
        log.warning(f"delete_login_state db error: {e}")


# ───────────────────────── Bot API helpers ─────────────────────────

def send(chat_id: int, text: str, parse_mode: str = "Markdown") -> dict:
    r = req.post(f"{BOT_API}/sendMessage", json={
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
    }, timeout=10)
    return r.json()


# ───────────────────────── Config persistence (Neon) ───────────────

def get_user_cfg(uid: int) -> dict:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT autoclick_enabled FROM user_configs WHERE uid = %s",
                    (uid,)
                )
                row = cur.fetchone()
                if row:
                    return {"autoclick_enabled": row[0]}
    except Exception as e:
        log.warning(f"get_user_cfg db error: {e}")
    return {"autoclick_enabled": False}


def set_user_cfg(uid: int, cfg: dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_configs (uid, autoclick_enabled, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (uid) DO UPDATE
                        SET autoclick_enabled = EXCLUDED.autoclick_enabled,
                            updated_at        = NOW()
                """, (uid, cfg.get("autoclick_enabled", False)))
            conn.commit()
    except Exception as e:
        log.warning(f"set_user_cfg db error: {e}")


# ───────────────────────── Session persistence (Neon) ──────────────

def save_session(uid: int, string_session: str):
    """រក្សាទុក session string ក្នុង Neon database — survive cold start។"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_sessions (uid, session, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (uid) DO UPDATE
                        SET session    = EXCLUDED.session,
                            updated_at = NOW()
                """, (uid, string_session))
            conn.commit()
    except Exception as e:
        log.warning(f"save_session db error: {e}")


def load_session(uid: int) -> str | None:
    """ទាញ session string ពី Neon database។"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT session FROM user_sessions WHERE uid = %s",
                    (uid,)
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        log.warning(f"load_session db error: {e}")
        return None


def get_session_string(uid: int) -> str | None:
    return load_session(uid)


def delete_session(uid: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_sessions WHERE uid = %s", (uid,))
                cur.execute("DELETE FROM user_configs WHERE uid = %s", (uid,))
            conn.commit()
    except Exception as e:
        log.warning(f"delete_session db error: {e}")


# ───────────────────────── Telethon helpers ────────────────────────

async def _get_me(uid: int) -> dict | None:
    """Connect user client, fetch 'me', disconnect."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    s = load_session(uid)
    if not s:
        return None
    client = TelegramClient(StringSession(s), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        return {"id": me.id, "first": me.first_name or "", "last": me.last_name or "", "username": me.username or ""}
    finally:
        await client.disconnect()


async def _do_login_phone(uid: int, phone: str, st: dict) -> dict:
    """Request OTP — connect, send_code, DISCONNECT. Returns updated state."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        st["phone_code_hash"] = sent.phone_code_hash
        st["partial_session"] = client.session.save()
        return st
    finally:
        await client.disconnect()


async def _do_login_code(uid: int, code: str, st: dict):
    """Reconnect with partial session, sign in with code. Returns (me, updated_st)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError

    client = TelegramClient(StringSession(st.get("partial_session", "")), API_ID, API_HASH)
    try:
        await client.connect()
        try:
            await client.sign_in(
                phone=st["phone"],
                code=code,
                phone_code_hash=st["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            # Update partial session after code is accepted, needed for 2FA
            st["partial_session"] = client.session.save()
            raise
        me = await client.get_me()
        save_session(uid, client.session.save())
        return me
    finally:
        await client.disconnect()


async def _do_login_password(uid: int, password: str, st: dict):
    """Reconnect with post-code partial session, sign in with 2FA password."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(st.get("partial_session", "")), API_ID, API_HASH)
    try:
        await client.connect()
        await client.sign_in(password=password)
        me = await client.get_me()
        save_session(uid, client.session.save())
        return me
    finally:
        await client.disconnect()


async def _do_autoclick(uid: int):
    """Connect user client, find latest Restore message, click all buttons."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    s = load_session(uid)
    if not s:
        return "⚠️ មិនមាន session។ សូម /start ម្ដងទៀត។"

    client = TelegramClient(StringSession(s), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "⚠️ Session ផុតកំណត់។ សូម /logout ហើយ /start ម្ដងទៀត។"

        dropmail = await client.get_entity(DROPMAIL_USER)
        msg = None
        async for m in client.iter_messages(dropmail, limit=20):
            if m.buttons and TRIGGER_TEXT in (m.message or "").lower():
                msg = m
                break

        if not msg:
            return "ℹ️ រកមិនឃើញសារ Restore ថ្មីៗ។"

        flat   = [b for row in msg.buttons for b in row]
        labels = []
        for i, btn in enumerate(flat):
            try:
                await msg.click(i)
                labels.append(getattr(btn, "text", f"#{i+1}"))
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning(f"click {i} failed: {e}")

        if not labels:
            return "⚠️ ចុច button បានខុស ឬ button ត្រូវបាន expire។"

        summary = "\n".join(f"  ✅ {l}" for l in labels)
        return f"🤖 ចុចបាន **{len(labels)}** button:\n{summary}"
    finally:
        await client.disconnect()


# ───────────────────────── Command helpers ─────────────────────────

HELP_TEXT = (
    "📖 **Commands**\n\n"
    "/start — ចាប់ផ្ដើម / menu\n"
    "/me — ព័ត៌មាន account\n"
    "/logout — លុប session\n"
    "/cancel — បោះបង់ login\n"
    "/autoclickon — បើក auto-click (ស្វ័យប្រវត្តិ រៀងរាល់នាទី)\n"
    "/autoclickoff — បិទ auto-click\n"
    "/autoclickstatus — ស្ថានភាព\n"
    "/mysession — ស្ថានភាព session"
)


def _run(coro):
    """Run async coroutine in a fresh event loop (serverless-safe)."""
    return asyncio.run(coro)


def _send_session_hint(chat_id: int, uid: int):
    """ក្រោយ login — ជូនដំណឹងថា session ត្រូវបានរក្សាទុក Neon DB ហើយ។"""
    send(
        chat_id,
        "🗄️ **Session ត្រូវបានរក្សាទុក Neon Database ហើយ!**\n\n"
        "✅ Cold start ក៏**មិន** logout អ្នកទេ\n"
        "✅ Vercel restart ក៏**នៅ** login ដដែល\n\n"
        "_Session ត្រូវបាន encrypt ក្នុង Neon PostgreSQL_",
    )


def handle_message(message: dict):
    uid      = message["from"]["id"]
    text     = (message.get("text") or "").strip()
    chat_id  = message["chat"]["id"]

    # Admin gate
    if ADMIN_IDS and uid not in ADMIN_IDS:
        send(chat_id, "⛔ អ្នកមិនមានសិទ្ធិប្រើ bot នេះទេ។")
        return

    # ── /start /help /menu ──────────────────────────────────────────
    if text in ("/start", "/help", "/menu"):
        if load_session(uid):
            send(chat_id, f"✅ អ្នកបាន login រួចហើយ។\n\n{HELP_TEXT}")
        else:
            set_login_state(uid, {"step": "phone"})
            send(chat_id,
                 "👋 **សួស្តី!**\n\n"
                 "📱 សូមបញ្ចូលលេខទូរស័ព្ទជាមួយកូដប្រទេស\n"
                 "_(ឧ. `+855xxxxxxxx`)_\n\n"
                 "វាយ /cancel ដើម្បីបោះបង់")
        return

    # ── /cancel ─────────────────────────────────────────────────────
    if text == "/cancel":
        delete_login_state(uid)
        send(chat_id, "❌ បានបោះបង់។")
        return

    # ── /logout ─────────────────────────────────────────────────────
    if text == "/logout":
        delete_login_state(uid)
        delete_session(uid)
        send(chat_id, "🗑️ Session ត្រូវបានលុប។ វាយ /start ដើម្បី login ម្ដងទៀត។")
        return

    # ── /me ─────────────────────────────────────────────────────────
    if text == "/me":
        me = _run(_get_me(uid))
        if not me:
            send(chat_id, "⚠️ សូម /start ដើម្បី login មុនសិន។")
        else:
            send(chat_id,
                 f"👤 **{me['first']} {me['last']}**\n"
                 f"Username: @{me['username'] or '—'}\n"
                 f"ID: `{me['id']}`")
        return

    # ── /mysession ───────────────────────────────────────────────────
    if text == "/mysession":
        s = get_session_string(uid)
        if not s:
            send(chat_id, "⚠️ មិនទាន់ login ទេ។ សូម /start មុនសិន។")
        else:
            send(chat_id,
                 "🗄️ **Session Status**\n\n"
                 "✅ Session កំពុងរក្សាទុកក្នុង **Neon Database**\n"
                 "✅ មិន logout ពេល cold start / restart\n\n"
                 "_វាយ /logout ដើម្បីលុប session_")
        return

    # ── /autoclickon ─────────────────────────────────────────────────
    if text == "/autoclickon":
        if not load_session(uid):
            send(chat_id, "⚠️ សូម /start មុនសិន។")
            return
        cfg = get_user_cfg(uid)
        cfg["autoclick_enabled"] = True
        set_user_cfg(uid, cfg)
        send(chat_id,
             f"🤖 Auto-click **បើក** ហើយ!\n\n"
             f"✅ Bot នឹងចុច Restore button **ដោយស្វ័យប្រវត្តិ** រៀងរាល់ **1 នាទី**\n"
             f"✅ ចុចទាំងអស់ button ក្នុងសារ Restore\n\n"
             f"_វាយ /autoclickoff ដើម្បីបិទ_")
        return

    # ── /autoclickoff ────────────────────────────────────────────────
    if text == "/autoclickoff":
        cfg = get_user_cfg(uid)
        cfg["autoclick_enabled"] = False
        set_user_cfg(uid, cfg)
        send(chat_id, "⏸️ Auto-click **បិទ** ហើយ។")
        return

    # ── /autoclickstatus ─────────────────────────────────────────────
    if text == "/autoclickstatus":
        cfg   = get_user_cfg(uid)
        state = "🟢 បើក" if cfg.get("autoclick_enabled") else "🔴 បិទ"
        send(chat_id,
             f"**🤖 Auto-click @{DROPMAIL_USER}**\n"
             f"Status: {state}\n"
             f"Trigger: សារមានពាក្យ `Restore`\n"
             f"⏱️ រៀបចំជា cron job រៀងរាល់ **1 នាទី**")
        return

    # ── Login conversation ───────────────────────────────────────────
    st = get_login_state(uid)
    if not st:
        return

    if st["step"] == "phone":
        phone = text.replace(" ", "")
        if not phone.startswith("+") or not phone[1:].isdigit():
            send(chat_id, "⚠️ ទម្រង់មិនត្រឹមត្រូវ។ ឧ. `+855xxxxxxxx`")
            return
        st["phone"] = phone
        try:
            updated_st = _run(_do_login_phone(uid, phone, st))
            updated_st["step"] = "code"
            set_login_state(uid, updated_st)
            send(chat_id,
                 "✉️ Telegram ផ្ញើ **code** ទៅ app។\n\n"
                 "សូមបញ្ចូល code (ឧ. `1 2 3 4 5` — បំបែកតួអក្សរ)")
        except Exception as e:
            delete_login_state(uid)
            send(chat_id, f"❌ `{e}`")
        return

    if st["step"] == "code":
        from telethon.errors import SessionPasswordNeededError
        code = "".join(ch for ch in text if ch.isdigit())
        if not code:
            send(chat_id, "⚠️ សូមបញ្ចូល code ជាលេខ។")
            return
        try:
            me = _run(_do_login_code(uid, code, st))
            delete_login_state(uid)
            send(chat_id,
                 f"✅ **Login ជោគជ័យ!**\n\n"
                 f"👤 {me.first_name or ''} {me.last_name or ''}\n"
                 f"ID: `{me.id}`\n\n{HELP_TEXT}")
            _send_session_hint(chat_id, uid)
        except SessionPasswordNeededError:
            # st["partial_session"] already updated inside _do_login_code
            st["step"] = "password"
            set_login_state(uid, st)
            send(chat_id, "🔒 Account បើក 2FA។ សូមបញ្ចូលពាក្យសម្ងាត់។")
        except Exception as e:
            delete_login_state(uid)
            send(chat_id, f"❌ `{e}`")
        return

    if st["step"] == "password":
        try:
            me = _run(_do_login_password(uid, text, st))
            delete_login_state(uid)
            send(chat_id,
                 f"✅ **Login ជោគជ័យ!**\n\n"
                 f"👤 {me.first_name or ''} {me.last_name or ''}\n"
                 f"ID: `{me.id}`\n\n{HELP_TEXT}")
            _send_session_hint(chat_id, uid)
        except Exception as e:
            send(chat_id, f"⚠️ ពាក្យសម្ងាត់ខុស ឬកំហុស៖ `{e}`")
        return


# ───────────────────────── Vercel Handler ─────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass  # suppress default access log

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot webhook is active.")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        try:
            update  = json.loads(body)
            message = update.get("message") or update.get("edited_message")
            if message:
                handle_message(message)
        except Exception as e:
            log.exception(f"webhook error: {e}")
