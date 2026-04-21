"""
Telegram Login Bot + Auto-Forward

អ្នកប្រើ login តាម bot រួច bot នឹងរក្សា session, បង្ហាញ groups,
និងអនុវត្ត auto-forward សារពីប្រភពទៅគោលដៅ។
"""
import os
import json
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, BotCommand, BotCommandScopeDefault
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

bot = TelegramClient("bot", API_ID, API_HASH)

# Admin gate — មានតែ user ID ទាំងនេះទេដែលអាចប្រើ bot បាន
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

# In-memory state
LOGIN_STATE: dict[int, dict] = {}       # uid -> login flow state
USER_CLIENTS: dict[int, TelegramClient] = {}  # uid -> running user TelegramClient
USER_HANDLERS: dict[int, object] = {}   # uid -> forward handler reference
AUTOCLICK_HANDLERS: dict[int, object] = {}  # uid -> autoclick handler reference

DROPMAIL_USERNAME = "DropmailBot"

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
    cfg.setdefault("from", [])
    cfg.setdefault("to", "me")
    cfg.setdefault("enabled", False)
    cfg.setdefault("autoclick_enabled", False)
    cfg.setdefault("autoclick_match", "")  # empty = click first button
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
    """ចាប់ផ្ដើម client សម្រាប់ user ហើយតម្លើង handler forward។"""
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
    await install_forward_handler(uid)
    await install_autoclick_handler(uid)
    return client


async def stop_user_client(uid: int):
    client = USER_CLIENTS.pop(uid, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    USER_HANDLERS.pop(uid, None)
    AUTOCLICK_HANDLERS.pop(uid, None)


async def install_autoclick_handler(uid: int):
    """Auto-click buttons on new messages from @DropmailBot."""
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
        flat = []
        for row in msg.buttons:
            for b in row:
                flat.append(b)
        if not flat:
            return

        target_idx = None
        if match:
            # ផ្គូផ្គងតាម label (substring ឬលេខ)
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
            target_idx = 0  # ចុចជាលើកទី 1

        if target_idx is None:
            log.info(f"uid={uid} autoclick: no matching button for '{match}'")
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


async def install_forward_handler(uid: int):
    """ដំឡើង event handler forward សម្រាប់ user ។"""
    client = USER_CLIENTS.get(uid)
    if not client:
        return

    # លុប handler ចាស់ បើមាន
    old = USER_HANDLERS.pop(uid, None)
    if old is not None:
        try:
            client.remove_event_handler(old)
        except Exception:
            pass

    cfg = get_user_cfg(uid)
    if not cfg.get("enabled") or not cfg.get("from"):
        return

    # Resolve source IDs ជាមុន
    resolved = set()
    for src in cfg["from"]:
        try:
            ent = await client.get_entity(int(src) if str(src).lstrip("-").isdigit() else src)
            resolved.add(ent.id)
        except Exception as e:
            log.warning(f"uid={uid} cannot resolve source {src}: {e}")

    dest = cfg["to"]
    try:
        dest_entity = await client.get_entity("me" if dest == "me" else (int(dest) if str(dest).lstrip("-").isdigit() else dest))
    except Exception as e:
        log.warning(f"uid={uid} cannot resolve dest {dest}: {e}")
        return

    async def handler(event):
        chat_id = event.chat_id
        # Telethon ច្រើនប្រើ peer id ជា −100... សម្រាប់ channel
        if chat_id in resolved or abs(chat_id) in resolved or (event.chat and event.chat.id in resolved):
            try:
                await client.forward_messages(dest_entity, event.message)
            except Exception as e:
                log.warning(f"uid={uid} forward failed: {e}")

    client.add_event_handler(handler, events.NewMessage())
    USER_HANDLERS[uid] = handler
    log.info(f"uid={uid} forward handler installed: from={cfg['from']} to={dest}")


# -------------------- Login flow --------------------
async def cleanup_login(uid: int):
    st = LOGIN_STATE.pop(uid, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass


HELP_TEXT = (
    "📖 **Commands ទាំងអស់**\n\n"
    "**👤 Account**\n"
    "/start — ចាប់ផ្ដើម / បង្ហាញ menu\n"
    "/me — ព័ត៌មាន account\n"
    "/logout — លុប session\n"
    "/cancel — បោះបង់ការ login\n\n"
    "**💬 Chats**\n"
    "/groups — បង្ហាញបញ្ជី groups និង channels\n\n"
    "**🔁 Auto-Forward**\n"
    "/setfrom `<ids>` — កំណត់ chat ប្រភព (បំបែកដោយ comma)\n"
    "/setto `<id|me>` — កំណត់ chat គោលដៅ\n"
    "/fwdon — បើក\n"
    "/fwdoff — បិទ\n"
    "/fwdstatus — បង្ហាញស្ថានភាព\n\n"
    "**🤖 Auto-Click @DropmailBot**\n"
    "/autoclickon `[keyword]` — បើក (keyword សម្រាប់ផ្គូផ្គង label)\n"
    "/autoclickoff — បិទ\n"
    "/autoclickstatus — បង្ហាញស្ថានភាព\n\n"
    "**🔘 ចុច Inline Buttons (manual)**\n"
    "/open `<@bot>` — បើក chat\n"
    "/btn `<លេខ>` — ចុច button\n"
    "/send `<text>` — ផ្ញើសារ\n"
    "/refresh — refresh សារចុងក្រោយ"
)


@bot.on(events.NewMessage(pattern=r"^/(start|help|menu)$"))
async def cmd_start(event):
    uid = event.sender_id
    if load_session(uid):
        await event.reply(
            f"✅ អ្នកបាន login រួចហើយ។\n\n{HELP_TEXT}",
            parse_mode="md",
        )
        await start_user_client(uid)
        return

    await cleanup_login(uid)
    await event.reply(
        "👋 **សួស្តី!**\n\n"
        "📱 សូមបញ្ចូលលេខទូរស័ព្ទរបស់អ្នកជាមួយកូដប្រទេស\n"
        "_(ឧ. `+855xxxxxxxx`)_\n\n"
        "វាយ /cancel ដើម្បីបោះបង់\n\n"
        f"{HELP_TEXT}",
        parse_mode="md",
    )
    LOGIN_STATE[uid] = {"step": "phone"}


@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cmd_cancel(event):
    await cleanup_login(event.sender_id)
    await event.reply("❌ បានបោះបង់។ វាយ /start ដើម្បីចាប់ផ្ដើមម្ដងទៀត។")


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
    await event.reply("🗑️ Session និងការកំណត់ត្រូវបានលុប។ វាយ /start ដើម្បី login ម្ដងទៀត។")


# -------------------- Info commands --------------------
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
        f"ID: `{me.id}`\n"
        f"Phone: `{me.phone or '—'}`",
        parse_mode="md",
    )


@bot.on(events.NewMessage(pattern=r"^/groups$"))
async def cmd_groups(event):
    uid = event.sender_id
    client = await start_user_client(uid)
    if not client:
        await event.reply("⚠️ សូម /start ដើម្បី login មុនសិន។")
        return

    await event.reply("⏳ កំពុងទាញយកបញ្ជី groups...")
    groups, channels = [], []
    async for d in client.iter_dialogs():
        ent = d.entity
        if isinstance(ent, Chat):
            groups.append((d.id, d.name, getattr(ent, "participants_count", "?")))
        elif isinstance(ent, Channel):
            if ent.megagroup:
                groups.append((d.id, d.name, getattr(ent, "participants_count", "?")))
            else:
                channels.append((d.id, d.name, getattr(ent, "participants_count", "?")))

    def fmt(items, title, emoji):
        if not items:
            return f"**{emoji} {title}:** (គ្មាន)"
        lines = [f"**{emoji} {title} ({len(items)}):**"]
        for cid, name, count in items:
            lines.append(f"`{cid}` — {name} _({count} members)_")
        return "\n".join(lines)

    # ចែកជា chunks បើវែងពេក
    full = fmt(groups, "Groups", "👥") + "\n\n" + fmt(channels, "Channels", "📢")
    # Telegram message limit ~4096 chars
    if len(full) <= 4000:
        await event.reply(full, parse_mode="md")
    else:
        # បំបែកចេញ
        await event.reply(fmt(groups, "Groups", "👥")[:4000], parse_mode="md")
        await event.reply(fmt(channels, "Channels", "📢")[:4000], parse_mode="md")


# -------------------- Forward config commands --------------------
@bot.on(events.NewMessage(pattern=r"^/setfrom(?:\s+(.+))?$"))
async def cmd_setfrom(event):
    uid = event.sender_id
    if not load_session(uid):
        await event.reply("⚠️ សូម /start មុនសិន។")
        return
    arg = event.pattern_match.group(1)
    if not arg:
        await event.reply(
            "ប្រើបែបនេះ៖ `/setfrom -1001234567890,@somechannel`\n"
            "អាចដាក់ច្រើន chat បំបែកដោយ comma",
            parse_mode="md",
        )
        return
    items = [s.strip() for s in arg.split(",") if s.strip()]
    cfg = get_user_cfg(uid)
    cfg["from"] = items
    set_user_cfg(uid, cfg)
    await event.reply(f"✅ ប្រភពត្រូវបានកំណត់៖ `{', '.join(items)}`", parse_mode="md")
    await install_forward_handler(uid)


@bot.on(events.NewMessage(pattern=r"^/setto(?:\s+(.+))?$"))
async def cmd_setto(event):
    uid = event.sender_id
    if not load_session(uid):
        await event.reply("⚠️ សូម /start មុនសិន។")
        return
    arg = event.pattern_match.group(1)
    if not arg:
        await event.reply("ប្រើបែបនេះ៖ `/setto me` ឬ `/setto -1001234567890` ឬ `/setto @channel`", parse_mode="md")
        return
    cfg = get_user_cfg(uid)
    cfg["to"] = arg.strip()
    set_user_cfg(uid, cfg)
    await event.reply(f"✅ គោលដៅត្រូវបានកំណត់៖ `{cfg['to']}`", parse_mode="md")
    await install_forward_handler(uid)


@bot.on(events.NewMessage(pattern=r"^/fwdon$"))
async def cmd_fwdon(event):
    uid = event.sender_id
    if not load_session(uid):
        await event.reply("⚠️ សូម /start មុនសិន។")
        return
    cfg = get_user_cfg(uid)
    if not cfg.get("from"):
        await event.reply("⚠️ សូមកំណត់ប្រភពតាម /setfrom មុន។")
        return
    cfg["enabled"] = True
    set_user_cfg(uid, cfg)
    await start_user_client(uid)
    await install_forward_handler(uid)
    await event.reply("🔁 Auto-forward **បើក** ហើយ។", parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/fwdoff$"))
async def cmd_fwdoff(event):
    uid = event.sender_id
    cfg = get_user_cfg(uid)
    cfg["enabled"] = False
    set_user_cfg(uid, cfg)
    await install_forward_handler(uid)
    await event.reply("⏸️ Auto-forward **បិទ** ហើយ។", parse_mode="md")


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
        f"នឹងចុច {match_desc} ដោយស្វ័យប្រវត្តិនៅពេលមានសារថ្មី។",
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


@bot.on(events.NewMessage(pattern=r"^/fwdstatus$"))
async def cmd_fwdstatus(event):
    uid = event.sender_id
    cfg = get_user_cfg(uid)
    state = "🟢 បើក" if cfg.get("enabled") else "🔴 បិទ"
    frm = ", ".join(cfg.get("from", [])) or "(មិនបានកំណត់)"
    to = cfg.get("to", "me")
    await event.reply(
        f"**🔁 Auto-forward**\n"
        f"Status: {state}\n"
        f"From: `{frm}`\n"
        f"To: `{to}`",
        parse_mode="md",
    )


# -------------------- Inline button interaction --------------------
# OPEN_CHAT[uid] = {"peer": <entity>, "msg_id": <int>}
OPEN_CHAT: dict[int, dict] = {}


def _format_buttons(message) -> str:
    """បំលែង buttons ទៅជាអត្ថបទមានលេខ"""
    if not message.buttons:
        return "_(សារនេះគ្មាន button)_"
    lines = []
    n = 1
    for row in message.buttons:
        for btn in row:
            label = getattr(btn, "text", str(btn))
            lines.append(f"`{n}`. {label}")
            n += 1
    return "\n".join(lines)


def _flat_buttons(message):
    flat = []
    if not message.buttons:
        return flat
    for row in message.buttons:
        for btn in row:
            flat.append(btn)
    return flat


@bot.on(events.NewMessage(pattern=r"^/open(?:\s+(.+))?$"))
async def cmd_open(event):
    uid = event.sender_id
    client = await start_user_client(uid)
    if not client:
        await event.reply("⚠️ សូម /start ដើម្បី login មុនសិន។")
        return
    arg = event.pattern_match.group(1)
    if not arg:
        await event.reply("ប្រើបែបនេះ៖ `/open @DropmailBot` ឬ `/open -100123456789`", parse_mode="md")
        return
    target = arg.strip()
    try:
        peer = await client.get_entity(int(target) if target.lstrip("-").isdigit() else target)
    except Exception as e:
        await event.reply(f"❌ រក chat មិនឃើញ៖ `{e}`", parse_mode="md")
        return

    # ទាញសារចុងក្រោយពីchat នោះ
    msgs = await client.get_messages(peer, limit=1)
    if not msgs:
        # បើមិនទាន់ធ្លាប់ chat សូមផ្ញើ /start ទៅ bot នោះ
        await client.send_message(peer, "/start")
        await asyncio.sleep(2)
        msgs = await client.get_messages(peer, limit=1)

    if not msgs:
        await event.reply("⚠️ មិនមានសារ។")
        return

    m = msgs[0]
    OPEN_CHAT[uid] = {"peer": peer, "msg_id": m.id}

    text = m.message or "_(គ្មានអត្ថបទ)_"
    buttons_text = _format_buttons(m)
    await event.reply(
        f"**💬 សារចុងក្រោយពី `{getattr(peer, 'username', None) or peer.id}`៖**\n\n"
        f"{text}\n\n"
        f"**🔘 Buttons៖**\n{buttons_text}\n\n"
        f"វាយ `/btn <លេខ>` ដើម្បីចុច • `/send <text>` ដើម្បីផ្ញើសារ • `/refresh` ដើម្បី refresh",
        parse_mode="md",
    )


@bot.on(events.NewMessage(pattern=r"^/btn(?:\s+(\d+))?$"))
async def cmd_btn(event):
    uid = event.sender_id
    state = OPEN_CHAT.get(uid)
    if not state:
        await event.reply("⚠️ វាយ `/open @botusername` មុនសិន។", parse_mode="md")
        return
    arg = event.pattern_match.group(1)
    if not arg:
        await event.reply("ប្រើបែបនេះ៖ `/btn 1`", parse_mode="md")
        return
    idx = int(arg)
    client = await start_user_client(uid)
    if not client:
        await event.reply("⚠️ Session បាត់។ សូម /start ម្តងទៀត។")
        return
    try:
        m = await client.get_messages(state["peer"], ids=state["msg_id"])
    except Exception as e:
        await event.reply(f"❌ `{e}`", parse_mode="md")
        return
    flat = _flat_buttons(m)
    if idx < 1 or idx > len(flat):
        await event.reply(f"⚠️ លេខ button មិនត្រឹមត្រូវ (១ ដល់ {len(flat)})")
        return
    btn = flat[idx - 1]
    try:
        result = await m.click(idx - 1)
    except Exception as e:
        await event.reply(f"❌ ចុចមិនបាន៖ `{e}`", parse_mode="md")
        return

    # រង់ចាំ reply ថ្មីបន្តិច
    await event.reply(f"✅ បានចុច៖ **{getattr(btn, 'text', '?')}**", parse_mode="md")
    await asyncio.sleep(2)
    msgs = await client.get_messages(state["peer"], limit=1)
    if msgs and msgs[0].id != state["msg_id"]:
        new = msgs[0]
        OPEN_CHAT[uid]["msg_id"] = new.id
        await event.reply(
            f"**📩 សារថ្មី៖**\n\n{new.message or '_(គ្មានអត្ថបទ)_'}\n\n"
            f"**🔘 Buttons៖**\n{_format_buttons(new)}",
            parse_mode="md",
        )


@bot.on(events.NewMessage(pattern=r"^/send\s+(.+)$"))
async def cmd_send(event):
    uid = event.sender_id
    state = OPEN_CHAT.get(uid)
    if not state:
        await event.reply("⚠️ វាយ `/open @botusername` មុនសិន។", parse_mode="md")
        return
    client = await start_user_client(uid)
    if not client:
        return
    text = event.pattern_match.group(1)
    await client.send_message(state["peer"], text)
    await asyncio.sleep(2)
    msgs = await client.get_messages(state["peer"], limit=1)
    if msgs:
        new = msgs[0]
        OPEN_CHAT[uid]["msg_id"] = new.id
        await event.reply(
            f"✅ ផ្ញើរួច។\n\n**📩 សារថ្មី៖**\n{new.message or '_(គ្មានអត្ថបទ)_'}\n\n"
            f"**🔘 Buttons៖**\n{_format_buttons(new)}",
            parse_mode="md",
        )


@bot.on(events.NewMessage(pattern=r"^/refresh$"))
async def cmd_refresh(event):
    uid = event.sender_id
    state = OPEN_CHAT.get(uid)
    if not state:
        await event.reply("⚠️ គ្មាន chat បើក។ វាយ `/open @botusername` មុន។", parse_mode="md")
        return
    client = await start_user_client(uid)
    if not client:
        return
    msgs = await client.get_messages(state["peer"], limit=1)
    if not msgs:
        await event.reply("⚠️ គ្មានសារ។")
        return
    new = msgs[0]
    OPEN_CHAT[uid]["msg_id"] = new.id
    await event.reply(
        f"**📩 សារចុងក្រោយ៖**\n\n{new.message or '_(គ្មានអត្ថបទ)_'}\n\n"
        f"**🔘 Buttons៖**\n{_format_buttons(new)}",
        parse_mode="md",
    )


# -------------------- Login conversation --------------------
@bot.on(events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
async def login_flow(event):
    uid = event.sender_id
    st = LOGIN_STATE.get(uid)
    if not st:
        return  # មិនមែនពេល login — ignore
    text = (event.raw_text or "").strip()

    if st["step"] == "phone":
        phone = text.replace(" ", "")
        if not phone.startswith("+") or not phone[1:].isdigit():
            await event.reply("⚠️ ទម្រង់មិនត្រឹមត្រូវ។ សូមបញ្ចូលដូច `+855xxxxxxxx`", parse_mode="md")
            return
        user_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await user_client.connect()
        try:
            sent = await user_client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await event.reply("⚠️ លេខមិនត្រឹមត្រូវ។ សូមសាកម្តងទៀត ឬ /cancel")
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
            "សូមបញ្ចូល code (ឧ. `1 2 3 4 5`)\n"
            "_បំបែកតួអក្សរដើម្បីកុំឱ្យ Telegram លុប code ដោយស្វ័យប្រវត្តិ_",
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
        f"Username: @{me.username or '—'}\n"
        f"ID: `{me.id}`\n\n"
        f"បន្ទាប់៖\n"
        f"• /groups — បង្ហាញ groups\n"
        f"• /setfrom, /setto, /fwdon — រៀបចំ auto-forward",
        parse_mode="md",
    )
    await start_user_client(uid)


# -------------------- Startup --------------------
async def restore_all_user_clients():
    for fname in os.listdir(SESSIONS_DIR):
        if fname.endswith(".session"):
            try:
                uid = int(fname.split(".")[0])
                c = await start_user_client(uid)
                if c:
                    log.info(f"Restored user client uid={uid}")
            except Exception as e:
                log.warning(f"Failed to restore {fname}: {e}")


BOT_COMMANDS = [
    ("start", "ចាប់ផ្ដើម / បង្ហាញ menu"),
    ("help", "បង្ហាញ commands ទាំងអស់"),
    ("me", "ព័ត៌មាន account"),
    ("groups", "បញ្ជី groups និង channels"),
    ("logout", "លុប session"),
    ("cancel", "បោះបង់ការ login"),
    ("setfrom", "កំណត់ chat ប្រភព auto-forward"),
    ("setto", "កំណត់ chat គោលដៅ auto-forward"),
    ("fwdon", "បើក auto-forward"),
    ("fwdoff", "បិទ auto-forward"),
    ("fwdstatus", "ស្ថានភាព auto-forward"),
    ("autoclickon", "បើក auto-click @DropmailBot"),
    ("autoclickoff", "បិទ auto-click"),
    ("autoclickstatus", "ស្ថានភាព auto-click"),
    ("open", "បើក chat ដើម្បីចុច inline buttons"),
    ("btn", "ចុច button តាមលេខ"),
    ("send", "ផ្ញើសារទៅ chat ដែលបើក"),
    ("refresh", "refresh សារចុងក្រោយ"),
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
