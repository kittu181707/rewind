"use strict";

const element = (id) => document.getElementById(id);
const chat = element("chat");
const live = document.body.dataset.mode === "live";
const choices = new Map();
let running = false;
let replayStopped = false;
let replayDecision;

function add(parent, tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined && value !== null) node.textContent = value;
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function card(title, className = "") {
  const node = add(chat, "article", null, `card ${className}`);
  add(node, "h2", title);
  return node;
}

function focusCard(node) {
  node.tabIndex = -1;
  node.focus();
}

function table(node, title, headers, rows) {
  const wrapper = add(node, "div", null, "table-wrap");
  const grid = add(wrapper, "table");
  add(grid, "caption", title);
  const heading = add(add(grid, "thead"), "tr");
  for (const text of headers) add(heading, "th", text).scope = "col";
  const body = add(grid, "tbody");
  for (const row of rows) {
    const item = add(body, "tr");
    row.forEach((value, index) => {
      if (index === 0) add(item, "th", value).scope = "row";
      else add(item, "td", value ?? "Not recorded", String(value).endsWith("(changed)") ? "changed" : "");
    });
  }
}

const flagValue = (value) => value === true ? "ON" : value === false ? "OFF" : "Not recorded";

function labeled(node, label, text) {
  const line = add(node, "p");
  add(line, "strong", `${label}: `);
  line.append(document.createTextNode(text ?? "Not recorded"));
}

function setRunning(value) {
  running = value;
  element("run").disabled = value;
  element("request").disabled = value;
  element("play").disabled = value;
  element("case").disabled = value;
  element("controls").hidden = !value;
  element("stop").disabled = !value;
}

async function post(path, body) {
  const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(typeof problem.detail === "string" ? problem.detail : "Rewind rejected this request.");
  }
  return response.json();
}

function resolveGate(id, approved) {
  const gate = choices.get(id);
  if (!gate) return;
  gate.actions.hidden = true;
  gate.note.textContent = approved ? "Approved" : "Declined";
  choices.delete(id);
}

function gateCard(data) {
  const node = card(data.gate === 1 ? "Gate 1 · Approve rehearsal" : "Gate 2 · Approve staging", "gate");
  labeled(node, "Environment", data.environment);
  if (data.preview?.problem) {
    labeled(node, "Problem found in rehearsal", data.preview.problem);
    labeled(node, "Diagnosis", data.preview.diagnosis);
    labeled(node, "Verified fix", data.preview.fix);
    table(node, "Who sees what", ["User", "Flag", "Before fix", "After fix"], data.preview.visibility.map((row) => {
      const changed = row.before !== null && row.after !== null && row.before !== row.after;
      return [row.user, row.flag, flagValue(row.before), `${flagValue(row.after)}${changed ? " (changed)" : ""}`];
    }));
  }
  if (data.writes?.length) {
    add(node, "h3", "Exact flag writes, in order");
    const list = add(node, "ol");
    for (const write of data.writes) add(list, "li", write.stage ? `${write.stage}: ${write.text}` : write.text);
  }
  if (data.fixture_writes?.length) {
    add(node, "h3", "Fixture database changes");
    const fixture = add(node, "ul");
    for (const write of data.fixture_writes) add(fixture, "li", write);
  }
  if (data.snapshot_hash) labeled(node, "Starting snapshot", data.snapshot_hash);
  if (data.starting_flags?.length) {
    const starting = add(node, "details");
    add(starting, "summary", "Starting staging flag state");
    table(starting, "Staging flag settings at approval time", ["Flag", "Default", "Overrides"],
      data.starting_flags.map((flag) => [flag.flag, flagValue(flag.enabled),
        flag.overrides.map((item) => `${item.kind} ${item.name}: ${flagValue(item.enabled)}`).join(", ") || "None"]));
  }
  add(node, "p", data.summary, "plan muted");
  const actions = add(node, "div", null, "actions");
  const approve = add(actions, "button", "Approve", "primary");
  const decline = add(actions, "button", "Decline");
  const note = add(node, "p", null, "muted");
  note.role = "status";
  choices.set(data.id, {actions, note});
  async function choose(decision) {
    approve.disabled = true;
    decline.disabled = true;
    if (!live) {
      replayDecision?.(decision === "approve");
      return;
    }
    try {
      await post("api/decision", {gate_id: data.id, decision});
    } catch (error) {
      note.textContent = error.message;
      approve.disabled = false;
      decline.disabled = false;
    }
  }
  approve.addEventListener("click", () => choose("approve"));
  decline.addEventListener("click", () => choose("decline"));
  focusCard(node);
}

function show(event) {
  const data = event.data;
  switch (event.type) {
    case "started":
      chat.replaceChildren();
      choices.clear();
      element("notice").textContent = "";
      element("progress").textContent = live ? "Starting rehearsal…" : "Starting replay…";
      setRunning(true);
      break;
    case "interpretation": {
      const node = card("Interpreted request");
      labeled(node, "Request", data.text);
      add(node, "p", data.interpretation);
      break;
    }
    case "progress":
      element("progress").textContent = data.text;
      break;
    case "failure": {
      const node = card("Problem found in rehearsal");
      add(node, "p", data.problem);
      table(node, "Test user results", ["User", "HTTP status", "Version that ran"],
        data.users.map((row) => [row.user, row.http_status, row.version]));
      break;
    }
    case "attempt": {
      const node = card(`Attempt ${data.number} · ${data.label}`);
      add(node, "p", data.diagnosis);
      if (data.fix) labeled(node, "Proposed fix", data.fix);
      break;
    }
    case "gate":
      gateCard(data);
      break;
    case "decision":
      resolveGate(data.id, data.approved);
      break;
    case "final": {
      const node = card(data.verdict, "final");
      add(node, "p", data.text);
      if (data.error) add(node, "p", data.error);
      if (data.ledger) labeled(node, "Autonomy", data.ledger);
      if (data.breakdown) add(node, "p", data.breakdown, "muted");
      if (data.report) {
        const link = add(node, "a", "Open evidence report");
        link.href = data.report;
        link.target = "_blank";
        link.rel = "noopener";
      }
      setRunning(false);
      focusCard(node);
      break;
    }
  }
}

async function inventory() {
  const flags = element("flags");
  try {
    const response = await fetch("api/flags");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not read the flags.");
    flags.replaceChildren();
    if (!data.flags.length) add(flags, "p", "No flags were returned.");
    for (const flag of data.flags) {
      const node = add(flags, "article");
      add(node, "h3", flag.name);
      if (!flag.allowed) add(node, "p", "Rewind will not change this", "muted");
      for (const environment of flag.environments) {
        labeled(node, environment.name, `default ${flagValue(environment.enabled)}`);
        for (const override of environment.overrides) {
          add(node, "p", `${override.kind} ${override.name}: ${flagValue(override.enabled)}`, "muted");
        }
      }
    }
  } catch (error) {
    flags.textContent = error.message;
  }
}

function connectLive() {
  element("mode").textContent = "Local live run · Flagsmith writes require approval.";
  element("request-form").hidden = false;
  element("inventory").hidden = false;
  const stream = new EventSource("api/events");
  stream.onmessage = (message) => show(JSON.parse(message.data));
  stream.onopen = () => { if (!running) element("run").disabled = false; };
  stream.onerror = () => {
    element("run").disabled = true;
    element("notice").textContent = "Disconnected. Pending approvals will be declined.";
  };
  element("request-form").addEventListener("submit", async (submit) => {
    submit.preventDefault();
    try {
      await post("api/run", {text: element("request").value});
    } catch (error) {
      element("notice").textContent = error.message;
    }
  });
  element("stop").title = "Decline pending and future approvals. Approved changes may finish.";
  element("stop").addEventListener("click", async () => {
    try { await post("api/stop", {action: "stop"}); }
    catch (error) { element("notice").textContent = error.message; }
  });
  window.addEventListener("pagehide", () => {
    if (running) navigator.sendBeacon("api/stop", new Blob(['{"action":"stop"}'], {type: "application/json"}));
    stream.close();
  });
  inventory();
}

function connectReplay() {
  element("mode").textContent = "Replay of a captured run: nothing is executed";
  element("replay-form").hidden = false;
  element("case").value = document.body.dataset.case;
  element("stop").addEventListener("click", () => {
    replayStopped = true;
    replayDecision?.(false);
  });
  element("replay-form").addEventListener("submit", async (submit) => {
    submit.preventDefault();
    if (running) return;
    replayStopped = false;
    const events = globalThis.rewindReplays[element("case").value];
    for (const event of events) {
      if (replayStopped) break;
      show(event);
      if (event.type === "gate") {
        const approved = await new Promise((resolve) => { replayDecision = resolve; });
        replayDecision = undefined;
        resolveGate(event.data.id, approved);
        if (!approved) replayStopped = true;
      } else if (event.type === "progress") {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    }
    if (replayStopped) show({type: "final", data: {verdict: "Replay stopped", text: "Nothing was executed."}});
  });
}

if (live) connectLive();
else connectReplay();
