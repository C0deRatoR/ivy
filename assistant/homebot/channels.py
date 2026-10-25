import asyncio

import discord
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .agent import Agent


def chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class TelegramChannel:
    def __init__(self, token: str, allowed: frozenset[int], agent: Agent) -> None:
        self.allowed = allowed
        self.agent = agent
        self.app = Application.builder().token(token).build()
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat, message = update.effective_user, update.effective_chat, update.message
        if user is None or chat is None or message is None or message.text is None:
            return
        if user.id not in self.allowed:
            # Telling strangers their ID is how the owner finds theirs for the allowlist.
            await message.reply_text(f"Not authorized. Your Telegram user ID is {user.id}.")
            return
        await chat.send_action(ChatAction.TYPING)
        reply = await self.agent.handle(f"telegram:{chat.id}", message.text)
        for part in chunks(reply, 4096):
            await message.reply_text(part)

    async def send(self, chat_id: str, text: str) -> None:
        for part in chunks(text, 4096):
            await self.app.bot.send_message(chat_id=int(chat_id), text=part)

    async def run(self) -> None:
        async with self.app:
            await self.app.start()
            assert self.app.updater is not None
            await self.app.updater.start_polling()
            await asyncio.Event().wait()


class DiscordChannel(discord.Client):
    """Answers DMs, and messages that @mention the bot in servers.

    Default intents are enough: Discord delivers message content for DMs and mentions
    without the privileged Message Content intent.
    """

    def __init__(self, allowed: frozenset[int], agent: Agent) -> None:
        super().__init__(intents=discord.Intents.default())
        self.allowed = allowed
        self.agent = agent

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return
        if message.guild is not None and self.user not in message.mentions:
            return
        if message.author.id not in self.allowed:
            await message.channel.send(f"Not authorized. Your Discord user ID is {message.author.id}.")
            return
        text = message.content.replace(self.user.mention, "").strip()
        async with message.channel.typing():
            reply = await self.agent.handle(f"discord:{message.channel.id}", text)
        for part in chunks(reply, 2000):
            await message.channel.send(part)

    async def send(self, channel_id: str, text: str) -> None:
        channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"Discord channel {channel_id} can't receive messages")
        for part in chunks(text, 2000):
            await channel.send(part)
