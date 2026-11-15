"""The Sidekick: a create_agent worker wrapped in an evaluator loop, streamed as events.

Compared with the course version (4_langchain_langgraph/sidekick.py):
- every filesystem write tool is gated behind HumanInTheLoopMiddleware (approve / edit / reject)
- request_human_help uses the "respond" decision, so the user's own words go back to the agent
- two tools of my own: convert_currency (live rates) and create_calendar_event (.ics files)
- run() / resume() are async generators of small JSON events that the web UI renders live
"""

import os
import uuid
from datetime import datetime
from typing import Any, AsyncIterator

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    PIIMiddleware,
    TodoListMiddleware,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from .models import build_model, build_structured, model_spec
from .tools import get_all_tools

MODEL = model_spec()
EVALUATOR_MODEL = model_spec("SIDEKICK_EVALUATOR_MODEL", fallback=MODEL)
MAX_ATTEMPTS = 3

# Filesystem MCP tools that change something on disk, plus my own file-writing tool
FILESYSTEM_WRITE_TOOLS = {
    "write_file", "append_file", "edit_file", "create_directory", "move_file", "copy_file",
    "delete_file", "remove_file",  # in case a future server version adds them
}
OWN_WRITE_TOOLS = {"create_calendar_event"}


class EvaluatorOutput(BaseModel):
    feedback: str = Field(description="Feedback on the assistant's response")
    success_criteria_met: bool = Field(description="Whether the success criteria have been met")
    user_input_needed: bool = Field(
        description="True if the assistant has a question, needs clarification, or is stuck and needs the user"
    )


WORKER_PROMPT = """You are Sidekick, a capable personal assistant who completes real tasks for the user.
You have a real web browser, a sandbox filesystem, web search, Wikipedia, live currency conversion,
a calendar-event maker, and push notifications.

Browser rules:
- Navigate to a page and read it with a snapshot rather than clicking around unnecessarily.
- Dismiss cookie banners and popups yourself.
- If you reach something only a human can do (logging in, a captcha, two-factor authentication,
  a bot-check wall), call request_human_help with exact, short instructions for what to do in
  YOUR browser window. Then continue from where you were once they reply.
- Travel sites block automation sometimes. If a page is blocked, say so, try once more, then ask the
  user for help or fall back to another site (Google Flights: https://www.google.com/travel/flights?q=...).
- For Expedia, sign-in lives at https://www.expedia.com/login (or the user's regional Expedia site).

Files: write outputs (reports, plans, comparisons) as Markdown in the sandbox. Writing files needs the
user's approval, which the system asks for automatically — just call the tool.

Money: when you quote prices in a foreign currency, also convert them with convert_currency if the user
asked for their home currency.

Keep working until the success criteria are met, or until you genuinely need to ask the user something.
When finished, give a clear final answer: what you did, what you produced (file names), and what you found."""


class TolerateToolErrors(AgentMiddleware):
    """Hand tool failures back to the model so it can recover, instead of crashing the run."""

    async def awrap_tool_call(self, request, handler):
        try:
            return await handler(request)
        except Exception as error:
            return ToolMessage(
                content=f"That tool call failed: {error}. Try another approach.",
                tool_call_id=request.tool_call["id"],
            )


def _short(value: Any, limit: int = 1200) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


class Sidekick:
    def __init__(self, sandbox: str):
        self.sandbox = sandbox
        self.thread_id = str(uuid.uuid4())
        self.memory = InMemorySaver()
        self.tools: list = []
        self.sessions = None
        self.worker = None
        self.evaluator = None
        self.gated: list[str] = []
        self.task = ""
        self.success_criteria = ""
        self.attempts = 0
        self.pending: list[dict] = []  # action requests waiting for the user
        self._seen: set[str] = set()
        self._todos: list = []

    # ------------------------------------------------------------------ lifecycle

    async def setup(self):
        os.makedirs(self.sandbox, exist_ok=True)
        self.tools, self.sessions = await get_all_tools(self.sandbox)
        names = {t.name for t in self.tools}

        interrupt_on: dict[str, Any] = {
            "send_push_notification": {"allowed_decisions": ["approve", "reject"]},
            "request_human_help": {"allowed_decisions": ["respond"]},
        }
        for name in sorted((FILESYSTEM_WRITE_TOOLS | OWN_WRITE_TOOLS) & names):
            interrupt_on[name] = {"allowed_decisions": ["approve", "edit", "reject"]}
        self.gated = sorted(interrupt_on)

        self.worker = create_agent(
            model=build_model(MODEL),
            tools=self.tools,
            system_prompt=f"{WORKER_PROMPT}\nToday is {datetime.now():%A %d %B %Y}.",
            middleware=[
                TolerateToolErrors(),
                TodoListMiddleware(),
                PIIMiddleware("credit_card", apply_to_tool_results=True),
                ModelCallLimitMiddleware(run_limit=40),
                HumanInTheLoopMiddleware(interrupt_on=interrupt_on),
            ],
            checkpointer=self.memory,
        )
        self.evaluator = build_structured(EVALUATOR_MODEL, EvaluatorOutput)

    async def cleanup(self):
        if self.sessions:
            await self.sessions.stop()

    def tool_catalog(self) -> list[dict]:
        core = {"google_serper", "wikipedia", "send_push_notification", "request_human_help",
                "convert_currency", "create_calendar_event"}
        out = []
        for t in self.tools:
            group = "browser" if t.name.startswith("browser_") else ("core" if t.name in core else "files")
            out.append({"name": t.name, "group": group, "gated": t.name in self.gated})
        return out

    @property
    def paused(self) -> bool:
        return bool(self.pending)

    # ------------------------------------------------------------------ running

    async def run(self, message: str, success_criteria: str) -> AsyncIterator[dict]:
        self.task = message
        self.success_criteria = success_criteria.strip() or "The answer should be clear, correct and complete"
        self.attempts = 0
        payload = {
            "messages": [{
                "role": "user",
                "content": f"{message}\n\nThe success criteria for this task are: {self.success_criteria}",
            }]
        }
        async for event in self._advance(payload):
            yield event

    async def resume(self, decisions: list[dict]) -> AsyncIterator[dict]:
        if len(decisions) != len(self.pending):
            yield {"type": "error", "message": f"Expected {len(self.pending)} decisions, got {len(decisions)}"}
            return
        self.pending = []
        async for event in self._advance(Command(resume={"decisions": decisions})):
            yield event

    async def _advance(self, payload) -> AsyncIterator[dict]:
        config = {"configurable": {"thread_id": self.thread_id}, "run_name": "sidekick"}
        while True:
            yield {"type": "status", "state": "working", "attempt": self.attempts + 1}
            state = None
            async for state in self.worker.astream(payload, config=config, stream_mode="values"):
                for event in self._diff(state):
                    yield event

            if state and "__interrupt__" in state:
                request = state["__interrupt__"][0].value
                configs = {c["action_name"]: c["allowed_decisions"] for c in request["review_configs"]}
                self.pending = [
                    {
                        "id": i,
                        "name": a["name"],
                        "args": a["args"],
                        "allowed": configs.get(a["name"], ["approve", "reject"]),
                        "kind": "help" if a["name"] == "request_human_help" else "approval",
                    }
                    for i, a in enumerate(request["action_requests"])
                ]
                yield {"type": "interrupt", "actions": self.pending}
                yield {"type": "status", "state": "waiting"}
                return

            messages = (state or {}).get("messages", [])
            reply = _content_text(messages[-1].content) if messages else ""
            tools_used = [c["name"] for m in messages for c in (getattr(m, "tool_calls", None) or [])]
            self.attempts += 1
            yield {"type": "answer", "content": reply, "attempt": self.attempts}

            yield {"type": "status", "state": "evaluating"}
            verdict = await self._evaluate(reply, tools_used)
            done = verdict.success_criteria_met or verdict.user_input_needed or self.attempts >= MAX_ATTEMPTS
            yield {
                "type": "evaluation",
                "met": verdict.success_criteria_met,
                "needs_input": verdict.user_input_needed,
                "feedback": verdict.feedback,
                "attempt": self.attempts,
                "final": done,
            }
            if done:
                yield {"type": "status", "state": "idle"}
                return
            payload = {"messages": [{
                "role": "user",
                "content": "Your last response did not meet the success criteria. "
                f"Feedback: {verdict.feedback}. Please keep working and address it.",
            }]}

    def _diff(self, state: dict) -> list[dict]:
        """Turn a full state snapshot into events for messages/todos we have not sent yet."""
        events = []
        todos = state.get("todos")
        if todos is not None and todos != self._todos:
            self._todos = todos
            events.append({"type": "todos", "todos": todos})
        for m in state.get("messages", []):
            if m.id in self._seen:
                continue
            self._seen.add(m.id)
            if isinstance(m, AIMessage):
                text = _content_text(m.content).strip()
                if m.tool_calls and text:
                    events.append({"type": "thought", "content": text})
                for call in m.tool_calls:
                    events.append({"type": "tool_call", "id": call["id"], "name": call["name"],
                                   "args": call["args"], "gated": call["name"] in self.gated})
            elif isinstance(m, ToolMessage):
                events.append({"type": "tool_result", "id": m.tool_call_id, "name": m.name,
                               "status": getattr(m, "status", "success"),
                               "content": _short(_content_text(m.content))})
        return events

    async def _evaluate(self, last_reply: str, tools_used: list[str]) -> EvaluatorOutput:
        prompt = f"""You decide whether an assistant has met the success criteria for a task.

The user's request was:
{self.task}

The success criteria are:
{self.success_criteria}

The tools the assistant called while working, in order:
{", ".join(tools_used) or "none"}

The assistant's most recent reply was:
{last_reply}

Decide whether the success criteria are met, using the tool calls as evidence of what was actually done.
Also decide whether the assistant needs more input from the user: it asked a question, needs
clarification, or seems stuck. Give brief, concrete feedback."""
        return await self.evaluator.ainvoke(prompt)
