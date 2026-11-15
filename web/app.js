// Sidekick web client: streams events from the server and renders feed, inbox, plan.

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => (window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parse(s || "")) : `<p>${esc(s)}</p>`);

const PRESETS = [
  {
    label: "✈️ Expedia flights",
    message:
      "Sign in to my Expedia account first (ask me to log in when you reach the login page). Then search Expedia for return flights from Karachi (KHI) to Dubai (DXB), leaving next Friday and returning the Sunday after, 1 adult, economy.",
    criteria:
      "I logged in when asked, and you list the 3 cheapest options (airline, times, stops, price) with prices also converted to PKR, saved to flights.md in the sandbox.",
  },
  {
    label: "🗓️ Plan my week",
    message:
      "Help me plan this week. Ask me for my 3 most important tasks, then make a day-by-day plan and create calendar events for each focused work block.",
    criteria: "A plan saved to week-plan.md and one .ics calendar event per task, all in the sandbox.",
  },
  {
    label: "💱 Price check",
    message:
      "Find the current price of the iPhone 17 Pro (256GB) on Apple's US store and in the UAE, convert both to PKR, and tell me which is cheaper including any tax refund for tourists in Dubai.",
    criteria: "Both prices with sources, both converted to PKR with today's rate, and a clear recommendation. Send me a push notification with the answer.",
  },
];

const ICONS = {
  browser: "🌐", files: "📁", core: "⚙️",
  google_serper: "🔎", wikipedia: "📚", send_push_notification: "📲",
  request_human_help: "🙋", convert_currency: "💱", create_calendar_event: "🗓️", write_todos: "📝",
};

const state = {
  ready: false,
  busy: false,
  pending: [],
  decisions: [],
  tools: {},
  mission: null, // { root, timeline, attempt }
  steps: {},     // tool_call_id -> element
  lastAnswer: null,
  knownFiles: new Set(),
};

// ------------------------------------------------------------------ status

function setPill(kind, label) {
  const pill = $("#status-pill");
  pill.className = `pill pill-${kind}`;
  pill.querySelector(".label").textContent = label;
}

function refreshControls() {
  $("#go-btn").disabled = !state.ready || state.busy || state.pending.length > 0;
  $("#reset-btn").disabled = state.busy;
}

async function pollStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    if (s.error) { setPill("error", "Startup failed"); showError(s.error); return; }
    if (!s.ready) { setPill("booting", "Starting browser…"); setTimeout(pollStatus, 1200); return; }
    state.ready = true;
    renderTools(s.tools);
    if (s.model) {
      $("#brand-sub").textContent = `personal co-worker · ${s.model}`;
      $("#brand-sub").title = `Evaluator: ${s.evaluator_model}`;
    }
    if (s.project) {
      const a = $("#trace-link");
      a.href = "https://smith.langchain.com/";
      a.title = `Project: ${s.project}`;
      a.hidden = false;
    }
    if (s.pending?.length) { state.pending = s.pending; renderInbox(); setPill("waiting", "Waiting for you"); }
    else if (!state.busy) setPill("idle", "Ready");
    refreshControls();
  } catch {
    setPill("error", "Server offline");
    setTimeout(pollStatus, 2000);
  }
}

// ------------------------------------------------------------------ left column

function renderPresets() {
  const box = $("#presets");
  PRESETS.forEach((p) => {
    const b = el("button", "chip", esc(p.label));
    b.onclick = () => { $("#message").value = p.message; $("#criteria").value = p.criteria; $("#message").focus(); };
    box.appendChild(b);
  });
}

function renderTools(tools) {
  state.tools = Object.fromEntries(tools.map((t) => [t.name, t]));
  $("#tool-count").textContent = tools.length;
  const groups = { core: "Core & my tools", files: "Sandbox filesystem", browser: "Browser (Playwright)" };
  const box = $("#toolbelt");
  box.innerHTML = "";
  for (const [key, title] of Object.entries(groups)) {
    const list = tools.filter((t) => t.group === key);
    if (!list.length) continue;
    const g = el("div", "group");
    g.appendChild(el("div", "group-name", `${title} · ${list.length}`));
    const tags = el("div", "tags");
    list.forEach((t) => tags.appendChild(el("span", `tag${t.gated ? " gated" : ""}`, esc(t.name.replace(/^browser_/, "")))));
    g.appendChild(tags);
    box.appendChild(g);
  }
}

async function refreshFiles() {
  try {
    const files = await (await fetch("/api/files")).json();
    const ul = $("#files");
    ul.innerHTML = "";
    if (!files.length) { ul.innerHTML = '<li class="muted small">Nothing yet</li>'; return; }
    files.forEach((f) => {
      const fresh = !state.knownFiles.has(f.path) && state.knownFiles.size > 0;
      const li = el("li", fresh ? "fresh" : "");
      li.innerHTML = `<a href="/api/files/${encodeURI(f.path)}" target="_blank">${esc(f.path)}</a><span class="muted small">${(f.size / 1024).toFixed(1)}kb</span>`;
      ul.appendChild(li);
    });
    state.knownFiles = new Set(files.map((f) => f.path).concat(["__init__"]));
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ feed

function scrollFeed() {
  const feed = $("#feed");
  feed.scrollTop = feed.scrollHeight;
}

function startMission(message, criteria) {
  $("#empty")?.remove();
  const root = el("div", "mission");
  const bubble = el("div", "bubble-user", esc(message));
  if (criteria) bubble.appendChild(el("div", "crit", `<b>Done when:</b> ${esc(criteria)}`));
  root.appendChild(bubble);
  const timeline = el("div", "timeline");
  root.appendChild(timeline);
  $("#feed").appendChild(root);
  state.mission = { root, timeline, attempt: 0 };
  state.steps = {};
  scrollFeed();
}

function ensureAttempt(n) {
  if (!state.mission || state.mission.attempt === n) return;
  state.mission.attempt = n;
  if (n > 1) state.mission.timeline.appendChild(el("div", "attempt-label", `Attempt ${n} · acting on evaluator feedback`));
}

function argsSummary(name, args) {
  if (!args) return "";
  if (args.url) return args.url;
  if (args.path) return args.path;
  if (args.query) return args.query;
  if (args.instructions) return args.instructions;
  if (args.text) return args.text;
  if (args.title) return args.title;
  if (name === "convert_currency") return `${args.amount} ${args.from_currency} → ${args.to_currency}`;
  if (name === "write_todos") return `${(args.todos || []).length} items`;
  const s = JSON.stringify(args);
  return s === "{}" ? "" : s;
}

function addToolCall(ev) {
  if (!state.mission) return;
  const group = state.tools[ev.name]?.group || "core";
  const icon = ICONS[ev.name] || ICONS[group] || "⚙️";
  const step = el("div", "step");
  step.innerHTML = `
    <div class="step-head">
      <div class="step-icon">${icon}</div>
      <div class="step-name">${esc(ev.name.replace(/^browser_/, "browser · "))}</div>
      <div class="step-args">${esc(argsSummary(ev.name, ev.args))}</div>
      <div class="step-state">${ev.gated ? "🔒 " : ""}<span class="spinner"></span></div>
    </div>
    <div class="step-body">
      <div class="lbl">Arguments</div><pre>${esc(JSON.stringify(ev.args, null, 2))}</pre>
      <div class="result"></div>
    </div>`;
  step.querySelector(".step-head").onclick = () => step.classList.toggle("open");
  state.mission.timeline.appendChild(step);
  state.steps[ev.id] = step;
  scrollFeed();
}

function addToolResult(ev) {
  const step = state.steps[ev.id];
  if (!step) return;
  const rejected = /rejected|not executed|not approved/i.test(ev.content) && state.tools[ev.name]?.gated;
  const failed = ev.status === "error" || /^That tool call failed/.test(ev.content);
  step.classList.add(failed ? "failed" : rejected ? "blocked" : "done");
  step.querySelector(".step-state").innerHTML = failed ? "failed" : rejected ? "rejected" : "✓";
  step.querySelector(".result").innerHTML = `<div class="lbl">Result</div><pre>${esc(ev.content)}</pre>`;
  if (["write_file", "append_file", "edit_file", "move_file", "copy_file", "create_directory", "create_calendar_event"].includes(ev.name)) refreshFiles();
}

function addThought(text) {
  if (!state.mission) return;
  state.mission.timeline.appendChild(el("div", "thought", esc(text)));
}

function addAnswer(ev) {
  if (!state.mission) return;
  if (state.lastAnswer) state.lastAnswer.classList.add("superseded");
  const card = el("div", "answer");
  card.innerHTML = `<div class="answer-head">💬 Sidekick · attempt ${ev.attempt}</div><div class="md">${md(ev.content)}</div>`;
  state.mission.timeline.appendChild(card);
  state.lastAnswer = card;
  scrollFeed();
}

function addVerdict(ev) {
  if (!state.mission) return;
  let cls = "retry", badge = "↻ Retrying";
  if (ev.met) { cls = "met"; badge = "✓ Criteria met"; }
  else if (ev.needs_input) { cls = "ask"; badge = "? Needs your input"; }
  else if (ev.final) { cls = "giveup"; badge = "✕ Gave up after 3 tries"; }
  const v = el("div", `verdict ${cls}`, `<span class="badge">${badge}</span><span>${esc(ev.feedback)}</span>`);
  state.mission.timeline.appendChild(v);
  if (ev.met && state.lastAnswer) state.lastAnswer.classList.remove("superseded");
  scrollFeed();
}

function showError(msg) {
  const box = el("div", "error-box", `<b>Error:</b> ${esc(msg)}`);
  (state.mission?.timeline || $("#feed")).appendChild(box);
  scrollFeed();
}

// ------------------------------------------------------------------ plan

function renderPlan(todos) {
  const ol = $("#plan");
  ol.innerHTML = "";
  if (!todos?.length) { ol.innerHTML = '<li class="muted small">No plan for this task.</li>'; $("#plan-bar").style.width = "0"; return; }
  todos.forEach((t) => {
    const li = el("li", t.status);
    li.innerHTML = `<span class="box">${t.status === "completed" ? "✓" : ""}</span><span>${esc(t.content)}</span>`;
    ol.appendChild(li);
  });
  const done = todos.filter((t) => t.status === "completed").length;
  $("#plan-bar").style.width = `${Math.round((done / todos.length) * 100)}%`;
}

// ------------------------------------------------------------------ inbox (human in the loop)

function renderInbox() {
  const box = $("#inbox");
  const card = $("#inbox-card");
  box.innerHTML = "";
  state.decisions = new Array(state.pending.length).fill(null);
  $("#inbox-count").textContent = state.pending.length || "";
  card.classList.toggle("alert", state.pending.length > 0);
  if (!state.pending.length) { box.innerHTML = '<div class="muted small">Nothing waiting. 👌</div>'; return; }

  state.pending.forEach((a, i) => {
    const c = el("div", "ask");
    if (a.kind === "help") {
      c.innerHTML = `
        <div class="ask-kind">🙋 Your turn in the browser</div>
        <div class="ask-help">${esc(a.args.instructions)}</div>
        <textarea rows="2" placeholder="Optional note for the Sidekick, e.g. 'logged in, 2FA done'"></textarea>
        <div class="row"><button class="btn btn-ok btn-sm" data-act="respond">Done, continue</button></div>`;
    } else {
      const preview = a.args.content !== undefined
        ? `<b>${esc(a.args.path || "")}</b>\n\n${esc(a.args.content)}`
        : esc(JSON.stringify(a.args, null, 2));
      c.innerHTML = `
        <div class="ask-kind">🔒 Approval needed</div>
        <div class="ask-title">${esc(a.name)}</div>
        <pre>${preview}</pre>
        <textarea rows="5" class="edit-box" hidden>${esc(JSON.stringify(a.args, null, 2))}</textarea>
        <input type="text" class="reason" placeholder="Reason (if rejecting)" hidden />
        <div class="row">
          <button class="btn btn-ok btn-sm" data-act="approve">Approve</button>
          ${a.allowed.includes("edit") ? '<button class="btn btn-sm" data-act="edit">Edit</button>' : ""}
          <button class="btn btn-bad btn-sm" data-act="reject">Reject</button>
        </div>`;
    }
    c.querySelectorAll("button").forEach((b) => (b.onclick = () => decide(i, b.dataset.act, c, a, b)));
    box.appendChild(c);
  });
}

function decide(i, act, card, action, button) {
  if (act === "edit") {
    const box = card.querySelector(".edit-box");
    if (box.hidden) { box.hidden = false; button.textContent = "Save edit"; return; }
    try {
      const args = JSON.parse(box.value);
      state.decisions[i] = { type: "edit", edited_action: { name: action.name, args } };
    } catch { box.style.borderColor = "var(--bad)"; return; }
  } else if (act === "reject") {
    const reason = card.querySelector(".reason");
    if (reason.hidden) { reason.hidden = false; reason.focus(); button.textContent = "Confirm reject"; return; }
    state.decisions[i] = { type: "reject", message: reason.value.trim() || "The user rejected this action. Do not retry it unless asked." };
  } else if (act === "respond") {
    const note = card.querySelector("textarea").value.trim();
    state.decisions[i] = { type: "respond", message: `The user has done it in the browser.${note ? " Their note: " + note : ""} Continue with the task.` };
  } else {
    state.decisions[i] = { type: "approve" };
  }
  card.classList.add("decided");
  if (state.decisions.every(Boolean)) submitDecisions();
}

async function submitDecisions() {
  const decisions = state.decisions;
  state.pending = [];
  renderInbox();
  await stream("/api/resume", { decisions });
}

// ------------------------------------------------------------------ streaming

function handle(ev) {
  switch (ev.type) {
    case "status":
      if (ev.state === "working") { setPill("working", "Working…"); ensureAttempt(ev.attempt); }
      else if (ev.state === "evaluating") setPill("evaluating", "Checking the result…");
      else if (ev.state === "waiting") setPill("waiting", "Waiting for you");
      else setPill("idle", "Ready");
      break;
    case "todos": renderPlan(ev.todos); break;
    case "thought": addThought(ev.content); break;
    case "tool_call": addToolCall(ev); break;
    case "tool_result": addToolResult(ev); break;
    case "interrupt":
      state.pending = ev.actions;
      renderInbox();
      // nothing runs until the user decides, so every unfinished step is on hold
      Object.values(state.steps).forEach((step) => {
        if (step.querySelector(".spinner")) step.querySelector(".step-state").innerHTML = "⏸ on hold";
      });
      break;
    case "answer": addAnswer(ev); break;
    case "evaluation": addVerdict(ev); break;
    case "error": showError(ev.message); break;
  }
}

async function stream(url, body) {
  state.busy = true;
  refreshControls();
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || res.statusText);
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (chunk.startsWith("data: ")) handle(JSON.parse(chunk.slice(6)));
      }
    }
  } catch (e) {
    showError(e.message);
    setPill("error", "Connection lost");
  } finally {
    state.busy = false;
    refreshControls();
    refreshFiles();
  }
}

async function launch() {
  const message = $("#message").value.trim();
  const criteria = $("#criteria").value.trim();
  if (!message || $("#go-btn").disabled) return;
  startMission(message, criteria);
  renderPlan([]);
  $("#message").value = "";
  await stream("/api/run", { message, success_criteria: criteria });
}

async function reset() {
  if (state.busy) return;
  await fetch("/api/reset", { method: "POST" });
  state.ready = false;
  state.pending = [];
  state.mission = null;
  renderInbox();
  renderPlan([]);
  $("#feed").innerHTML = `<div class="empty" id="empty"><div class="empty-orb"></div><h1>Fresh start.</h1><p>New browser, empty memory. What's next?</p></div>`;
  refreshControls();
  pollStatus();
}

// ------------------------------------------------------------------ boot

renderPresets();
$("#go-btn").onclick = launch;
$("#reset-btn").onclick = reset;
$("#files-refresh").onclick = refreshFiles;
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") launch();
});
refreshFiles();
pollStatus();
