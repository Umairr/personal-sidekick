"""Tools for the Sidekick: MCP servers (browser + sandbox filesystem), ready-made
LangChain tools, and two tools of my own (live currency conversion and calendar events)."""

import asyncio
import os
import re
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone

import requests
import wikipedia
from langchain_community.tools import GoogleSerperRun, WikipediaQueryRun
from langchain_community.utilities import GoogleSerperAPIWrapper, WikipediaAPIWrapper
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

wikipedia.set_user_agent("personal-sidekick (https://github.com/)")


# --------------------------------------------------------------------------- ready-made tools

def search_tool():
    return GoogleSerperRun(api_wrapper=GoogleSerperAPIWrapper())


wikipedia_lookup = WikipediaQueryRun(api_wrapper=WikipediaAPIWrapper())


@tool
def send_push_notification(text: str) -> str:
    """Send a short push notification to the user's phone."""
    response = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={"token": os.getenv("PUSHOVER_TOKEN"), "user": os.getenv("PUSHOVER_USER"), "message": text},
        timeout=15,
    )
    response.raise_for_status()
    return "Notification sent"


@tool
def request_human_help(instructions: str) -> str:
    """Ask the user to do something in the browser window that you cannot do yourself,
    such as logging in to a site, passing a captcha, or approving two-factor authentication.
    Explain exactly what you need them to do. The run pauses until they have done it,
    and their reply comes back to you as this tool's result."""
    return "The user says it is done. Continue with the task."


# --------------------------------------------------------------------------- my own tools

@tool
def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    """Convert an amount between currencies using today's live exchange rate.
    Use ISO codes such as USD, PKR, AED, EUR, GBP, SAR. Handy for comparing flight
    and hotel prices in the user's home currency."""
    src, dst = from_currency.strip().upper(), to_currency.strip().upper()
    data = requests.get(f"https://open.er-api.com/v6/latest/{src}", timeout=15).json()
    if data.get("result") != "success":
        return f"Could not fetch rates for {src}: {data.get('error-type', 'unknown error')}"
    rate = data["rates"].get(dst)
    if rate is None:
        return f"No rate available from {src} to {dst}"
    return (
        f"{amount:,.2f} {src} = {amount * rate:,.2f} {dst} "
        f"(rate {rate:.4f}, updated {data.get('time_last_update_utc', 'recently')})"
    )


def make_calendar_tool(sandbox: str):
    """Build the calendar tool bound to the sandbox folder."""

    @tool
    def create_calendar_event(
        title: str,
        start: str,
        duration_minutes: int = 60,
        location: str = "",
        notes: str = "",
    ) -> str:
        """Create a calendar event file (.ics) in the sandbox that the user can double-click
        to add to Apple Calendar, Google Calendar or Outlook. `start` is local time in ISO
        format, for example 2026-11-18T19:30. Use this for reminders, deadlines and bookings."""
        begin = datetime.fromisoformat(start)
        end = begin + timedelta(minutes=duration_minutes)
        fmt = "%Y%m%dT%H%M%S"
        esc = lambda s: s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
        ics = "\r\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//personal-sidekick//EN",
            "BEGIN:VEVENT",
            f"UID:{uuid.uuid4()}@personal-sidekick",
            f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
            f"DTSTART:{begin.strftime(fmt)}",
            f"DTEND:{end.strftime(fmt)}",
            f"SUMMARY:{esc(title)}",
            f"LOCATION:{esc(location)}",
            f"DESCRIPTION:{esc(notes)}",
            "BEGIN:VALARM",
            "TRIGGER:-PT30M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{esc(title)}",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ])
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "event"
        name = f"{begin:%Y-%m-%d}-{slug}.ics"
        with open(os.path.join(sandbox, name), "w") as fh:
            fh.write(ics)
        return f"Saved calendar event to sandbox/{name} ({begin:%a %d %b %H:%M}, {duration_minutes} min)"

    return create_calendar_event


# --------------------------------------------------------------------------- MCP servers

def mcp_connections(sandbox: str) -> dict:
    """A headed Playwright browser and a filesystem server locked to the sandbox."""
    return {
        "playwright": {
            "transport": "stdio",
            "command": "npx",
            "args": ["@playwright/mcp@latest", "--isolated"],
        },
        "filesystem": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", sandbox],
        },
    }


class McpSessions:
    """Keeps MCP sessions open so the browser keeps its state (cookies, login) between calls.

    The stdio transport must be opened and closed from the same asyncio task, so one
    background task owns the sessions and unwinds them when stop() is called."""

    def __init__(self, connections: dict):
        self.connections = connections
        self.tools = []
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task = None

    async def _run(self):
        client = MultiServerMCPClient(self.connections)
        async with AsyncExitStack() as stack:
            for name in self.connections:
                session = await stack.enter_async_context(client.session(name))
                self.tools += await load_mcp_tools(session, server_name=name)
            self._ready.set()
            await self._stop.wait()

    async def start(self) -> list:
        self._task = asyncio.create_task(self._run())
        ready = asyncio.create_task(self._ready.wait())
        await asyncio.wait([ready, self._task], return_when=asyncio.FIRST_COMPLETED)
        ready.cancel()
        if self._task.done():
            self._task.result()  # surface the real startup error
        return self.tools

    async def stop(self):
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:
                pass


async def get_all_tools(sandbox: str):
    sessions = McpSessions(mcp_connections(sandbox))
    mcp_tools = await sessions.start()
    ours = [
        search_tool(),
        wikipedia_lookup,
        send_push_notification,
        request_human_help,
        convert_currency,
        make_calendar_tool(sandbox),
    ]
    return ours + mcp_tools, sessions
