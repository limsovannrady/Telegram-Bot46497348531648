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
SESSIONS_DIR   = "/tmp/user_sessions"
CONFIG_FILE    = "/tmp/user_configs.json"
LOGIN_STATE    = {}          # in-memory (per warm instance)

os.makedirs(SESSIONS_DIR, exist_ok=True)


# ───────────────────────── Bot API helpers ─────────────────────────

def send(chat_id: int, text: str, parse_mode: str = "Markdown") -> dict:
    r = req.post(f"{BOT_API}/sendMessage", json={
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
    }, timeout=10)
    return r.json()


# ───────────────────────── Config persistence ──────────────────────

def load_configs() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_configs(data: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_user_cfg(uid: int) -> dict:
    cfg = load_configs().get(str(uid), {})
    cfg.setdefault("autoclick_enabled", False)
    return cfg


def set_user_cfg(uid: int, cfg: dict):
    data = load_configs()
    data[str(uid)] = cfg
    save_configs(data)


# ───────────────────────── Session persistence ─────────────────────

def session_path(uid: int) -> str:
    return os.path.join(SESSIONS_DIR, f"{uid}.session")


def save_session(uid: int, string_session: str):
    with open(session_path(uid), "w") as f:
        f.write(string_session)


def load_session(uid: int) -> str | None:
    p = session_path(uid)
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip() or None
    return None


def delete_session(uid: int):
    p = session_path(uid)
    if os.path.exists(p):
        os.remove(p)


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


async def _do_login_phone(uid: int, phone: str) -> str:
    """Start phone login, return phone_code_hash or raise."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    sent = await client.send_code_request(phone)
    LOGIN_STATE[uid]["client"]          = client
    LOGIN_STATE[uid]["phone_code_hash"] = sent.phone_code_hash
    return sent.phone_code_hash


async def _do_login_code(uid: int, code: str):
    from telethon.errors import SessionPasswordNeededError

    client = LOGIN_STATE[uid]["client"]
    phone  = LOGIN_STATE[uid]["phone"]
    hash_  = LOGIN_STATE[uid]["phone_code_hash"]
    await client.sign_in(phone=phone, code=code, phone_code_hash=hash_)
    me = await client.get_me()
    save_session(uid, client.session.save())
    await client.disconnect()
    LOGIN_STATE.pop(uid, None)
    return me


async def _do_login_password(uid: int, password: str):
    client = LOGIN_STATE[uid]["client"]
    await client.sign_in(password=password)
    me = await client.get_me()
    save_session(uid, client.session.save())
    await client.disconnect()
    LOGIN_STATE.pop(uid, None)
    return me


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
    "/autoclickon — បើក auto-click\n"
    "/autoclickoff — បិទ auto-click\n"
    "/autoclickstatus — ស្ថានភាព\n"
    "/clicknow — ចុច Restore button ឥឡូវ"
)


def _run(coro):
    """Run async coroutine safely inside a sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


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
            LOGIN_STATE[uid] = {"step": "phone"}
            send(chat_id,
                 "👋 **សួស្តី!**\n\n"
                 "📱 សូមបញ្ចូលលេខទូរស័ព្ទជាមួយកូដប្រទេស\n"
                 "_(ឧ. `+855xxxxxxxx`)_\n\n"
                 "វាយ /cancel ដើម្បីបោះបង់")
        return

    # ── /cancel ─────────────────────────────────────────────────────
    if text == "/cancel":
        st = LOGIN_STATE.pop(uid, None)
        if st and st.get("client"):
            try:
                _run(st["client"].disconnect())
            except Exception:
                pass
        send(chat_id, "❌ បានបោះបង់។")
        return

    # ── /logout ─────────────────────────────────────────────────────
    if text == "/logout":
        LOGIN_STATE.pop(uid, None)
        delete_session(uid)
        cfgs = load_configs()
        cfgs.pop(str(uid), None)
        save_configs(cfgs)
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

    # ── /autoclickon ─────────────────────────────────────────────────
    if text == "/autoclickon":
        if not load_session(uid):
            send(chat_id, "⚠️ សូម /start មុនសិន។")
            return
        cfg = get_user_cfg(uid)
        cfg["autoclick_enabled"] = True
        set_user_cfg(uid, cfg)
        send(chat_id,
             f"🤖 Auto-click **បើក** ហើយ។\n\n"
             f"វាយ /clicknow ដើម្បីចុច Restore button ឥឡូវ\n"
             f"_(Webhook mode: auto-listen មិនដំណើរការ — ត្រូវវាយ /clicknow ដោយខ្លួនឯង)_")
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
             f"Trigger: សារមានពាក្យ `Restore`\n\n"
             f"វាយ /clicknow ដើម្បីចុច button ឥឡូវ")
        return

    # ── /clicknow ────────────────────────────────────────────────────
    if text == "/clicknow":
        if not load_session(uid):
            send(chat_id, "⚠️ សូម /start ដើម្បី login មុនសិន។")
            return
        send(chat_id, "⏳ កំពុងស្វែងរក Restore button...")
        result = _run(_do_autoclick(uid))
        send(chat_id, result)
        return

    # ── Login conversation ───────────────────────────────────────────
    st = LOGIN_STATE.get(uid)
    if not st:
        return

    if st["step"] == "phone":
        phone = text.replace(" ", "")
        if not phone.startswith("+") or not phone[1:].isdigit():
            send(chat_id, "⚠️ ទម្រង់មិនត្រឹមត្រូវ។ ឧ. `+855xxxxxxxx`")
            return
        st["phone"] = phone
        try:
            _run(_do_login_phone(uid, phone))
            st["step"] = "code"
            send(chat_id,
                 "✉️ Telegram ផ្ញើ **code** ទៅ app។\n\n"
                 "សូមបញ្ចូល code (ឧ. `1 2 3 4 5` — បំបែកតួអក្សរ)")
        except Exception as e:
            LOGIN_STATE.pop(uid, None)
            send(chat_id, f"❌ `{e}`")
        return

    if st["step"] == "code":
        from telethon.errors import SessionPasswordNeededError
        code = "".join(ch for ch in text if ch.isdigit())
        if not code:
            send(chat_id, "⚠️ សូមបញ្ចូល code ជាលេខ។")
            return
        try:
            me = _run(_do_login_code(uid, code))
            send(chat_id,
                 f"✅ **Login ជោគជ័យ!**\n\n"
                 f"👤 {me.first_name or ''} {me.last_name or ''}\n"
                 f"ID: `{me.id}`\n\n{HELP_TEXT}")
        except SessionPasswordNeededError:
            st["step"] = "password"
            send(chat_id, "🔒 Account បើក 2FA។ សូមបញ្ចូលពាក្យសម្ងាត់។")
        except Exception as e:
            LOGIN_STATE.pop(uid, None)
            send(chat_id, f"❌ `{e}`")
        return

    if st["step"] == "password":
        try:
            me = _run(_do_login_password(uid, text))
            send(chat_id,
                 f"✅ **Login ជោគជ័យ!**\n\n"
                 f"👤 {me.first_name or ''} {me.last_name or ''}\n"
                 f"ID: `{me.id}`\n\n{HELP_TEXT}")
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
