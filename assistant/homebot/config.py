import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    llm_api_key: str
    llm_base_url: str
    llm_models: tuple[str, ...]
    timezone: str
    db_path: str
    telegram_token: str
    telegram_users: frozenset[int]
    discord_token: str
    discord_users: frozenset[int]
    app_logins: frozenset[str]
    app_port: int
    ha_url: str
    ha_token: str
    jellyfin_url: str
    jellyfin_key: str
    adguard_url: str
    adguard_user: str
    adguard_password: str


def _user_ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    try:
        return frozenset(int(part) for part in raw.split(",") if part.strip())
    except ValueError as e:
        raise ConfigError(f"{name} must be comma-separated numeric user IDs") from e


def _port(raw: str) -> int:
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ConfigError(f"APP_PORT must be a port number, got {raw!r}")
    return int(raw)


def load() -> Config:
    env = os.environ.get
    cfg = Config(
        llm_api_key=env("LLM_API_KEY", ""),
        # Any OpenAI-compatible endpoint works; the default is Gemini's free tier.
        llm_base_url=env("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        # Comma-separated; later models are fallbacks for when the first is overloaded.
        llm_models=tuple(m.strip() for m in env("LLM_MODEL", "gemini-3.6-flash,gemini-3.5-flash").split(",") if m.strip()),
        timezone=env("TZ", "Asia/Kolkata"),
        db_path=env("DB_PATH", "homebot.db"),
        telegram_token=env("TELEGRAM_BOT_TOKEN", ""),
        telegram_users=_user_ids("TELEGRAM_ALLOWED_USER_IDS"),
        discord_token=env("DISCORD_BOT_TOKEN", ""),
        discord_users=_user_ids("DISCORD_ALLOWED_USER_IDS"),
        app_logins=frozenset(x.strip() for x in env("APP_ALLOWED_LOGINS", "").split(",") if x.strip()),
        app_port=_port(env("APP_PORT", "8787")),
        ha_url=env("HOME_ASSISTANT_URL", "http://localhost:8123").rstrip("/"),
        ha_token=env("HOME_ASSISTANT_TOKEN", ""),
        jellyfin_url=env("JELLYFIN_URL", "http://localhost:8096").rstrip("/"),
        jellyfin_key=env("JELLYFIN_API_KEY", ""),
        adguard_url=env("ADGUARD_URL", "http://localhost").rstrip("/"),
        adguard_user=env("ADGUARD_USERNAME", ""),
        adguard_password=env("ADGUARD_PASSWORD", ""),
    )
    if not cfg.llm_api_key:
        raise ConfigError("LLM_API_KEY is not set")
    if not cfg.llm_models:
        raise ConfigError("LLM_MODEL is empty")
    if not cfg.telegram_token and not cfg.discord_token and not cfg.app_logins:
        raise ConfigError("Enable at least one of TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN, APP_ALLOWED_LOGINS")
    return cfg
