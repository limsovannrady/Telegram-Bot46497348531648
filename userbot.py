"""
Telegram Auto-Click Bot (@DropmailBot)

មុខងារ៖
- Admin login ចូល personal account តាម bot
- Auto-click inline button (ឧ. "Restore") រាល់សារពី @DropmailBot
"""
import os
import json
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.errors import (
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    PhoneNumberInvalidError,
    FloodWaitError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loginbot")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

SESSIONS_DIR = "user_sessions"
CONFIG_FILE = "user_configs.json"
os.makedirs(SESSIONS_DIR, exist_ok=True)

DROPMAIL_USERNAME = "DropmailBot"

bot = TelegramClient("bot", API_ID, API_HASH)

ADMIN_IDS = {int(x) for x in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}


@bot.on(events.NewMessage())
async def _admin_gate(event):
    if not event.is_private:
        raise events.StopPropagation
    if ADMIN_IDS and event.sender_id not in ADMIN_IDS:
        try:
            await event.reply("⛔ អ្នកមិនមានសិទ្ធិប្រើ bot នេះទេ។")
        except Exception:
            pass
        log.info(f"Blocked non-admin uid={event.sender_id}")
        raise events.StopPropagation


LOGIN_STATE: dict[int, dict] = {}
USER_CLIENTS: dict[int, TelegramClient] = {}
AUTOCLICK_HANDLERS: dict[int, object] = {}


# -------------------- Config persistence --------------------
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
    data = load_configs()
    cfg = data.get(str(uid), {})
    cfg.setdefault("autoclick_enabled", False)
    cfg.setdefault("autoclick_match", "")
    return cfg


def set_user_cfg(uid: int, cfg: dict):
    data = load_configs()
    data[str(uid)] = cfg
    save_configs(data)


# -------------------- Session persistence --------------------
def session_path(uid: int) -> str:
    return os.path.join(SESSIONS_DIR, f"{uid}.session")


def save_session(uid: int, string_session: str):
    with open(session_path(uid), "w") as f:
        f.write(string_session)


def load_session(uid: int) -> str | None:
    p = session_path(uid)
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return None


# -------------------- User client management --------------------
async def start_user_client(uid: int) -> TelegramClient | None:
    if uid in USER_CLIENTS:
        return USER_CLIENTS[uid]
    s = load_session(uid)
    if not s:
        return None
    client = TelegramClient(StringSession(s), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return None
    USER_CLIENTS[uid] = client
    await install_autoclick_handler(uid)
    return client


async def stop_user_client(uid: int):
    client = USER_CLIENTS.pop(uid, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    AUTOCLICK_HANDLERS.pop(uid, None)


async def install_autoclick_handler(uid: int):
    """Auto-click buttons on new/edited messages from @DropmailBot."""
    client = USER_CLIENTS.get(uid)
    if not client:
        return

    old = AUTOCLICK_HANDLERS.pop(uid, None)
    if old is not None:
        try:
            client.remove_event_handler(old)
        except Exception:
            pass

    cfg = get_user_cfg(uid)
    if not cfg.get("autoclick_enabled"):
        return

    try:
        dropmail = await client.get_entity(DROPMAIL_USERNAME)
    except Exception as e:
        log.warning(f"uid={uid} cannot resolve @{DROPMAIL_USERNAME}: {e}")
        return

    match = (cfg.get("autoclick_match") or "").strip().lower()

    async def handler(event):
        if event.chat_id != dropmail.id:
            return
        msg = event.message
        if not msg.buttons:
            return
        flat = [b for row in msg.buttons for b in row]
        if not flat:
            return

        target_idx = None
        if match:
            if match.isdigit():
                n = int(match) - 1
                if 0 <= n < len(flat):
                    target_idx = n
            else:
                for i, b in enumerate(flat):
                    if match in (getattr(b, "text", "") or "").lower():
                        target_idx = i
                        break
        else:
            target_idx = 0

        if target_idx is None:
            labels = [getattr(b, "text", "") or repr(b) for b in flat]
            log.info(f"uid={uid} autoclick: no matching button for '{match}'. Buttons: {labels}")
            return

        try:
            await msg.click(target_idx)
            label = getattr(flat[target_idx], "text", "?")
            log.info(f"uid={uid} autoclicked button[{target_idx}]: {label}")
            try:
                await bot.send_message(uid, f"🤖 Auto-clicked: **{label}**", parse_mode="md")
            except Exception:
                pass
        except Exception as e:
            log.warning(f"uid={uid} autoclick failed: {e}")

    client.add_event_handler(handler, events.NewMessage(chats=dropmail))
    client.add_event_handler(handler, events.MessageEdited(chats=dropmail))
    AUTOCLICK_HANDLERS[uid] = handler
    log.info(f"uid={uid} autoclick handler installed (match='{match}', new+edited)")


# -------------------- Login flow --------------------
async def cleanup_login(uid: int):
    st = LOGIN_STATE.pop(uid, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass


HELP_TEXT = (
    "📖 **Commands**\n\n"
    "/start — ចាប់ផ្ដើម / បង្ហាញ menu\n"
    "/me — ព័ត៌មាន account\n"
    "/logout — លុប session\n"
    "/cancel — បោះបង់ការ login\n\n"
    "**🤖 Auto-Click @DropmailBot**\n"
    "/autoclickon `[keyword]` — បើក (keyword ជម្រើស)\n"
    "/autoclickoff — បិទ\n"
    "/autoclickstatus — បង្ហាញស្ថានភាព"
)


@bot.on(events.NewMessage(pattern=r"^/(start|help|menu)$"))
async def cmd_start(event):
    uid = event.sender_id
    if load_session(uid):
        await event.reply(f"✅ អ្នកបាន login រួចហើយ។\n\n{HELP_TEXT}", parse_mode="md")
        await start_user_client(uid)
        return
    await cleanup_login(uid)
    await event.reply(
        "👋 **សួស្តី!**\n\n"
        "📱 សូមបញ្ចូលលេខទូរស័ព្ទរបស់អ្នកជាមួយកូដប្រទេស\n"
        "_(ឧ. `+855xxxxxxxx`)_\n\n"
        "វាយ /cancel ដើម្បីបោះបង់",
        parse_mode="md",
    )
    LOGIN_STATE[uid] = {"step": "phone"}


@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cmd_cancel(event):
    await cleanup_login(event.sender_id)
    await event.reply("❌ បានបោះបង់។")


@bot.on(events.NewMessage(pattern=r"^/logout$"))
async def cmd_logout(event):
    uid = event.sender_id
    await stop_user_client(uid)
    p = session_path(uid)
    if os.path.exists(p):
        os.remove(p)
    cfgs = load_configs()
    cfgs.pop(str(uid), None)
    save_configs(cfgs)
    await event.reply("🗑️ Session ត្រូវបានលុប។ វាយ /start ដើម្បី login ម្ដងទៀត។")


@bot.on(events.NewMessage(pattern=r"^/me$"))
async def cmd_me(event):
    uid = event.sender_id
    client = await start_user_client(uid)
    if not client:
        await event.reply("⚠️ សូម /start ដើម្បី login មុនសិន។")
        return
    me = await client.get_me()
    await event.reply(
        f"👤 **{me.first_name or ''} {me.last_name or ''}**\n"
        f"Username: @{me.username or '—'}\n"
        f"ID: `{me.id}`",
        parse_mode="md",
    )


# -------------------- Auto-click commands --------------------
@bot.on(events.NewMessage(pattern=r"^/autoclickon(?:\s+(.+))?$"))
async def cmd_autoclickon(event):
    uid = event.sender_id
    if not load_session(uid):
        await event.reply("⚠️ សូម /start មុនសិន។")
        return
    arg = event.pattern_match.group(1)
    cfg = get_user_cfg(uid)
    cfg["autoclick_enabled"] = True
    cfg["autoclick_match"] = (arg or "").strip()
    set_user_cfg(uid, cfg)
    await start_user_client(uid)
    await install_autoclick_handler(uid)
    match_desc = f"button ដែលផ្គូផ្គង `{cfg['autoclick_match']}`" if cfg["autoclick_match"] else "button **ទី 1**"
    await event.reply(
        f"🤖 Auto-click **បើក** សម្រាប់ @{DROPMAIL_USERNAME}\n"
        f"នឹងចុច {match_desc} ដោយស្វ័យប្រវត្តិ។",
        parse_mode="md",
    )


@bot.on(events.NewMessage(pattern=r"^/autoclickoff$"))
async def cmd_autoclickoff(event):
    uid = event.sender_id
    cfg = get_user_cfg(uid)
    cfg["autoclick_enabled"] = False
    set_user_cfg(uid, cfg)
    await install_autoclick_handler(uid)
    await event.reply("⏸️ Auto-click **បិទ** ហើយ។", parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/autoclickstatus$"))
async def cmd_autoclickstatus(event):
    uid = event.sender_id
    cfg = get_user_cfg(uid)
    state = "🟢 បើក" if cfg.get("autoclick_enabled") else "🔴 បិទ"
    match = cfg.get("autoclick_match") or "(button ទី 1)"
    await event.reply(
        f"**🤖 Auto-click @{DROPMAIL_USERNAME}**\n"
        f"Status: {state}\n"
        f"Match: `{match}`",
        parse_mode="md",
    )


# -------------------- Login conversation --------------------
@bot.on(events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
async def login_flow(event):
    uid = event.sender_id
    st = LOGIN_STATE.get(uid)
    if not st:
        return
    text = (event.raw_text or "").strip()

    if st["step"] == "phone":
        phone = text.replace(" ", "")
        if not phone.startswith("+") or not phone[1:].isdigit():
            await event.reply("⚠️ ទម្រង់មិនត្រឹមត្រូវ។ ឧ. `+855xxxxxxxx`", parse_mode="md")
            return
        user_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await user_client.connect()
        try:
            sent = await user_client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await event.reply("⚠️ លេខមិនត្រឹមត្រូវ។")
            await user_client.disconnect()
            return
        except FloodWaitError as e:
            await event.reply(f"⏳ ត្រូវរង់ចាំ {e.seconds} វិនាទី។")
            await user_client.disconnect()
            await cleanup_login(uid)
            return
        except Exception as e:
            log.exception("send_code_request failed")
            await event.reply(f"❌ `{e}`", parse_mode="md")
            await user_client.disconnect()
            await cleanup_login(uid)
            return
        st.update(step="code", client=user_client, phone=phone, phone_code_hash=sent.phone_code_hash)
        await event.reply(
            "✉️ Telegram ផ្ញើ **code** ទៅ app។\n\n"
            "សូមបញ្ចូល code (ឧ. `1 2 3 4 5` — បំបែកតួអក្សរ)",
            parse_mode="md",
        )
        return

    if st["step"] == "code":
        code = "".join(ch for ch in text if ch.isdigit())
        if not code:
            await event.reply("⚠️ សូមបញ្ចូល code ជាលេខ។")
            return
        client: TelegramClient = st["client"]
        try:
            await client.sign_in(phone=st["phone"], code=code, phone_code_hash=st["phone_code_hash"])
        except SessionPasswordNeededError:
            st["step"] = "password"
            await event.reply("🔒 Account បើក 2FA។ សូមបញ្ចូលពាក្យសម្ងាត់។")
            return
        except PhoneCodeInvalidError:
            await event.reply("⚠️ Code ខុស។ សាកម្តងទៀត ឬ /cancel")
            return
        except PhoneCodeExpiredError:
            await event.reply("⚠️ Code ផុតកំណត់។ សូម /start ម្ដងទៀត។")
            await cleanup_login(uid)
            return
        except Exception as e:
            log.exception("sign_in failed")
            await event.reply(f"❌ `{e}`", parse_mode="md")
            await cleanup_login(uid)
            return
        await finish_login(event, uid)
        return

    if st["step"] == "password":
        client: TelegramClient = st["client"]
        try:
            await client.sign_in(password=text)
        except Exception as e:
            await event.reply(f"⚠️ ពាក្យសម្ងាត់ខុស ឬកំហុស៖ `{e}`", parse_mode="md")
            return
        await finish_login(event, uid)
        return


async def finish_login(event, uid: int):
    st = LOGIN_STATE.get(uid)
    if not st:
        return
    client: TelegramClient = st["client"]
    me = await client.get_me()
    save_session(uid, client.session.save())
    await client.disconnect()
    LOGIN_STATE.pop(uid, None)
    await event.reply(
        f"✅ **Login ជោគជ័យ!**\n\n"
        f"👤 {me.first_name or ''} {me.last_name or ''}\n"
        f"ID: `{me.id}`\n\n"
        f"{HELP_TEXT}",
        parse_mode="md",
    )
    await start_user_client(uid)


# -------------------- Startup --------------------
async def restore_all_user_clients():
    for fname in os.listdir(SESSIONS_DIR):
        if fname.endswith(".session"):
            try:
                uid = int(fname.split(".")[0])
                if await start_user_client(uid):
                    log.info(f"Restored user client uid={uid}")
            except Exception as e:
                log.warning(f"Failed to restore {fname}: {e}")


BOT_COMMANDS = [
    ("start", "ចាប់ផ្ដើម / បង្ហាញ menu"),
    ("help", "បង្ហាញ commands"),
    ("me", "ព័ត៌មាន account"),
    ("logout", "លុប session"),
    ("cancel", "បោះបង់ការ login"),
    ("autoclickon", "បើក auto-click @DropmailBot"),
    ("autoclickoff", "បិទ auto-click"),
    ("autoclickstatus", "ស្ថានភាព auto-click"),
]


async def register_bot_commands():
    try:
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=[BotCommand(command=c, description=d) for c, d in BOT_COMMANDS],
        ))
        log.info("Bot commands registered")
    except Exception as e:
        log.warning(f"Failed to register bot commands: {e}")


async def main():
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"✅ Bot started as: @{me.username}")
    await register_bot_commands()
    await restore_all_user_clients()
    print("Ready. Open the bot and send /start")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
