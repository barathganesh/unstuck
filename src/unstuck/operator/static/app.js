// Unstuck operator surface.
//
// No framework, no bundler, no remote asset, no inline handler. Every request
// carries the exact loopback Origin the local API requires, and every value
// rendered here comes from the API as text: this file never computes a figure
// and never substitutes zero for an absent measurement.
"use strict";

(function () {
  const ORIGIN = window.location.origin;
  const JSON_HEADERS = { "content-type": "application/json", origin: ORIGIN };

  const state = { runId: null, spine: [], selected: null };

  const byId = (id) => document.getElementById(id);
  const say = (text) => {
    byId("live-status").textContent = text;
  };

  async function readJson(path) {
    const response = await fetch(path, { headers: { origin: ORIGIN } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.detail || body.reason_code || "request refused");
    }
    return body;
  }

  async function writeJson(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const gate = body.gate ? ` ${body.gate}` : "";
      throw new Error(`${body.reason_code || "refused"}.${gate}`);
    }
    return body;
  }

  // ---- navigation -------------------------------------------------------
  function showView(name) {
    document.querySelectorAll(".view").forEach((section) => {
      section.hidden = section.id !== `view-${name}`;
    });
    document.querySelectorAll(".nav").forEach((button) => {
      if (button.dataset.view === name) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    });
    if (name === "benchmark") {
      loadBenchmark();
    }
  }

  document.querySelectorAll(".nav").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });

  // ---- run setup --------------------------------------------------------
  byId("run-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const start = byId("start");
    const error = byId("run-error");
    error.hidden = true;
    start.disabled = true;
    say("Starting the run.");
    try {
      const mode = document.querySelector('input[name="mode"]:checked').value;
      const view = await writeJson("/api/runs", {
        scenario: byId("scenario").value,
        idempotency_key: byId("idempotency").value,
        mode: mode,
      });
      state.runId = view.run_id;
      renderRun(view);
      showView("detail");
      // Move focus to the run heading without stealing it from a later action.
      byId("detail-heading").focus();
      say(`Run ${view.run_id} ${view.outcome_text}`);
    } catch (failure) {
      error.textContent = String(failure.message);
      error.hidden = false;
      say(`The run was not started. ${failure.message}`);
    } finally {
      start.disabled = false;
    }
  });

  // ---- run detail -------------------------------------------------------
  function renderRun(view) {
    const summary = byId("run-summary");
    summary.textContent = "";
    const rows = [
      ["Run", view.run_id],
      ["Scenario", view.scenario || "Not provided"],
      [
        "Fixture identity",
        view.fixture_identity
          ? view.fixture_identity.slice(0, 12)
          : "Not provided",
      ],
      ["Outcome", view.outcome_text],
      ["Reason", view.reason_code || "Not provided"],
      ["Mode", view.mode.text || view.mode.requested],
      ["Path", view.path.replace(/_/g, " ")],
      ["Selector calls", String(view.selector_calls)],
      ["Diagnostic reads", String(view.diagnostic_reads)],
      ["History", view.history_status],
      ["Verification", view.verification_status],
      ["Elapsed", view.duration.text],
      ["Cost", view.cost.text],
      ["Confidence", view.confidence.text],
      ["Data", "Synthetic"],
    ];
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      summary.append(dt, dd);
    });
    state.spine = view.spine;
    renderSpine(view.spine);
    renderExportChecklist(view);
  }

  function renderSpine(nodes) {
    const list = byId("spine");
    list.textContent = "";
    nodes.forEach((node) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "spine-node";
      button.dataset.state = node.state;
      button.dataset.nodeId = node.node_id;
      button.setAttribute("aria-pressed", "false");
      const label = document.createElement("span");
      label.textContent = `${node.order}. ${node.label}`;
      const meta = document.createElement("span");
      meta.className = "state";
      meta.textContent = node.present ? node.state : "skipped";
      button.append(label, meta);
      button.addEventListener("click", () => selectNode(node.node_id));
      button.addEventListener("keydown", moveWithinSpine);
      item.append(button);
      list.append(item);
    });
  }

  function moveWithinSpine(event) {
    const keys = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    const step = keys[event.key];
    if (!step) {
      return;
    }
    event.preventDefault();
    const buttons = Array.from(document.querySelectorAll(".spine-node"));
    const index = buttons.indexOf(event.currentTarget);
    const next = buttons[index + step];
    if (next) {
      next.focus();
    }
  }

  async function selectNode(nodeId) {
    state.selected = nodeId;
    document.querySelectorAll(".spine-node").forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        button.dataset.nodeId === nodeId ? "true" : "false"
      );
    });
    const node = state.spine.find((item) => item.node_id === nodeId);
    byId("out-evidence").textContent = node ? node.detail : "";
    try {
      const panel = await readJson(
        `/api/runs/${state.runId}/evidence/${nodeId}`
      );
      const evidence = JSON.stringify(panel.evidence, null, 2);
      byId("out-evidence").textContent =
        `${node ? node.detail + "\n\n" : ""}${evidence === "{}" ? "Not provided" : evidence}`;
      byId("out-raw").textContent = panel.raw_event
        ? JSON.stringify(panel.raw_event, null, 2)
        : "Not provided";
      byId("out-validation").textContent = JSON.stringify(
        panel.validation,
        null,
        2
      );
      say(`Showing evidence for ${nodeId}.`);
    } catch (failure) {
      byId("out-validation").textContent = String(failure.message);
    }
  }

  // ---- evidence tabs ----------------------------------------------------
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  function selectTab(tab) {
    tabs.forEach((item) => {
      const active = item === tab;
      item.setAttribute("aria-selected", active ? "true" : "false");
      item.tabIndex = active ? 0 : -1;
      byId(item.getAttribute("aria-controls")).hidden = !active;
    });
    tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) {
        return;
      }
      event.preventDefault();
      selectTab(tabs[(index + step + tabs.length) % tabs.length]);
    });
  });

  // ---- benchmark --------------------------------------------------------
  async function loadBenchmark() {
    try {
      const view = await readJson("/api/benchmark");
      byId("benchmark-notice").textContent = view.notice;
      const body = byId("benchmark-body");
      body.textContent = "";
      view.arms.forEach((arm) => {
        const row = document.createElement("tr");
        const name = document.createElement("th");
        name.scope = "row";
        name.textContent = arm.arm;
        row.append(name);
        [
          String(arm.episodes),
          arm.verified_success.text,
          arm.harmful_choices.text,
          arm.diagnostic_reads.text,
          arm.selector_latency_p50_ms.text,
          arm.selector_latency_p95_ms.text,
          arm.cost_usd.text,
        ].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        });
        body.append(row);
      });
      byId("benchmark-caption").textContent =
        `Arm comparison across ${view.episodes_total} recorded episode(s). ` +
        "Every figure is read from the evaluation export; none is computed here.";
      byId("retention-rule").textContent = view.retention_rule;
      byId("retention-state").textContent = view.retention_enabled
        ? `Decision: ${view.retention_decision}.`
        : "Retention is disabled: the required arms and denominators do not exist yet.";
    } catch (failure) {
      byId("benchmark-notice").textContent = String(failure.message);
    }
  }

  // ---- export -----------------------------------------------------------
  function renderExportChecklist(view) {
    const list = byId("export-checklist");
    list.textContent = "";
    [
      `Scenario ${view.scenario || "Not provided"}`,
      `Fixture identity ${
        view.fixture_identity ? view.fixture_identity.slice(0, 12) : "Not provided"
      }`,
      `Task hash ${view.task_hash.slice(0, 12)}`,
      `Query hash ${view.query_hash.slice(0, 12)}`,
      `First choice ${view.first_choice || "Not provided"}`,
      `Receipt references ${view.receipt_references.length}`,
      `Blocked tool ${view.blocked_tool_id || "Not provided"}`,
      `Repair attempts ${view.repair_attempts}`,
      `Verification ${view.verification_status}`,
      `Elapsed ${view.duration.text}`,
      `Cost ${view.cost.text}`,
      `Artifacts ${view.artifact_refs.length}`,
    ].forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      list.append(item);
    });
  }

  byId("download").addEventListener("click", async () => {
    if (!state.runId) {
      say("Start a run before exporting its evidence.");
      return;
    }
    const format = byId("export-format").value;
    const response = await fetch("/api/exports", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ run_id: state.runId, format: format }),
    });
    if (!response.ok) {
      say("The export was refused.");
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `unstuck-${state.runId}.${format}`;
    link.click();
    URL.revokeObjectURL(url);
    say(`Downloaded the ${format.toUpperCase()} export.`);
  });

  // ---- boot -------------------------------------------------------------
  readJson("/api/health")
    .then((health) => {
      byId("mode-badge").textContent =
        `${health.mode} mode, ${health.provider} provider`;
      byId("setup-boundary").textContent =
        "Live mode is blocked until its preflight evidence exists. " +
        "Nothing falls back to a mock silently.";
    })
    .catch(() => {
      byId("setup-boundary").textContent =
        "The local API did not answer. Start it with: unstuck serve";
    });
})();
