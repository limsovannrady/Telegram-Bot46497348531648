"""
Telegram Userbot — ដំណើរការដោយប្រើ session ដែល login.py បានបង្កើត។
បន្ថែម handlers របស់អ្នកនៅខាងក្រោម។
"""
import os
from telethon import TelegramClient, events

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = "userbot"

client = TelegramClient(SESSION, API_ID, API_HASH)


@client.on(events.NewMessage(pattern=r"^\.ping$", outgoing=True))
async def ping(event):
    """វាយ .ping នៅក្នុង chat ណាមួយ -> bot នឹង edit ទៅ Pong!"""
    await event.edit("🏓 Pong!")


@client.on(events.NewMessage(pattern=r"^\.id$", outgoing=True))
async def get_id(event):
    """វាយ .id -> បង្ហាញ chat id"""
    await event.edit(f"💬 Chat ID: `{event.chat_id}`")


@client.on(events.NewMessage(pattern=r"^\.me$", outgoing=True))
async def me(event):
    me = await client.get_me()
    await event.edit(f"👤 {me.first_name} (@{me.username}) | id=`{me.id}`")


async def main():
    await client.start()
    me = await client.get_me()
    print(f"✅ Userbot started as: {me.first_name} (@{me.username})")
    print("Commands: .ping  .id  .me  (វាយនៅក្នុង chat ណាក៏បាន)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
