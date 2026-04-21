"""
Telegram Bot — ប្រើ Bot Token ពី @BotFather រួមជាមួយ API ID / API Hash។
"""
import os
import asyncio
from telethon import TelegramClient, events

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SESSION = "bot"

client = TelegramClient(SESSION, API_ID, API_HASH)


@client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    sender = await event.get_sender()
    name = sender.first_name or "មិត្ត"
    await event.reply(
        f"សួស្តី {name}! 👋\n\n"
        "ខ្ញុំជា bot ដែលដំណើរការតាម Telethon។\n\n"
        "Commands:\n"
        "/start — ចាប់ផ្តើម\n"
        "/ping — សាកល្បង\n"
        "/id — បង្ហាញ chat ID និង user ID\n"
        "/me — បង្ហាញព័ត៌មានរបស់អ្នក"
    )


@client.on(events.NewMessage(pattern=r"^/ping$"))
async def ping(event):
    await event.reply("🏓 Pong!")


@client.on(events.NewMessage(pattern=r"^/id$"))
async def get_id(event):
    await event.reply(
        f"💬 Chat ID: `{event.chat_id}`\n"
        f"👤 Your ID: `{event.sender_id}`",
        parse_mode="md",
    )


@client.on(events.NewMessage(pattern=r"^/me$"))
async def me(event):
    s = await event.get_sender()
    await event.reply(
        f"👤 **{s.first_name or ''} {s.last_name or ''}**\n"
        f"Username: @{s.username or '—'}\n"
        f"ID: `{s.id}`",
        parse_mode="md",
    )


async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"✅ Bot started as: @{me.username} (id={me.id})")
    print("Commands: /start  /ping  /id  /me")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
