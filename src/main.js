import "./style.css";

const $ = (selector) => document.querySelector(selector);
const endpoint = "/api/invoice";
const labels = {
  "01-readable-match.pdf": "01 · Readable invoice",
  "02-scanned-mistyped-po.pdf": "02 · Scanned PO typo",
  "03-split-with-price-variance.pdf": "03 · Split invoice + variance",
  "04-quantity-exceeds-balance.pdf": "04 · Quantity overage",
};
const stages = [
  ["extract", "Read invoice PDF", "Extract text and line items, using OCR for scans."],
  ["validate", "Validate invoice fields", "Check key fields and reconcile totals."],
  ["match", "Find the purchase order", "Start with the exact reference, then examine item and reference evidence."],
  ["reconcile", "Check line items and prior approvals", "Verify duplicates, quantities, unit prices, and tolerance."],
  ["decision", "Record the decision", "Save the result and update balances for approvals."],
  ["advice", "Generate AI guidance", "Suggest what the analyst should verify."],
];
const state = { file: null, busy: false, ready: false, runs: [], demos: [], step: -1, detail: "", finished: false };

function escape(value) {
  return String(value ?? "—").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function statusClass(status) {
  return { Approved: "approved", Duplicate: "duplicate", "Needs review": "review" }[status] ?? "review";
}

function badge(status) {
  return `<span class="badge ${statusClass(status)}">${escape(status)}</span>`;
}

function quantity(value) {
  return value === "" || value == null ? "—" : escape(value);
}

function money(value, currency = "") {
  if (value === "" || value == null) return "—";
  return escape([currency, Number(value).toFixed(2)].filter(Boolean).join(" "));
}

function table(headings, rows) {
  return `<div class="table-wrap"><table><thead><tr>${headings.map((label) => `<th scope="col">${escape(label)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((row) => `<tr>${row.map((item) => `<td>${item}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function showView(view) {
  for (const key of ["run", "history"]) {
    const active = key === view;
    $(`#${key}-view`).hidden = !active;
    const nav = $(`#nav-${key}`);
    nav.classList.toggle("active", active);
    if (active) nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
  }
}

function drawSteps() {
  const stageCount = state.step === 5 ? 6 : 5;
  $("#steps").innerHTML = stages.slice(0, stageCount).map(([, title, detail], index) => {
    const status = index < state.step || state.finished ? "done" : index === state.step ? "active" : "idle";
    return `<li class="step" data-state="${status}"${status === "active" ? ' aria-current="step"' : ""}>
      <span class="step-icon" aria-hidden="true">${status === "done" ? "✓" : index + 1}</span>
      <div><strong>${title}</strong><p>${escape(status === "active" ? state.detail : detail)}</p></div>
    </li>`;
  }).join("");
}

function setStage(id, detail) {
  state.step = stages.findIndex(([key]) => key === id);
  state.detail = detail;
  $("#pipeline-status").textContent = "In progress";
  drawSteps();
}

function renderControls() {
  $("#run-button").disabled = !state.file || state.busy || !state.ready;
  $("#file-input").disabled = state.busy;
  $("#ai-toggle").disabled = state.busy;
  for (const button of $("#sample-list").querySelectorAll("button")) button.disabled = state.busy || !state.ready;
}

function showError(message) {
  $("#run-error").textContent = message;
  $("#run-error").hidden = !message;
}

function selectFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    showError("Choose an invoice with a .pdf extension.");
    return;
  }
  state.file = file;
  $("#file-label").textContent = file.name;
  $("#file-help").textContent = `${(file.size / 1024).toFixed(0)} KB · Ready to review`;
  showError("");
  renderControls();
}

function renderResult(container, outcome, runId) {
  const metrics = [
    ["Vendor", escape(outcome.vendor || "—")],
    ["Invoice", escape(outcome.invoice || "—")],
    ["Purchase order", escape(outcome.po || "Unresolved")],
    ["Invoice total", money(outcome.total, outcome.currency)],
  ];
  const header = `<div class="card decision ${statusClass(outcome.status)}">
    <div class="card-header"><h2>Decision · Run #${escape(runId)}</h2>${badge(outcome.status)}</div>
    <div class="metrics">${metrics.map(([name, value]) => `<div class="metric"><span>${name}</span><strong>${value}</strong></div>`).join("")}</div>
    <p class="meta"><strong>Match method</strong> · ${escape(outcome.match_method)}
      &nbsp; <strong>Invoice reference</strong> · ${escape(outcome.reference || "Missing")}
      &nbsp; <strong>Invoice date</strong> · ${escape(outcome.date || "Missing")}</p>
    <p class="meta"><strong>Extraction</strong> · ${(outcome.sources || []).map(escape).join(" · ")}</p>
    <ul class="reasons">${outcome.reasons.map((reason) => `<li>${escape(reason)}</li>`).join("")}</ul>
  </div>`;

  const candidates = outcome.candidates.length ? `<section class="card"><h3>Purchase order candidates</h3>${table(
    ["Purchase order", "Reference similarity", "Matching SKUs", "Matching descriptions", "Passing lines"],
    outcome.candidates.map((candidate) => [
      escape(candidate.PO),
      candidate["Reference similarity"] == null ? "—" : `${escape(candidate["Reference similarity"])}%`,
      escape(candidate["Matching SKUs"]), escape(candidate["Matching descriptions"]), escape(candidate["Lines passing every check"]),
    ]),
  )}</section>` : "";

  const lines = outcome.lines.length ? `<section class="card"><div class="card-header"><h2>Line-item reconciliation</h2></div>${table(
    ["Invoice item", "PO SKU", "Invoiced", "Prior approvals", "Remaining before", "If approved", "Invoice unit price", "PO unit price", "Check"],
    outcome.lines.map((line) => [
      escape(line["Invoice item"]), escape(line["PO SKU"] || "—"), quantity(line["Invoice qty"]), quantity(line["Previously approved"]),
      quantity(line["PO qty remaining"]), quantity(line["If approved"]), money(line["Invoice unit price"]), money(line["PO unit price"]),
      escape(line.Check),
    ]),
  )}${outcome.variance ? `<p class="meta"><strong>Absolute PO price variance</strong> ·
    ${money(outcome.variance.current, outcome.currency)} this invoice ·
    ${money(outcome.variance.used, outcome.currency)} previously used · ${money(outcome.variance.cap, outcome.currency)} limit</p>` : ""}</section>` : "";

  const guidance = outcome.review_advice;
  const advice = guidance ? `<section class="card"><h3>AI review guidance</h3>
      ${guidance.suggested_po ? `<p class="meta"><strong>Suggested PO</strong> · ${escape(guidance.suggested_po)}</p>` : ""}
      <p class="meta">${escape(guidance.summary)}</p>
      <ol class="reasons">${guidance.next_steps.map((step) => `<li>${escape(step)}</li>`).join("")}</ol></section>`
    : outcome.advice_unavailable ? '<p class="empty">AI guidance was unavailable. The decision and evidence were saved.</p>' : "";
  container.innerHTML = header + candidates + lines + advice;
  container.hidden = false;
}

function renderHistory() {
  $("#history-count").textContent = state.runs.length;
  $("#history-list").innerHTML = state.runs.length ? `<section class="card">${table(
    ["Run", "Invoice file", "Purchase order", "Decision", "Recorded (UTC)"],
    state.runs.map(({ run_id: id, filename, outcome, created }) => [
      `#${escape(id)}`, `<button class="link-button" data-run="${escape(id)}" type="button">${escape(filename)}</button>`,
      escape(outcome.po || "—"), badge(outcome.status), escape(String(created).slice(0, 19).replace("T", " ")),
    ]),
  )}</section>` : '<section class="card"><p class="empty">No invoices have been reviewed in this workspace yet.</p></section>';
}

async function errorMessage(response) {
  const body = await response.json().catch(() => null);
  return typeof body?.detail === "string" ? body.detail : `The request could not be completed (${response.status}).`;
}

async function refreshWorkspace() {
  const response = await fetch(endpoint);
  if (!response.ok) throw new Error(await errorMessage(response));
  const payload = await response.json();
  state.runs = payload.runs;
  state.demos = payload.demos;
  state.ready = true;
  const connection = $("#connection");
  connection.dataset.state = "ok";
  connection.innerHTML = "<i></i> Ledger connected";
  $("#ai-control").hidden = !payload.ai_enabled;
  $("#sample-list").innerHTML = state.demos.map((file) =>
    `<button class="sample" type="button" data-sample="${escape(file)}">${escape(labels[file] || file)}</button>`).join("");
  renderHistory();
  renderControls();
}

async function readEvents(response) {
  if (!response.ok) throw new Error(await errorMessage(response));
  if (!response.body) throw new Error("The server did not return a run.");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let pending = "";
  let outcome;
  function handle(line) {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.error) throw new Error(event.error);
    if (event.stage) setStage(event.stage, event.detail);
    if (event.outcome) outcome = event.outcome;
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      pending += value;
      const records = pending.split("\n");
      pending = records.pop();
      for (const record of records) handle(record);
    }
    handle(pending);
  } finally {
    await reader.cancel().catch(() => {});
  }
  if (!outcome) throw new Error("The run ended without a decision. Refresh Run history before retrying.");
  return outcome;
}

async function runInvoice(getFile) {
  if (state.busy || !state.ready) return;
  state.busy = true;
  state.finished = false;
  $("#result").hidden = true;
  showError("");
  renderControls();
  setStage("extract", "Opening the source PDF");
  try {
    const file = await getFile();
    selectFile(file);
    const { readPdf } = await import("./pdf.js");
    const pages = await readPdf(file, (detail) => setStage("extract", detail));
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name.slice(0, 150), pages, use_ai: $("#ai-toggle").checked }),
    });
    const outcome = await readEvents(response);
    state.finished = true;
    $("#pipeline-status").textContent = "Complete";
    drawSteps();
    renderResult($("#result"), outcome, outcome.run_id);
    await refreshWorkspace().catch(() => showError("The decision was recorded. Reload to refresh Run history."));
  } catch (error) {
    $("#pipeline-status").textContent = "Run interrupted";
    showError(error.message || "The invoice could not be processed.");
  } finally {
    state.busy = false;
    renderControls();
  }
}

$("#nav-run").addEventListener("click", () => showView("run"));
$("#nav-history").addEventListener("click", () => showView("history"));
$("#history-new").addEventListener("click", () => showView("run"));
$("#file-input").addEventListener("change", ({ target }) => selectFile(target.files[0]));
$("#run-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.file) runInvoice(() => state.file);
});

const zone = $("#dropzone");
zone.addEventListener("dragover", (event) => { event.preventDefault(); if (!state.busy) zone.classList.add("dragging"); });
zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
zone.addEventListener("drop", (event) => {
  event.preventDefault();
  zone.classList.remove("dragging");
  if (!state.busy) selectFile(event.dataTransfer.files[0]);
});

$("#sample-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-sample]");
  if (!button) return;
  const name = button.dataset.sample;
  runInvoice(async () => {
    const response = await fetch(`${endpoint}?demo=${encodeURIComponent(name)}`);
    if (!response.ok) throw new Error(await errorMessage(response));
    return new File([await response.blob()], name, { type: "application/pdf" });
  });
});

$("#history-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-run]");
  if (!button) return;
  const record = state.runs.find(({ run_id: id }) => String(id) === button.dataset.run);
  if (record) renderResult($("#history-result"), record.outcome, record.run_id);
});

drawSteps();
renderHistory();
refreshWorkspace().catch((error) => {
  $("#connection").dataset.state = "error";
  $("#connection").innerHTML = "<i></i> Ledger unavailable";
  showError(error.message);
});
