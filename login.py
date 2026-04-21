"""
Login ចូល Telegram personal account ម្ដងគត់ ដើម្បីបង្កើតឯកសារ session។
ដំណើរការនៅក្នុង Shell: python3 login.py
"""
import os
import asyncio
from telethon import TelegramClient

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = "userbot"


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Logged in as: {me.first_name} (@{me.username}) | id={me.id}")
    print(f"📁 Session saved: {SESSION}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
