"use strict";

const element = (id) => document.getElementById(id);
const chat = element("chat");
const live = document.body.dataset.mode === "live";
const choices = new Map();

let running = false;
let replayStopped = false;
let replayDecision;
let runStartMs = 0;
let doneAloneCount = 0;
let approvalsAskedCount = 0;
let timelineList = null;
let activeTimelineItem = null;
let lastPromptText = "";

function add(parent, tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined && value !== null) node.textContent = value;
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function card(title, className = "") {
  const node = add(chat, "article", null, `card ${className}`.trim());
  add(node, "h2", title);
  return node;
}

function pill(parent, text, variant = "") {
  return add(parent, "span", text, `pill ${variant}`.trim());
}

function setEnvironmentBadge(envName) {
  const badge = element("env-badge");
  if (!badge) return;
  const isTarget = envName && (envName.includes("target") || envName.includes("staging"));
  badge.textContent = isTarget ? `${envName} (staging)` : (envName || "rehearsal");
  badge.className = `pill mono ${isTarget ? "pill-env-target" : "pill-env-rehearsal"}`;
}

function updateCounters() {
  const alone = element("counter-alone");
  const approvals = element("counter-approvals");
  if (alone) alone.textContent = `${doneAloneCount} done alone`;
  if (approvals) approvals.textContent = `${approvalsAskedCount} ${approvalsAskedCount === 1 ? "approval" : "approvals"} asked`;
}

function finishActiveTimelineItem() {
  if (!activeTimelineItem) return;
  activeTimelineItem.classList.remove("is-active");
  const sp = activeTimelineItem.querySelector(".spinner");
  if (sp) sp.remove();
  activeTimelineItem = null;
}

function ensureTimeline() {
  if (timelineList && timelineList.isConnected) return timelineList;
  const box = add(chat, "section", null, "card");
  box.setAttribute("aria-label", "Agent timeline");
  add(box, "h2", "Timeline");
  timelineList = add(box, "ol", null, "timeline");
  return timelineList;
}

function appendTimelineStep(text, explicitElapsed) {
  finishActiveTimelineItem();
  const list = ensureTimeline();
  const li = add(list, "li", null, "timeline-item is-active");
  const main = add(li, "span", null, "timeline-main");
  add(main, "span", null, "spinner").setAttribute("aria-hidden", "true");
  add(main, "span", text);
  const secs = typeof explicitElapsed === "number"
    ? explicitElapsed.toFixed(1)
    : ((performance.now() - runStartMs) / 1000).toFixed(1);
  add(li, "span", `+${secs}s`, "timeline-elapsed");
  activeTimelineItem = li;
}

function renderRequestBubble(text) {
  if (!text) return;
  const bubble = add(chat, "article", null, "msg-user");
  add(bubble, "div", "You", "msg-user-label");
  add(bubble, "p", text, "msg-user-text");
}

function renderCustomerScreens(parent, title, rows, mode) {
  if (!rows || !rows.length) return;
  const panel = add(parent, "section", null, "customer-panel");
  add(panel, "h3", title);
  const screens = add(panel, "div", null, "customer-screens");
  for (const row of rows) {
    const screen = add(screens, "article", null, "customer-screen");
    const bar = add(screen, "div", null, "customer-screen-bar");
    const who = add(bar, "span", row.user, "mono");
    if (row.is_vip === true) pill(bar, "VIP", "pill-warn");
    else if (row.cart_id) pill(bar, row.cart_id, "mono");

    const body = add(screen, "div", null, "customer-screen-body");
    const chips = add(body, "div", null, "user-chip-list");
    if (row.http_status !== null && row.http_status !== undefined) {
      const httpOk = row.http_status >= 200 && row.http_status < 400;
      pill(chips, `HTTP ${row.http_status}`, `mono ${httpOk ? "pill-ok" : "pill-err"}`);
    }
    if (row.executed_version) {
      pill(chips, row.executed_version, `mono ${row.version_ok === false ? "pill-err" : "pill-ok"}`);
    }

    if (row.error) {
      add(body, "div", row.error, "shop-error-banner");
    } else if (mode === "before" && row.version_ok === false) {
      add(body, "div", `Ran ${row.executed_version} instead of rollback target`, "shop-warn-banner");
    } else if (mode === "after" && row.order_placed === true) {
      const parts = ["Order placed"];
      if (row.cart_intact === true) parts.push("Cart intact");
      if (row.totals) parts.push(row.totals);
      add(body, "div", parts.join(" · "), "shop-ok-banner");
    } else if (mode === "before" && row.order_placed === true && row.totals) {
      add(body, "div", `Order placed · ${row.totals}`, "shop-ok-banner");
    }

    if (row.cart_id && row.is_vip === true) {
      add(body, "div", `Cart: ${row.cart_id}`, "mono muted");
    }
  }
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
      if (index === 0) add(item, "th", value, "mono").scope = "row";
      else {
        const textVal = value ?? "Not recorded";
        const isChanged = String(textVal).includes("(changed)");
        add(item, "td", textVal, isChanged ? "changed" : "");
      }
    });
  }
}

const flagValue = (value) => value === true ? "ON" : value === false ? "OFF" : "Not recorded";

function labeled(node, label, text, valueClass = "") {
  const line = add(node, "p");
  add(line, "strong", `${label}: `);
  add(line, "span", text ?? "Not recorded", valueClass);
}

function setRunning(value) {
  running = value;
  const runBtn = element("run");
  const playBtn = element("play");
  if (runBtn) {
    runBtn.disabled = value;
    runBtn.dataset.loading = value ? "true" : "false";
  }
  if (playBtn) {
    playBtn.disabled = value;
    playBtn.dataset.loading = value ? "true" : "false";
  }
  if (element("request")) element("request").disabled = value;
  if (element("case")) element("case").disabled = value;
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
  gate.actions.classList.remove("is-pending");
  gate.actions.hidden = true;
  gate.note.textContent = approved ? "Approved by human" : "Declined by human";
  gate.note.className = `pill ${approved ? "pill-ok" : "pill-err"}`;
  choices.delete(id);
}

function renderInventory(flagsList) {
  const flagsEl = element("flags");
  element("inventory").hidden = false;
  flagsEl.replaceChildren();
  if (!flagsList || !flagsList.length) {
    add(flagsEl, "p", "No flags were returned.", "muted");
    return;
  }
  for (const flag of flagsList) {
    const item = add(flagsEl, "article", null, "flag-item");
    const head = add(item, "div", null, "flag-head");
    add(head, "span", flag.name, "flag-name");
    const totalOverrides = (flag.environments || []).reduce((acc, env) => acc + (env.overrides?.length || 0), 0);
    pill(head, `${totalOverrides} ${totalOverrides === 1 ? "override" : "overrides"}`);

    const envBox = add(item, "div", null, "flag-envs");
    for (const environment of (flag.environments || [])) {
      const row = add(envBox, "div", null, "flag-env-row");
      add(row, "span", environment.name, "mono muted");
      const val = flagValue(environment.enabled);
      pill(row, val, environment.enabled === true ? "pill-ok" : environment.enabled === false ? "pill-err" : "");
      for (const ov of (environment.overrides || [])) {
        add(envBox, "div", `${ov.kind} ${ov.name}: ${flagValue(ov.enabled)}`, "mono muted");
      }
    }
    if (!flag.allowed) {
      add(item, "p", "Rewind will not change this", "flag-note");
    }
  }
}

function gateCard(data) {
  finishActiveTimelineItem();
  if (data.gate === 2 || (data.environment && data.environment.includes("target"))) {
    setEnvironmentBadge(data.environment || "demo-target");
  }
  approvalsAskedCount += 1;
  updateCounters();

  const node = card(data.gate === 1 ? "Gate 1 · Approve rehearsal" : "Gate 2 · Approve staging", "gate");
  add(node, "div", "Needs your approval", "gate-eyebrow");
  labeled(node, "Environment", data.environment, "mono");

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
    for (const write of data.writes) add(list, "li", write.stage ? `${write.stage}: ${write.text}` : write.text, "mono");
  }
  if (data.fixture_writes?.length) {
    add(node, "h3", "Fixture database changes");
    const fixture = add(node, "ul");
    for (const write of data.fixture_writes) add(fixture, "li", write);
  }
  if (data.snapshot_hash) labeled(node, "Starting snapshot", data.snapshot_hash, "mono");
  if (data.starting_flags?.length) {
    const starting = add(node, "details");
    add(starting, "summary", "Starting staging flag state");
    table(starting, "Staging flag settings at approval time", ["Flag", "Default", "Overrides"],
      data.starting_flags.map((flag) => [flag.flag, flagValue(flag.enabled),
        flag.overrides.map((item) => `${item.kind} ${item.name}: ${flagValue(item.enabled)}`).join(", ") || "None"]));
  }
  if (data.summary) add(node, "p", data.summary, "plan");

  const actions = add(node, "div", null, "gate-actions is-pending");
  const approve = add(actions, "button", "Approve", "btn-primary");
  const decline = add(actions, "button", "Decline");
  const note = add(node, "p", null, "muted");
  note.role = "status";
  choices.set(data.id, {actions, note});

  async function choose(decision) {
    approve.disabled = true;
    decline.disabled = true;
    approve.dataset.loading = "true";
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
      approve.dataset.loading = "false";
    }
  }
  approve.addEventListener("click", () => choose("approve"));
  decline.addEventListener("click", () => choose("decline"));
  node.tabIndex = -1;
  approve.focus();
}

function show(event) {
  const data = event.data || {};
  switch (event.type) {
    case "started": {
      chat.replaceChildren();
      choices.clear();
      timelineList = null;
      activeTimelineItem = null;
      runStartMs = performance.now();
      doneAloneCount = 0;
      approvalsAskedCount = 0;
      setEnvironmentBadge(data.rehearsal || "rehearsal");
      updateCounters();
      element("notice").textContent = "";
      element("progress").textContent = live ? "Starting rehearsal…" : "Starting replay…";
      setRunning(true);
      const userPrompt = live ? (element("request")?.value || lastPromptText) : (data.prompt || "");
      renderRequestBubble(userPrompt);
      break;
    }
    case "inventory":
      renderInventory(data.flags || []);
      break;
    case "interpretation": {
      doneAloneCount += 1;
      updateCounters();
      const node = card("Interpreted request");
      labeled(node, "Request", data.text);
      add(node, "p", data.interpretation);
      break;
    }
    case "progress": {
      doneAloneCount += 1;
      updateCounters();
      if (data.environment && (data.environment.includes("target") || data.environment.includes("staging"))) {
        setEnvironmentBadge(data.environment);
      } else if (data.text && data.text.startsWith("Target:")) {
        setEnvironmentBadge("demo-target");
      }
      element("progress").textContent = data.text;
      appendTimelineStep(data.text, data.elapsed_s);
      break;
    }
    case "failure": {
      finishActiveTimelineItem();
      doneAloneCount += 1;
      updateCounters();
      const node = card("Failure evidence");
      add(node, "p", data.problem);
      const split = add(node, "div", null, (data.customers && data.customers.length) ? "failure-split" : "");
      const gridBox = add(split, "div");
      add(gridBox, "h3", "Per-user checkouts");
      const grid = add(gridBox, "div", null, "user-grid");
      for (const row of (data.users || [])) {
        const tile = add(grid, "div", null, "user-chip-card");
        add(tile, "strong", row.user, "mono");
        const chips = add(tile, "div", null, "user-chip-list");
        const httpOk = row.http_status >= 200 && row.http_status < 400;
        pill(chips, `HTTP ${row.http_status ?? "?"}`, `mono ${httpOk ? "pill-ok" : "pill-err"}`);
        const verOk = row.version_ok !== false;
        pill(chips, row.version ?? "unknown", `mono ${verOk ? "pill-ok" : "pill-err"}`);
      }
      if (data.customers && data.customers.length) {
        renderCustomerScreens(split, "Customer view (before fix)", data.customers, "before");
      }
      break;
    }
    case "attempt": {
      finishActiveTimelineItem();
      doneAloneCount += 1;
      updateCounters();
      const node = card(`Attempt ${data.number} · ${data.label}`);
      add(node, "p", data.diagnosis);
      if (data.fix) labeled(node, "Proposed fix", data.fix, "mono");
      break;
    }
    case "gate":
      gateCard(data);
      break;
    case "decision":
      resolveGate(data.id, data.approved);
      break;
    case "final": {
      finishActiveTimelineItem();
      if (data.ledger_counts) {
        doneAloneCount = Number(data.ledger_counts.autonomous_steps ?? doneAloneCount);
        approvalsAskedCount = Number(data.ledger_counts.approvals_asked ?? approvalsAskedCount);
        updateCounters();
      }
      const statusClass = data.status === "verified" ? "is-verified" : data.status === "refused" ? "is-refused" : "";
      const node = card(data.verdict, `final ${statusClass}`.trim());
      add(node, "p", data.text);
      if (data.error) add(node, "p", data.error, "shop-error-banner");
      if (data.ledger) labeled(node, "Autonomy", data.ledger);
      if (data.breakdown) add(node, "p", data.breakdown, "muted");
      const afterUsers = data.customer_views?.after?.users || [];
      if (afterUsers.length) {
        const afterLabel = data.customer_views?.after?.label
          ? `Customer view (${data.customer_views.after.label})`
          : "Customer view (after run)";
        renderCustomerScreens(node, afterLabel, afterUsers, "after");
      }
      const footerActions = add(node, "div", null, "actions");
      if (data.report) {
        const link = add(footerActions, "a", "Open evidence report", "btn-link");
        link.href = data.report;
        link.target = "_blank";
        link.rel = "noopener";
      }
      const again = add(footerActions, "button", "Run again", "btn-primary");
      again.type = "button";
      again.addEventListener("click", () => {
        if (live) {
          const req = element("request");
          if (req) { req.focus(); req.select(); }
        } else {
          element("replay-form")?.requestSubmit();
        }
      });
      setRunning(false);
      node.tabIndex = -1;
      node.focus();
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
    renderInventory(data.flags || []);
  } catch (error) {
    flags.textContent = error.message;
  }
}

function connectLive() {
  element("mode").textContent = "Local live run · Flagsmith writes require your approval.";
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
    lastPromptText = element("request").value;
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
  element("mode").textContent = "Replay of a captured run: nothing is executed.";
  element("replay-form").hidden = false;
  const caseSelect = element("case");
  caseSelect.value = document.body.dataset.case || "a";
  const initialEvents = globalThis.rewindReplays?.[caseSelect.value] || [];
  const invEvt = initialEvents.find((evt) => evt.type === "inventory");
  if (invEvt) renderInventory(invEvt.data.flags);

  caseSelect.addEventListener("change", () => {
    const selected = globalThis.rewindReplays?.[caseSelect.value] || [];
    const inv = selected.find((evt) => evt.type === "inventory");
    if (inv) renderInventory(inv.data.flags);
  });

  element("stop").addEventListener("click", () => {
    replayStopped = true;
    replayDecision?.(false);
  });
  element("replay-form").addEventListener("submit", async (submit) => {
    submit.preventDefault();
    if (running) return;
    replayStopped = false;
    const events = globalThis.rewindReplays[caseSelect.value];
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
    if (replayStopped) show({type: "final", data: {status: "stopped", verdict: "Replay stopped", text: "Nothing was executed."}});
  });
}

if (live) connectLive();
else connectReplay();
