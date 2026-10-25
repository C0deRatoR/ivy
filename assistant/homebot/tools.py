import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import psutil
from openai.types.chat import ChatCompletionToolParam

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

# The agent asks the user before running these. Lights and media act at once: harmless and easy to undo.
NEEDS_CONFIRMATION = frozenset({"adguard_pause"})

# Locks, alarms and the like are left out on purpose: too risky for a chat bot.
CONTROLLABLE_DOMAINS = frozenset({"light", "switch", "fan", "climate", "media_player"})
LISTED_DOMAINS = CONTROLLABLE_DOMAINS | {"sensor", "binary_sensor", "cover", "lock"}
ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
COLOR_NAME = re.compile(r"^[A-Za-z ]{3,30}$")
MAX_PAUSE_MINUTES = 120


def _tool(
    name: str, description: str, properties: dict[str, object] | None = None, optional: tuple[str, ...] = ()
) -> ChatCompletionToolParam:
    props = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": [p for p in props if p not in optional],
            },
        },
    }


def describe(name: str, args: dict[str, object]) -> str:
    """One line a person can read when asked to confirm a tool call."""
    if name == "adguard_pause":
        return f"Pause ad blocking for {args.get('minutes')} minutes"
    return f"{name} {json.dumps(args)}"


TOOLS: list[ChatCompletionToolParam] = [
    _tool("ha_list_entities", "List smart-home devices and sensors from Home Assistant with their current state."),
    _tool(
        "ha_control",
        "Turn a Home Assistant device on or off. Only light, switch, fan, climate and media_player "
        "entities can be controlled. Get the entity_id from ha_list_entities first. "
        "For lights with action turn_on you may also set brightness_pct, and either color or kelvin.",
        {
            "entity_id": {"type": "string", "description": "For example light.living_room"},
            "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle"]},
            "brightness_pct": {"type": "integer", "description": "1 to 100"},
            "color": {"type": "string", "description": "A CSS colour name, e.g. red, blue, orange, purple"},
            "kelvin": {"type": "integer", "description": "White temperature, 2200 (warm) to 6500 (cool)"},
        },
        optional=("brightness_pct", "color", "kelvin"),
    ),
    _tool(
        "jellyfin_search",
        "Search the Jellyfin media library for movies and shows by title.",
        {"query": {"type": "string"}},
    ),
    _tool("jellyfin_recent", "List the movies and shows most recently added to Jellyfin."),
    _tool("adguard_stats", "Get AdGuard Home ad-blocking statistics: queries, blocked count, top blocked domains."),
    _tool(
        "adguard_pause",
        f"Pause AdGuard ad blocking for a number of minutes (1 to {MAX_PAUSE_MINUTES}).",
        {"minutes": {"type": "integer"}},
    ),
    _tool(
        "server_status",
        "Get home server health: load, memory, disk space, temperatures, uptime, and whether "
        "Home Assistant, Jellyfin and AdGuard respond.",
    ),
    _tool("add_note", "Save a note for the user.", {"text": {"type": "string"}}),
    _tool("list_notes", "List the user's saved notes, newest first."),
    _tool(
        "add_reminder",
        "Schedule a reminder message to the user. 'at' is a local date and time in ISO 8601, "
        "for example 2026-10-23T09:00.",
        {"text": {"type": "string"}, "at": {"type": "string"}},
    ),
]


class ToolError(Exception):
    """A failure the model is told about, in words safe to show it."""


def _arg(args: dict[str, object], name: str, kind: type) -> object:
    value = args.get(name)
    # bool is a subclass of int; don't accept true/false as a number of minutes.
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise ToolError(f"'{name}' must be a {kind.__name__}")
    return value


def _optional(args: dict[str, object], name: str, kind: type) -> object | None:
    return None if args.get(name) is None else _arg(args, name, kind)


def _text(args: dict[str, object], name: str) -> str:
    value = str(_arg(args, name, str)).strip()
    if not value:
        raise ToolError(f"'{name}' must not be empty")
    return value


class Toolbox:
    def __init__(self, cfg: Config, http: httpx.AsyncClient, store: Store) -> None:
        self.cfg = cfg
        self.http = http
        self.store = store

    async def run(self, name: str, args: dict[str, object], chat_key: str) -> str:
        try:
            match name:
                case "ha_list_entities":
                    return await self._ha_list()
                case "ha_control":
                    return await self._ha_control(
                        _text(args, "entity_id"),
                        _text(args, "action"),
                        brightness_pct=_optional(args, "brightness_pct", int),
                        color=_optional(args, "color", str),
                        kelvin=_optional(args, "kelvin", int),
                    )
                case "jellyfin_search":
                    return await self._jellyfin({"searchTerm": _text(args, "query")})
                case "jellyfin_recent":
                    return await self._jellyfin({"SortBy": "DateCreated", "SortOrder": "Descending"})
                case "adguard_stats":
                    return await self._adguard_stats()
                case "adguard_pause":
                    return await self._adguard_pause(int(str(_arg(args, "minutes", int))))
                case "server_status":
                    return await self._server_status()
                case "add_note":
                    self.store.add_note(chat_key, _text(args, "text"))
                    return "Saved."
                case "list_notes":
                    notes = self.store.list_notes(chat_key)
                    return json.dumps([{"saved": self._local(ts), "text": t} for ts, t in notes]) if notes else "No notes."
                case "add_reminder":
                    return self._add_reminder(chat_key, _text(args, "text"), _text(args, "at"))
                case _:
                    raise ToolError(f"Unknown tool {name}")
        except httpx.HTTPStatusError as e:
            log.warning("tool %s got HTTP %s from %s", name, e.response.status_code, e.request.url.host)
            raise ToolError(f"The service answered with HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            log.warning("tool %s failed: %r", name, e)
            raise ToolError("The service is not reachable right now") from e

    def _local(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, ZoneInfo(self.cfg.timezone)).strftime("%Y-%m-%d %H:%M")

    # Home Assistant

    def _ha_headers(self) -> dict[str, str]:
        if not self.cfg.ha_token:
            raise ToolError("Home Assistant is not set up (HOME_ASSISTANT_TOKEN is empty)")
        return {"Authorization": f"Bearer {self.cfg.ha_token}"}

    async def _ha_list(self) -> str:
        r = await self.http.get(f"{self.cfg.ha_url}/api/states", headers=self._ha_headers())
        r.raise_for_status()
        rows = [
            f"{s['entity_id']} | {s.get('attributes', {}).get('friendly_name', '')} | {s['state']}"
            for s in r.json()
            if s["entity_id"].split(".", 1)[0] in LISTED_DOMAINS
        ]
        return "\n".join(rows[:150]) or "No devices found."

    async def _ha_control(
        self,
        entity_id: str,
        action: str,
        *,
        brightness_pct: object | None = None,
        color: object | None = None,
        kelvin: object | None = None,
    ) -> str:
        if not ENTITY_ID.match(entity_id):
            raise ToolError(f"'{entity_id}' is not a valid entity_id")
        domain = entity_id.split(".", 1)[0]
        if domain not in CONTROLLABLE_DOMAINS:
            raise ToolError(f"{domain} devices can't be controlled from chat")
        if action not in {"turn_on", "turn_off", "toggle"}:
            raise ToolError(f"Unknown action {action}")

        payload: dict[str, object] = {"entity_id": entity_id}
        if brightness_pct is not None:
            if not isinstance(brightness_pct, int) or not 1 <= brightness_pct <= 100:
                raise ToolError("brightness_pct must be between 1 and 100")
            payload["brightness_pct"] = brightness_pct
        if color is not None and kelvin is not None:
            raise ToolError("Use either color or kelvin, not both")
        if color is not None:
            if not isinstance(color, str) or not COLOR_NAME.match(color):
                raise ToolError(f"'{color}' is not a colour name")
            payload["color_name"] = color.lower().replace(" ", "")  # Home Assistant wants e.g. "lightblue"
        if kelvin is not None:
            if not isinstance(kelvin, int) or not 2000 <= kelvin <= 6500:
                raise ToolError("kelvin must be between 2000 and 6500")
            payload["color_temp_kelvin"] = kelvin
        if len(payload) > 1 and (domain != "light" or action != "turn_on"):
            raise ToolError("Brightness and colour only work with turn_on on a light")

        r = await self.http.post(
            f"{self.cfg.ha_url}/api/services/{domain}/{action}", headers=self._ha_headers(), json=payload
        )
        if r.status_code == 400:
            raise ToolError("Home Assistant rejected that setting (unknown colour, or the device can't do it)")
        r.raise_for_status()
        extras = ", ".join(f"{k}={v}" for k, v in payload.items() if k != "entity_id")
        return f"Done: {action} {entity_id}" + (f" ({extras})" if extras else "")

    # Jellyfin

    async def _jellyfin(self, params: dict[str, str]) -> str:
        if not self.cfg.jellyfin_key:
            raise ToolError("Jellyfin is not set up (JELLYFIN_API_KEY is empty)")
        r = await self.http.get(
            f"{self.cfg.jellyfin_url}/Items",
            headers={"Authorization": f'MediaBrowser Token="{self.cfg.jellyfin_key}"'},
            params={"Recursive": "true", "IncludeItemTypes": "Movie,Series", "Limit": "15", **params},
        )
        r.raise_for_status()
        items = [
            f"{i.get('Name')} ({i.get('ProductionYear', '?')}, {i.get('Type')})" for i in r.json().get("Items", [])
        ]
        return "\n".join(items) or "Nothing found."

    # AdGuard Home

    def _adguard_auth(self) -> tuple[str, str]:
        if not self.cfg.adguard_user:
            raise ToolError("AdGuard is not set up (ADGUARD_USERNAME is empty)")
        return (self.cfg.adguard_user, self.cfg.adguard_password)

    async def _adguard_stats(self) -> str:
        r = await self.http.get(f"{self.cfg.adguard_url}/control/stats", auth=self._adguard_auth())
        r.raise_for_status()
        s = r.json()
        return json.dumps(
            {
                "dns_queries": s.get("num_dns_queries"),
                "blocked": s.get("num_blocked_filtering"),
                "top_blocked_domains": s.get("top_blocked_domains", [])[:5],
            }
        )

    async def _adguard_pause(self, minutes: int) -> str:
        if not 1 <= minutes <= MAX_PAUSE_MINUTES:
            raise ToolError(f"minutes must be between 1 and {MAX_PAUSE_MINUTES}")
        r = await self.http.post(
            f"{self.cfg.adguard_url}/control/protection",
            auth=self._adguard_auth(),
            json={"enabled": False, "duration": minutes * 60_000},
        )
        r.raise_for_status()
        return f"Ad blocking paused for {minutes} minutes."

    # Server

    async def _server_status(self) -> str:
        mem = psutil.virtual_memory()
        disks = {}
        for path in ("/", "/srv/share"):
            if os.path.isdir(path):
                du = shutil.disk_usage(path)
                disks[path] = f"{du.free / 1e9:.0f} GB free of {du.total / 1e9:.0f} GB"
        temps = {
            name: max(t.current for t in readings)
            for name, readings in psutil.sensors_temperatures().items()
            if readings
        }
        services = {}
        for name, url in (
            ("home_assistant", self.cfg.ha_url),
            ("jellyfin", f"{self.cfg.jellyfin_url}/health"),
            ("adguard", self.cfg.adguard_url),
        ):
            try:
                r = await self.http.get(url, timeout=3)
                services[name] = "up" if r.status_code < 500 else f"error {r.status_code}"
            except httpx.HTTPError:
                services[name] = "down"
        return json.dumps(
            {
                "load_1_5_15": os.getloadavg(),
                "memory": f"{mem.used / 1e9:.1f} GB used of {mem.total / 1e9:.1f} GB",
                "disks": disks,
                "temperatures_c": temps,
                "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
                "services": services,
            }
        )

    # Reminders

    def _add_reminder(self, chat_key: str, text: str, at: str) -> str:
        try:
            due = datetime.fromisoformat(at)
        except ValueError as e:
            raise ToolError(f"'{at}' is not an ISO 8601 date and time") from e
        if due.tzinfo is None:
            due = due.replace(tzinfo=ZoneInfo(self.cfg.timezone))
        if due.timestamp() <= time.time():
            raise ToolError("That time is in the past")
        self.store.add_reminder(chat_key, text, due.timestamp())
        return f"Reminder set for {self._local(due.timestamp())}."
