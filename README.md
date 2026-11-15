# Personal Sidekick

A personal AI assistant that handles errands for me: it opens a real browser, looks things up,
writes notes and reports, converts prices into rupees, puts reminders in my calendar and pings
my phone when it's done.

It doesn't act on its own when the stakes are real. Anything that writes to disk or sends me a
notification waits for my approval. When it hits a login page or a captcha, it stops and asks me
to step in.

<!-- Add a screenshot of the UI here: docs/screenshot.png -->

---

## Why I built this

This started as the "Sidekick" project in week 4 (LangChain & LangGraph) of an agentic AI course.
The course version worked, but it felt like a demo: a Gradio chat box, file writes nobody checked,
and very little visibility into what the agent was actually doing.

So I rebuilt it as something I'd want to use every day:

- **A UI that shows what the agent is doing.** Every browser click, search and file write appears
  on a live timeline. You can expand any step to see its exact inputs and outputs.
- **Approvals before anything risky.** File writes, calendar events and push notifications stop
  in a *Needs you* inbox. You can approve them, reject them with a reason, or edit the arguments
  first (fix a file name, tweak the content).
- **A proper hand-off for logins.** When it reaches a login, 2FA or bot check, it tells you exactly
  what to do in its browser window. Your note ("logged in, 2FA done") goes straight back to it.
- **An answer gets checked before you're told it's done.** A second model grades each answer
  against your own "done when…" line and sends it back to try again (up to 3 times) if it falls short.
- **Tools I actually needed.** Live currency conversion (I think in PKR) and calendar events I can
  double-click to add.

## What it can do

| Capability | How |
|---|---|
| Browse the web like a person | [Playwright MCP](https://github.com/microsoft/playwright-mcp): navigate, click, type, read pages |
| Search | Google via Serper, plus Wikipedia |
| Read and write files | [Filesystem MCP](https://github.com/modelcontextprotocol/servers), locked to `sandbox/` |
| Convert money | `convert_currency`: live rates from open.er-api.com |
| Make calendar events | `create_calendar_event`: writes a standard `.ics` file |
| Notify you | Pushover push notification |
| Plan its work | LangChain `TodoListMiddleware`, shown as a live checklist in the UI |

🔒 Needs approval: `write_file`, `append_file`, `edit_file`, `create_directory`, `move_file`,
`copy_file`, `create_calendar_event`, `send_push_notification`.

## How it works

```
 Browser UI  ──fetch + SSE──►  FastAPI (server.py)
 web/                               │
                                    ▼
                       Sidekick (sidekick/agent.py)
                       │
                       ├── Worker: LangChain create_agent
                       │     middleware: tool-error recovery · todo list · PII redaction
                       │                 · model-call limit · human-in-the-loop
                       │
                       ├── Tools (sidekick/tools.py)
                       │     Playwright MCP · Filesystem MCP · Serper · Wikipedia
                       │     push · request_human_help · convert_currency · create_calendar_event
                       │
                       └── Evaluator: structured-output LLM that grades the answer
                             against your success criteria → accept / retry / ask you
```

The approval flow in one paragraph: when the worker calls a gated tool, `HumanInTheLoopMiddleware`
pauses the LangGraph run and saves its state. The server streams an `interrupt` event and the UI
shows a card. Your decision (`approve`, `edit`, `reject` or `respond`) is sent back as
`Command(resume={"decisions": [...]})`, and the graph continues exactly where it stopped.

## Run it locally

**You need:** Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 20+ (the MCP servers run
through `npx`), and API keys for OpenAI, Serper and Pushover.

```bash
git clone https://github.com/<your-username>/personal-sidekick.git
cd personal-sidekick
cp .env.example .env        # add your keys
uv sync
uv run server.py
```

Open the link printed in the terminal (normally http://localhost:8765). If that port is busy, the
server picks the next free one and tells you. The first start takes a minute while `npx`
downloads the MCP servers.

Locally the browser window is **visible**. That matters: it's how you log in when the Sidekick
asks for help.

### Things to try

1. **Expedia errand.** Click the *✈️ Expedia flights* preset and press Launch. When it reaches the
   login page it pauses. Log in inside *its* Chromium window, then press **Done, continue**.
   Travel sites fight automation, so you will hit a bot wall now and then. When that happens it
   asks you for help or falls back to Google Flights.
2. **See the approval gate.** Ask it to save anything to a file. `write_file` stops in *Needs you*.
   Try **Edit** to rename the file before it's written.
3. **Give it a real task for this week.** The trick is a success criterion someone else could check:

   > Find 3 gyms within 3 km of my office in Clifton, Karachi, with monthly fees and timings.
   >
   > **Done when:** a table of 3 gyms with fee in PKR, timings and a Google Maps link is saved to
   > `gyms.md`, and there's a calendar event for a trial visit on Saturday at 10:00.

Everything it produces lands in `sandbox/` and shows up in the *Sandbox files* panel.

## Run it with Docker

The image bundles Python, Node, both MCP servers and a headless Chromium, so the host needs nothing
except Docker.

```bash
cp .env.example .env            # add your keys, and set SIDEKICK_PASSWORD
docker compose up -d --build
docker compose logs -f          # wait for "Sidekick → http://localhost:8765"
```

Open http://localhost:8765. To publish on a different host port, set
`SIDEKICK_PUBLISH_PORT=9000` before `docker compose up`.

A few things to know:

- **The browser is headless in Docker**, so there is no window to log in through. Research,
  price checks and file work all run fine. Tasks that need *your* login (like the Expedia errand)
  are best run locally.
- `sandbox/` is mounted from the host, so files survive restarts and rebuilds.
- `/healthz` is an unauthenticated health check, used by Docker and by AWS load balancers.
- Conversation memory lives in the process. Restarting the container starts a fresh session.

## Deploy to AWS

The simplest setup that works well is **one EC2 instance running Docker Compose**, with Caddy in
front for HTTPS. The Sidekick keeps its session in memory and drives a browser, so it wants one
long-lived machine, not a fleet of short-lived containers.

> ⚠️ **Always set `SIDEKICK_PASSWORD` before exposing it.** Anyone who can open the page can spend
> your OpenAI credits, send you notifications and drive a browser from your server.

### 1. Launch the instance

- **AMI:** Ubuntu 24.04 LTS
- **Type:** `t3.medium` (2 vCPU, 4 GB). Chromium needs the memory; `t3.small` struggles.
- **Storage:** 20 GB gp3
- **Security group:** allow **22** from *your IP only*, plus **80** and **443** from anywhere.
  Don't open 8765.

### 2. Install Docker

```bash
ssh -i your-key.pem ubuntu@<ec2-public-ip>

curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit        # log out and back in so the group change applies
```

### 3. Get the code and configure it

```bash
ssh -i your-key.pem ubuntu@<ec2-public-ip>
git clone https://github.com/<your-username>/personal-sidekick.git
cd personal-sidekick
cp .env.example .env
nano .env        # add your keys, SIDEKICK_USERNAME and a long SIDEKICK_PASSWORD
```

### 4. Add HTTPS with Caddy

Point a domain (or subdomain such as `sidekick.example.com`) at the instance's public IP with an
**A record**. An Elastic IP keeps that address from changing when the instance restarts.

Create `Caddyfile` next to `docker-compose.yml`:

```caddy
sidekick.example.com {
    reverse_proxy sidekick:8765 {
        flush_interval -1      # stream agent events immediately
    }
}
```

Then create `docker-compose.prod.yml`:

```yaml
services:
  sidekick:
    ports: !reset []          # only Caddy is exposed
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on:
      - sidekick
volumes:
  caddy_data:
```

### 5. Start it

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Caddy gets a Let's Encrypt certificate automatically. Open `https://sidekick.example.com`
and sign in with your username and password.

**Update after a `git push`:**

```bash
cd personal-sidekick && git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Other AWS options

- **ECS on Fargate:** push the image to ECR and run **exactly one task** (sessions are in memory).
  Give it 1 vCPU and 2–4 GB, and add the env vars as Secrets Manager secrets. Raise the ALB
  **idle timeout to 300 s** so long agent runs aren't cut off mid-stream. Health check path: `/healthz`.
- **App Runner:** not recommended. Its request timeout ends long-running agent streams.

### Rough monthly cost

A `t3.medium` on demand is roughly **$30/month** in most regions, plus storage and data transfer.
Stopping the instance when you're not using it brings that down a lot. OpenAI usage is separate;
with `gpt-5.4-mini`, a typical errand costs cents.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `OPENAI_API_KEY` | — | Required |
| `SERPER_API_KEY` | — | Required for web search |
| `PUSHOVER_USER`, `PUSHOVER_TOKEN` | — | Push notifications |
| `SIDEKICK_MODEL` | `gpt-5.4-mini` | Model for both the worker and the evaluator |
| `SIDEKICK_PORT` | `8765` | Preferred port; the next free one is used if it's taken |
| `SIDEKICK_HOST` | `127.0.0.1` | `0.0.0.0` in Docker |
| `SIDEKICK_USERNAME`, `SIDEKICK_PASSWORD` | — | Turns on HTTP Basic auth for every page except `/healthz` |
| `SIDEKICK_HEADLESS` | `false` | Hide the browser window (`true` in Docker) |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | — | Trace every run in [LangSmith](https://smith.langchain.com) |

## Project structure

```
personal-sidekick/
├── server.py              FastAPI app: streaming run/resume, status, files, auth, port fallback
├── sidekick/
│   ├── agent.py           the agent, approval rules, evaluator loop, event stream
│   └── tools.py           MCP sessions, ready-made tools, my own tools
├── web/                   index.html · styles.css · app.js (no build step, no framework)
├── sandbox/               the only folder the agent can write to (git-ignored)
├── Dockerfile
└── docker-compose.yml
```

## Known limitations

- **One user, one session at a time.** It's a personal tool; state lives in memory.
- **Sites block bots.** Expedia, Booking and friends sometimes show a wall. It asks for help, but
  it can't beat a captcha for you.
- **No login hand-off in headless mode.** Run locally for errands that need your accounts.

## Built with

[LangChain](https://python.langchain.com) ·
[LangGraph](https://langchain-ai.github.io/langgraph/) ·
[Model Context Protocol](https://modelcontextprotocol.io) ·
[Playwright](https://playwright.dev) · [FastAPI](https://fastapi.tiangolo.com) ·
[uv](https://docs.astral.sh/uv/)
