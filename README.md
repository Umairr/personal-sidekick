# Personal Sidekick

A personal AI co-worker that drives a **real browser**, searches the web, writes files, converts
currencies, makes calendar events and pings your phone. Anything that changes your files or
reaches your phone **waits for your approval** first.

Built on LangChain `create_agent` + LangGraph, with a custom FastAPI + vanilla JS web UI.
This started as the Sidekick from *4_langchain_langgraph / lab 5* of the Agentic AI course and
grew from there.

## What's different from the course version

| | Course (lab 5) | This repo |
|---|---|---|
| UI | Gradio | Custom web app: live tool timeline, "Needs you" inbox, plan with progress, sandbox file browser |
| Filesystem writes | run freely | **gated**: `write_file`, `append_file`, `edit_file`, `create_directory`, `move_file`, `copy_file` need Approve / Edit / Reject |
| Human help | approve-only | `request_human_help` uses the **respond** decision: your note ("logged in, 2FA done") goes back to the agent |
| Own tools | none | `convert_currency` (live rates, e.g. USD→PKR), `create_calendar_event` (.ics file, also gated) |
| Streaming | final answer only | every tool call, result, plan update, answer and evaluator verdict streams live |

## How it works

```
you ──► FastAPI (server.py) ──SSE──► web UI
              │
              ▼
        Sidekick (sidekick/agent.py)
        ├─ worker: create_agent + middleware
        │    TolerateToolErrors · TodoList · PII · ModelCallLimit · HumanInTheLoop
        ├─ tools (sidekick/tools.py)
        │    Playwright MCP (browser) · filesystem MCP (sandbox/) · Serper · Wikipedia
        │    push notification · request_human_help · convert_currency · create_calendar_event
        └─ evaluator loop: checks the answer against your "Done when…" criteria,
           sends it back with feedback up to 3 times
```

When the worker calls a gated tool, `HumanInTheLoopMiddleware` interrupts the graph. The server
streams an `interrupt` event, the UI shows a card in **Needs you**, and your decision is sent back
as `Command(resume={"decisions": [...]})`.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js (for the MCP servers via `npx`).

```bash
cp .env.example .env      # then fill in your keys
uv sync
uv run server.py          # opens on http://localhost:8765
```

The first start downloads the Playwright and filesystem MCP servers, so give it a minute.
Set `LANGSMITH_TRACING=true` to trace every run in LangSmith (project from `LANGSMITH_PROJECT`).

## Try it

1. **Expedia errand**: click the *✈️ Expedia flights* preset, then Launch. When the Sidekick reaches
   the login page it calls `request_human_help` and pauses. Log in inside **its** browser window,
   then press *Done, continue* in the inbox. Travel sites fight automation, so you'll sometimes hit
   a bot wall. That's part of working with real-world agents; the Sidekick will ask for help or
   fall back to Google Flights.
2. **Approval gate**: ask it to save anything to a file and watch `write_file` stop in *Needs you*.
   Try **Edit** to change the file name or content before it runs, or **Reject** with a reason.
3. **A real task for this week**: write a request plus a *Done when…* line that a stranger could check,
   for example:
   > Find 3 gyms within 3 km of my office in Clifton, Karachi, with monthly fees and timings.
   > **Done when:** a table of 3 gyms with fee in PKR, timings and Google Maps link is saved to
   > `gyms.md`, and a calendar event is created for a trial visit on Saturday at 10:00.

Outputs land in `sandbox/` (git-ignored) and show up in the *Sandbox files* panel.

## Project layout

```
server.py            FastAPI app: /api/run, /api/resume (SSE), /api/status, /api/files, /api/reset
sidekick/agent.py    Sidekick class: worker, approval config, evaluator loop, event stream
sidekick/tools.py    MCP sessions + LangChain tools + my own tools
web/                 index.html · styles.css · app.js (no build step)
sandbox/             the only folder the agent can write to
```
