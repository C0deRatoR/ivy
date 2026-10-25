import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import httpx
import openai

from . import config
from .agent import Agent
from .app_api import AppChannel
from .channels import DiscordChannel, TelegramChannel
from .store import Store
from .tools import Toolbox

log = logging.getLogger("homebot")

Sender = Callable[[str, str], Awaitable[None]]


async def reminder_loop(store: Store, senders: dict[str, Sender]) -> None:
    while True:
        for reminder_id, chat_key, text in store.due_reminders(time.time()):
            channel, chat_id = chat_key.split(":", 1)
            try:
                await senders[channel](chat_id, f"⏰ Reminder: {text}")
            except Exception:
                # Any failure (network, channel disabled) must not stop the loop; retried next tick.
                log.exception("could not send reminder %s", reminder_id)
                continue
            store.mark_sent(reminder_id)
        await asyncio.sleep(30)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every URL, including the bot token
    try:
        cfg = config.load()
    except config.ConfigError as e:
        sys.exit(f"Config error: {e}")

    store = Store(cfg.db_path)
    async with httpx.AsyncClient(timeout=10) as http:
        llm = openai.AsyncOpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, timeout=30, max_retries=1)
        agent = Agent(llm, cfg.llm_models, Toolbox(cfg, http, store), cfg.timezone)
        senders: dict[str, Sender] = {}
        tasks: list[Coroutine[Any, Any, None]] = []
        if cfg.telegram_token:
            telegram = TelegramChannel(cfg.telegram_token, cfg.telegram_users, agent)
            senders["telegram"] = telegram.send
            tasks.append(telegram.run())
        if cfg.discord_token:
            discord_bot = DiscordChannel(cfg.discord_users, agent)
            senders["discord"] = discord_bot.send
            tasks.append(discord_bot.start(cfg.discord_token))
        if cfg.app_logins:
            app = AppChannel(cfg.app_logins, agent, store, cfg.app_port)
            senders["app"] = app.send
            tasks.append(app.run())
        tasks.append(reminder_loop(store, senders))
        log.info("started with models %s on %s", ", ".join(cfg.llm_models), ", ".join(senders))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
