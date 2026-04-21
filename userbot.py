"""
Telegram Userbot — Auto-forward messages ពី chat មួយ (ឬច្រើន) ទៅ chat គោលដៅ។

កំណត់ ENV vars ទាំងនេះ៖
  FORWARD_FROM : chat IDs ឬ @usernames បំបែកដោយ comma  (ឧ.  -1001234567890,@somechannel)
  FORWARD_TO   : chat ID ឬ @username ឬ "me" (Saved Messages)  [default: me]

Commands (វាយនៅក្នុង chat ណាក៏បាន ពី account អ្នក)៖
  .id        — បង្ហាញ chat ID របស់ chat បច្ចុប្បន្ន
  .chats     — បង្ហាញបញ្ជី chat ទាំងអស់រួមនឹង ID
  .fwdstatus — បង្ហាញការកំណត់ auto-forward បច្ចុប្បន្ន
"""
import os
import asyncio
from telethon import TelegramClient, events

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = "userbot"

FORWARD_FROM_RAW = os.environ.get("FORWARD_FROM", "").strip()
FORWARD_TO_RAW = os.environ.get("FORWARD_TO", "me").strip() or "me"


def parse_targets(raw: str):
    """បំលែង '−1001234,@chan' ទៅជា list [−1001234, '@chan']"""
    items = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        try:
            items.append(int(s))
        except ValueError:
            items.append(s)
    return items


FORWARD_FROM = parse_targets(FORWARD_FROM_RAW)
try:
    FORWARD_TO = int(FORWARD_TO_RAW)
except ValueError:
    FORWARD_TO = FORWARD_TO_RAW  # "me" ឬ @username

client = TelegramClient(SESSION, API_ID, API_HASH)


# --- Auto-forward handler ---
@client.on(events.NewMessage())
async def auto_forward(event):
    if not FORWARD_FROM:
        return
    # ពិនិត្យមើលថាសារមកពី chat គោលដៅឬអត់
    resolved_ids = []
    for src in FORWARD_FROM:
        try:
            ent = await client.get_entity(src)
            resolved_ids.append(ent.id)
        except Exception:
            pass
    if event.chat_id in resolved_ids or (event.chat and event.chat.id in resolved_ids):
        try:
            await client.forward_messages(FORWARD_TO, event.message)
        except Exception as e:
            print(f"⚠️  Forward failed: {e}")


# --- Helper commands (outgoing only) ---
@client.on(events.NewMessage(pattern=r"^\.id$", outgoing=True))
async def show_id(event):
    await event.edit(f"💬 Chat ID: `{event.chat_id}`")


@client.on(events.NewMessage(pattern=r"^\.chats$", outgoing=True))
async def list_chats(event):
    lines = ["**📋 Chats ថ្មីៗ៖**"]
    async for d in client.iter_dialogs(limit=25):
        lines.append(f"`{d.id}` — {d.name}")
    await event.edit("\n".join(lines), parse_mode="md")


@client.on(events.NewMessage(pattern=r"^\.fwdstatus$", outgoing=True))
async def fwd_status(event):
    src = FORWARD_FROM_RAW or "(មិនបានកំណត់)"
    dst = FORWARD_TO_RAW
    await event.edit(
        f"**🔁 Auto-forward Config**\n"
        f"From: `{src}`\nTo: `{dst}`",
        parse_mode="md",
    )


async def main():
    await client.start()
    me = await client.get_me()
    print(f"✅ Userbot started as: {me.first_name} (@{me.username})")
    if FORWARD_FROM:
        print(f"🔁 Auto-forwarding from {FORWARD_FROM_RAW} → {FORWARD_TO_RAW}")
    else:
        print("ℹ️  FORWARD_FROM មិនបានកំណត់ — auto-forward បិទ")
    print("Commands: .id  .chats  .fwdstatus")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
