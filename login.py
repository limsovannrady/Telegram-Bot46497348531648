"""
ស្គ្រីបសម្រាប់ log in ចូល Telegram account ផ្ទាល់ខ្លួនមួយដង។
ដំណើរការនៅក្នុង Shell: python login.py
បន្ទាប់ពី log in រួច វានឹងបង្កើតឯកសារ session (userbot.session)
ដែល userbot.py អាចប្រើបានដោយមិនចាំបាច់សួរ code ម្តងទៀត។
"""
import os
import asyncio
from telethon import TelegramClient

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = "userbot"


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()  # វានឹងសួរលេខទូរស័ព្ទ + code + ពាក្យសម្ងាត់ 2FA បើមាន
    me = await client.get_me()
    print(f"\n✅ Logged in successfully as: {me.first_name} (@{me.username}) | id={me.id}")
    print(f"📁 Session file saved to: {SESSION}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
