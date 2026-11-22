"use strict";

const $ = (sel) => document.querySelector(sel);
const STATE_LABEL = { running: "Running", queued: "Queued", done: "Done", error: "Error", cancelled: "Cancelled" };
const MODEL_LABEL = { loading: "loading model", loaded: "model loaded", released: "model released", "per stage": "per stage" };
const LOCAL_ONLY_KINDS = ["shell", "builtin"];
let services = {};
let jobsOffset = 0;
let openJobId = null;
let lastRunningKey = "";
let activeTab = "jobs";
let me = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(t) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  const today = new Date().toDateString() === d.toDateString();
  const hm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return today ? hm : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) + " " + hm;
}
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.round(s);
  if (s < 60) return s + " s";
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + " min " + String(r).padStart(2, "0") + " s";
  return Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0") + " min";
}
function fmtBytes(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}
function svcName(id) { return services[id]?.name || id; }
function outUrl(job, o, download) { return `/api/v1/jobs/${job.id}/outputs/${encodeURIComponent(o.name)}${download ? "?download=true" : ""}`; }
const JSON_HEADERS = { "Content-Type": "application/json" };

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401) { showLogin(); throw new Error("unauthorized"); }
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const d = body?.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : (d?.reason || d?.error || res.statusText);
    const err = new Error(msg);
    err.status = res.status; err.body = body; err.retryAfter = res.headers.get("retry-after");
    throw err;
  }
  return body;
}

// ---------------------------------------------------------------- login

function showLogin() { $("#app").hidden = true; $("#login").hidden = false; $("#login-key").focus(); }
async function start() {
  try {
    me = await api("/api/v1/me");
    editorMeta = null;
    $("#who").textContent = me.local ? "local access (no key)" : `${me.client}${me.admin ? " · admin" : ""}`;
    $("#logout").hidden = !!me.local;
    $("#reload").hidden = !me.admin;
    $("#new-service").hidden = !me.admin;
    $("#login").hidden = true; $("#app").hidden = false;
    await loadServices();
    await loadJobs(true);
    pollStatus();
  } catch (e) { if (e.message !== "unauthorized") console.error(e); }
}
$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").hidden = true;
  try {
    await api("/api/v1/login", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ key: $("#login-key").value }) });
    $("#login-key").value = "";
    start();
  } catch (e) { $("#login-error").textContent = "This key is not accepted."; $("#login-error").hidden = false; }
});
$("#logout").addEventListener("click", async () => { await fetch("/api/v1/logout", { method: "POST" }); showLogin(); });

// ---------------------------------------------------------------- status and running jobs

let statusTimer = null;
async function pollStatus() {
  clearTimeout(statusTimer);
  try {
    const s = await api("/api/v1/status");
    renderStatus(s);
    const key = s.running.map((r) => r.id + r.state).join(",");
    if (key !== lastRunningKey) { lastRunningKey = key; loadJobs(true); }
  } catch (e) { if (e.message === "unauthorized") return; }
  statusTimer = setTimeout(pollStatus, 2000);
}

let lastStatus = null;
function holderNames(r) {
  return [...r.holders.map((h) => svcName(h.service) + (h.calls > 1 ? ` (${h.calls} calls)` : "")),
    ...(r.external || []).map((e) => `${e} outside openField`)].join(", ");
}
function renderStatus(s) {
  lastStatus = s;
  const pill = $("#gpu-pill");
  const gpu = (s.resources || {}).gpu || Object.values(s.resources || {})[0];
  if (!gpu || gpu.used === 0) {
    pill.className = "pill free";
    pill.textContent = gpu && gpu.slots > 1 ? `GPU free · ${gpu.slots} slots` : "GPU free";
  } else if (gpu.free === 0) {
    pill.className = "pill busy";
    pill.textContent = `GPU ${gpu.slots > 1 ? `${gpu.used}/${gpu.slots}` : "busy"} — ${holderNames(gpu)}`;
  } else {
    pill.className = "pill partial";
    pill.textContent = `GPU ${gpu.used}/${gpu.slots} — ${holderNames(gpu)}`;
  }
  $("#mem").textContent = `${s.memory_available_gb} GB free`;
  if (activeTab === "system") renderGauges();
  const box = $("#running");
  if (!s.running.length) { box.innerHTML = ""; return; }
  box.innerHTML = s.running.map((j) => {
    const pct = Math.round((j.progress || 0) * 100);
    const ms = j.model_state ? `<span class="chip ${esc(j.model_state)}">${esc(MODEL_LABEL[j.model_state] || j.model_state)}</span>` : "";
    return `<div class="card run">
      <div class="run-head"><span class="svc">${esc(svcName(j.service))}</span><span class="muted">${esc(j.stage || "")}</span>
        <span class="right">${ms}<button class="danger" data-cancel="${esc(j.id)}">Cancel</button>
        <button class="secondary" data-open="${esc(j.id)}">Details</button></span></div>
      <div class="bar"><div style="width:${pct}%"></div></div>
      <div class="run-meta"><span>${pct}%</span><span>ETA ${fmtDur(j.eta_s)}</span>
        <span>elapsed ${fmtDur(Date.now() / 1000 - j.started_at)}</span>${j.model ? `<span>${esc(j.model)}</span>` : ""}
        <span>${esc(j.client || "")}</span></div>
      ${j.prompt ? `<div class="muted" style="margin-top:6px">${esc(j.prompt)}</div>` : ""}
    </div>`;
  }).join("");
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-cancel],[data-open],[data-delete]");
  if (!t) return;
  if (t.dataset.open) openJob(t.dataset.open);
  if (t.dataset.cancel) {
    t.disabled = true;
    try { await api(`/api/v1/jobs/${t.dataset.cancel}/cancel`, { method: "POST" }); } catch (e) { alertBox(e.message); }
    pollStatus();
  }
  if (t.dataset.delete) {
    if (t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Confirm delete"; return; }
    try { await api(`/api/v1/jobs/${t.dataset.delete}`, { method: "DELETE" }); closeDrawer(); loadJobs(true); } catch (e) { alertBox(e.message); }
  }
});
function alertBox(msg) { const n = $("#n-result"); if (n) n.innerHTML = `<div class="notice err">${esc(msg)}</div>`; }

// ---------------------------------------------------------------- jobs list

function thumb(j) {
  const o = (j.outputs || [])[0];
  if (!o || j.outputs_purged) return `<div class="thumb">${j.outputs_purged ? "removed" : esc(STATE_LABEL[j.state] || "")}</div>`;
  if ((o.media_type || "").startsWith("image/")) return `<img class="thumb" loading="lazy" src="${outUrl(j, o)}" alt="">`;
  if ((o.media_type || "").startsWith("video/")) return `<video class="thumb" preload="metadata" muted src="${outUrl(j, o)}#t=0.5"></video>`;
  return `<div class="thumb">file</div>`;
}

async function loadJobs(reset) {
  if (reset) jobsOffset = 0;
  const q = new URLSearchParams({ limit: "40", offset: String(jobsOffset) });
  if ($("#f-service").value) q.set("service", $("#f-service").value);
  if ($("#f-state").value) q.set("state", $("#f-state").value);
  let data;
  try { data = await api(`/api/v1/jobs?${q}`); } catch { return; }
  const html = data.jobs.map((j) => {
    const sub = j.prompt || (j.state === "cancelled" ? "cancelled" : j.error ? "Error: " + j.error
      : (j.outputs || []).map((o) => `${o.name} ${o.width ? o.width + "×" + o.height : ""}`).join(", "));
    return `<div class="job" data-open="${esc(j.id)}">${thumb(j)}
      <div><div class="title">${esc(svcName(j.service))}</div>
        <div class="sub">${esc(fmtTime(j.created_at))} · ${esc(fmtDur(j.elapsed_s ?? (j.state === "running" ? Date.now() / 1000 - j.started_at : null)))} · ${esc(j.client || "")} — ${esc(sub || "")}</div></div>
      <span class="state ${esc(j.state)}">${esc(STATE_LABEL[j.state] || j.state)}</span></div>`;
  }).join("");
  if (reset) $("#jobs").innerHTML = html || `<p class="muted">No jobs yet.</p>`; else $("#jobs").insertAdjacentHTML("beforeend", html);
  jobsOffset += data.jobs.length;
  $("#jobs-count").textContent = `${data.total} job${data.total === 1 ? "" : "s"}`;
  $("#more").hidden = jobsOffset >= data.total;
}
$("#more").addEventListener("click", () => loadJobs(false));
$("#f-service").addEventListener("change", () => loadJobs(true));
$("#f-state").addEventListener("change", () => loadJobs(true));

// ---------------------------------------------------------------- job detail

let drawerTimer = null;
function openDrawer(title) {
  openJobId = null; connectState = null; editorState = null; clearTimeout(drawerTimer);
  $("#d-title").textContent = title;
  $("#drawer").hidden = false;
}
async function openJob(id) {
  let j;
  try { j = await api(`/api/v1/jobs/${id}`); } catch (e) { return; }
  if (openJobId !== id) openDrawer("");
  openJobId = id;
  $("#d-title").textContent = `${svcName(j.service)} — ${STATE_LABEL[j.state] || j.state}`;
  const outputs = (j.outputs || []).map((o) => {
    const media = (o.media_type || "");
    const view = j.outputs_purged ? `<p class="muted">File removed by retention.</p>`
      : media.startsWith("image/") ? `<img src="${outUrl(j, o)}" alt="${esc(o.name)}">`
      : media.startsWith("video/") ? `<video src="${outUrl(j, o)}" controls preload="metadata"></video>` : "";
    const facts = [o.width ? `${o.width}×${o.height}` : "", o.duration_s ? `${o.duration_s.toFixed(2)} s` : "",
      o.frames ? `${o.frames} frames` : "", o.fps ? `${o.fps} fps` : "", o.audio ? "with audio" : "", fmtBytes(o.size_bytes)].filter(Boolean).join(" · ");
    return `<div>${view}<div class="actions"><span class="muted">${esc(o.name)} — ${esc(facts)}</span>
      ${j.outputs_purged ? "" : `<a href="${outUrl(j, o, true)}"><button class="secondary">Download</button></a>`}</div></div>`;
  }).join("");
  const inputs = Object.entries(j.inputs || {}).map(([param, items]) => items.map((it, i) =>
    it.kind === "image" && !j.outputs_purged ? `<img title="${esc(param)}" src="/api/v1/jobs/${j.id}/inputs/${encodeURIComponent(param)}/${i}" alt="">`
      : `<span class="chip">${esc(param)}: ${esc(it.filename || it.file)} (${esc(it.kind)})</span>`).join("")).join("");
  const params = Object.entries(j.params || {}).filter(([, v]) => v !== null && v !== "")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("");
  const pct = Math.round((j.progress || 0) * 100);
  $("#d-body").innerHTML = `
    ${j.state === "running" ? `<div class="bar"><div style="width:${pct}%"></div></div>
      <div class="run-meta"><span>${pct}%</span><span>${esc(j.stage || "")}</span><span>ETA ${fmtDur(j.eta_s)}</span></div>` : ""}
    ${j.error ? `<div class="notice err">${esc(j.error)}</div>` : ""}
    <div class="outputs">${outputs}</div>
    ${inputs ? `<h3>Inputs</h3><div class="inputs">${inputs}</div>` : ""}
    <dl class="kv">
      <dt>Job</dt><dd>${esc(j.id)}</dd><dt>Client</dt><dd>${esc(j.client || "")}</dd>
      <dt>Model</dt><dd>${esc(j.model || "—")} ${j.model_state ? `<span class="chip ${esc(j.model_state)}">${esc(MODEL_LABEL[j.model_state] || j.model_state)}</span>` : ""}</dd>
      <dt>Created</dt><dd>${esc(fmtTime(j.created_at))}</dd><dt>Duration</dt><dd>${esc(fmtDur(j.elapsed_s))} (estimate ${esc(fmtDur(j.estimate_s))})</dd>
    </dl>
    <h3>Parameters</h3><dl class="kv">${params}</dl>
    ${j.result ? `<h3>Result</h3><pre>${esc(JSON.stringify(j.result, null, 2))}</pre>` : ""}
    ${j.log_tail ? `<h3>Log</h3><pre>${esc(j.log_tail)}</pre>` : ""}
    <div class="actions">${j.state === "running" ? `<button class="danger" data-cancel="${esc(j.id)}">Cancel</button>` :
      `<button class="danger" data-delete="${esc(j.id)}">Delete job and files</button>`}</div>`;
  if (j.state === "running") drawerTimer = setTimeout(() => { if (openJobId === id) openJob(id); }, 2000);
}
function closeDrawer() { openJobId = null; connectState = null; editorState = null; clearTimeout(drawerTimer); $("#drawer").hidden = true; }
$("#d-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ---------------------------------------------------------------- services and new job

let configErrors = null;
async function loadServices() {
  const data = await api("/api/v1/services");
  services = Object.fromEntries(data.services.map((s) => [s.id, s]));
  configErrors = data.config_errors;
  const opts = data.services.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  const cur = $("#f-service").value;
  $("#f-service").innerHTML = `<option value="">All services</option>` + opts;
  $("#f-service").value = cur;
  const runnable = data.services.filter((s) => s.kind !== "http_proxy");
  const curN = $("#n-service").value;
  $("#n-service").innerHTML = runnable.map((s) => `<option value="${esc(s.id)}"${s.available ? "" : " disabled"}>${esc(s.name)}${s.available ? "" : " (unavailable)"}</option>`).join("");
  if (curN && services[curN]) $("#n-service").value = curN;
  buildForm();
  renderServiceCards();
}

function serviceActions(s) {
  if (!me?.admin || !s.file) return "";
  const localOnly = !me.local && LOCAL_ONLY_KINDS.includes(s.kind);
  return `<div class="actions">
    <button type="button" class="secondary small" data-svc-edit="${esc(s.file)}">${localOnly ? "View" : "Edit"}</button>
    <button type="button" class="secondary small" data-svc-dup="${esc(s.file)}"${localOnly ? " disabled" : ""}>Duplicate</button>
    ${s.kind === "http_proxy" ? `<button type="button" class="secondary small" data-svc-test="${esc(s.id)}">Test connection</button>` : ""}
    <button type="button" class="danger small" data-svc-del="${esc(s.file)}"${localOnly ? " disabled" : ""}>Delete</button>
    <span class="muted test-result" id="test-${esc(s.id)}"></span>
  </div>`;
}

function renderServiceCards() {
  const list = Object.values(services);
  $("#services").innerHTML = list.map((s) => `<div class="card service">
      <h3>${esc(s.name)}</h3>
      <div class="ids">${esc(s.id)} · ${esc(s.kind)} · ${esc(s.category)} · resources: ${esc(s.resources.join(", ") || "none")} (${esc(s.lock)})</div>
      <div>${s.available ? `<span class="chip loaded">available</span>` : `<span class="chip loading">unavailable: ${esc(s.unavailable_reason)}</span>`}
        ${s.model ? `<span class="chip">${esc(s.model)}</span>` : ""}</div>
      <p class="muted">${esc(s.description)}</p>
      ${s.proxy_base_url ? `<p class="params">Endpoint: <code>${esc(s.proxy_base_url)}</code></p>` : ""}
      <div class="params">${Object.entries(s.params).map(([k, p]) => `<code>${esc(k)}</code> ${esc(p.type)}${p.required ? "*" : ""}`).join(" · ")}</div>
      ${serviceActions(s)}
    </div>`).join("") + (configErrors ? `<div class="card service"><h3>Config errors</h3>${Object.entries(configErrors).map(([file, err]) =>
      `<p><code>${esc(file)}</code></p><pre>${esc(err)}</pre>${me?.admin ? `<div class="actions"><button type="button" class="secondary small" data-svc-edit="${esc(file)}">Fix</button>
       <button type="button" class="danger small" data-svc-del="${esc(file)}">Delete</button></div>` : ""}`).join("")}</div>` : "");
}

$("#reload").addEventListener("click", async () => {
  try { const r = await api("/api/v1/admin/reload", { method: "POST" }); $("#reload-msg").textContent = `${r.services.length} services loaded${r.config_errors ? " — with errors" : ""}`; await loadServices(); }
  catch (e) { $("#reload-msg").textContent = e.message; }
});

function fieldHtml(name, p) {
  const label = `${name}${p.required ? " *" : ""}`;
  const hint = [p.description, p.min != null || p.max != null ? `${p.min ?? ""}–${p.max ?? ""}` : "", p.multiple_of ? `multiple of ${p.multiple_of}` : "",
    p.default != null && p.default !== "random" ? `default ${p.default}` : (p.default === "random" ? "random if empty" : "")].filter(Boolean).join(" · ");
  let input;
  if (p.type === "string" && name.includes("prompt")) input = `<textarea name="${esc(name)}"${p.required ? " required" : ""}></textarea>`;
  else if (p.type === "enum") input = `<select name="${esc(name)}"><option value="">${p.default != null ? "default (" + esc(p.default) + ")" : "—"}</option>${(p.values || []).map((v) => `<option>${esc(v)}</option>`).join("")}</select>`;
  else if (p.type === "bool") input = `<select name="${esc(name)}"><option value="">default (${esc(p.default)})</option><option value="true">yes</option><option value="false">no</option></select>`;
  else if (p.type === "file" || p.type === "files") input = `<input type="file" name="${esc(name)}"${p.type === "files" ? " multiple" : ""}${(p.accept || []).includes("image") ? ' accept="image/*"' : (p.accept || []).includes("video") ? ' accept="video/*"' : ""}${p.required ? " required" : ""}>`;
  else if (p.type === "json") input = `<textarea name="${esc(name)}" placeholder="JSON"></textarea>`;
  else input = `<input name="${esc(name)}" ${p.type === "int" || p.type === "float" ? `type="number" step="${p.type === "int" ? (p.multiple_of || 1) : "any"}"` : 'type="text"'}${p.required ? " required" : ""}>`;
  return `<label class="field">${esc(label)}${input}<span class="hint">${esc(hint)}</span></label>`;
}

function buildForm() {
  const s = services[$("#n-service").value];
  $("#n-result").innerHTML = "";
  if (!s) { $("#n-form").innerHTML = ""; $("#n-desc").textContent = ""; return; }
  $("#n-desc").textContent = s.description;
  const entries = Object.entries(s.params);
  const big = entries.filter(([, p]) => ["file", "files", "json"].includes(p.type) || (p.type === "string" && p.max_length > 600));
  const small = entries.filter((e) => !big.includes(e));
  $("#n-form").innerHTML = big.map(([k, p]) => fieldHtml(k, p)).join("") +
    `<div class="row2">${small.map(([k, p]) => fieldHtml(k, p)).join("")}</div><div><button type="submit">Submit job</button></div>`;
}
$("#n-service").addEventListener("change", buildForm);
// Files go to the uploads API first (streamed to disk, with progress), then the job references them, so a
// large video is sent once even when the job is refused as busy and submitted again.
const uploadedFiles = new Map();  // name|size|lastModified -> upload id (kept for retries in this page)

function putFile(id, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", `/api/v1/uploads/${id}`);
    x.withCredentials = true;
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => onProgress(e.loaded, e.total || file.size);
    x.onload = () => {
      let body = null;
      try { body = JSON.parse(x.responseText); } catch { /* not JSON */ }
      if (x.status < 300) resolve(body);
      else reject(new Error(body?.detail?.error || body?.detail || `upload failed (HTTP ${x.status})`));
    };
    x.onerror = () => reject(new Error("the upload was interrupted (network error)"));
    x.send(file);
  });
}

async function uploadFile(file, show) {
  const key = `${file.name}|${file.size}|${file.lastModified}`;
  const known = uploadedFiles.get(key);
  if (known) {
    try { const u = await api(`/api/v1/uploads/${known}`); if (u.state === "complete") return known; } catch { /* expired */ }
  }
  const up = await api("/api/v1/uploads", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ filename: file.name, size: file.size }) });
  await putFile(up.id, file, (done, total) => show(file.name, done, total));
  uploadedFiles.set(key, up.id);
  return up.id;
}

function showUpload(name, done, total) {
  const pct = total ? Math.round(done / total * 100) : 0;
  $("#n-result").innerHTML = `<div class="notice">Uploading ${esc(name)}: ${pct}% (${esc(fmtBytes(done))} of ${esc(fmtBytes(total))})
    <div class="bar"><div style="width:${pct}%"></div></div></div>`;
}

$("#n-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const s = services[$("#n-service").value];
  const params = {}, uploads = {};
  const btn = ev.submitter; if (btn) btn.disabled = true;
  try {
    for (const [name, p] of Object.entries(s.params)) {
      const el = $("#n-form").elements[name];
      if (!el) continue;
      if (p.type === "file" || p.type === "files") {
        for (const f of el.files) (uploads[name] ??= []).push(await uploadFile(f, showUpload));
        continue;
      }
      const v = el.value.trim();
      if (v === "") continue;
      if (p.type === "int" || p.type === "float") params[name] = Number(v);
      else if (p.type === "bool") params[name] = v === "true";
      else if (p.type === "json") { try { params[name] = JSON.parse(v); } catch { throw new Error(`${name}: invalid JSON`); } }
      else if (p.type === "enum") params[name] = (p.values || []).find((x) => String(x) === v) ?? v;
      else params[name] = v;
    }
    const r = await api("/api/v1/jobs", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ service: s.id, params, uploads }) });
    $("#n-result").innerHTML = `<div class="notice">Job accepted (estimate ${esc(fmtDur(r.estimate_s))}). <button class="link" data-open="${esc(r.id)}">Follow it</button></div>`;
    pollStatus();
  } catch (e) {
    const busy = e.status === 409 && e.retryAfter ? ` Try again in ${fmtDur(Number(e.retryAfter))}; the files are already uploaded.` : "";
    $("#n-result").innerHTML = `<div class="notice err">${esc(e.message)}${esc(busy)}</div>`;
  } finally { if (btn) btn.disabled = false; }
});

// ---------------------------------------------------------------- service editor

let editorState = null;
let editorMeta = null;  // templates, resources, secrets, local

async function editorInfo() {
  if (!editorMeta) editorMeta = await api("/api/v1/admin/service-files");
  return editorMeta;
}

function yq(v) {
  // A YAML scalar: plain when that is unambiguous, otherwise a JSON string (valid YAML).
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  const s = String(v);
  const plain = /^[A-Za-z0-9][A-Za-z0-9 _.\/:()+,\-—]*$/.test(s) && !/: |:$| #/.test(s) &&
    !/^(true|false|yes|no|on|off|null|y|n)$/i.test(s) && isNaN(Number(s));
  return plain ? s : JSON.stringify(s);
}
function slug(s) {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
}

async function openNewService() {
  let info;
  try { info = await editorInfo(); } catch (e) { return; }
  openDrawer("New service");
  editorState = { mode: "choose" };
  const cards = [
    { id: "form", title: "API proxy (form)", text: "The simple way: put an HTTP API that already runs (LM Studio, Ollama, Unsloth Studio, any OpenAI-compatible server) behind openField's keys, GPU slots and request log." },
    ...info.templates.map((t) => ({ id: "tpl:" + t.kind, title: `${t.title} (YAML)`, text: t.description, disabled: t.local_only && !info.local })),
  ];
  $("#d-body").innerHTML = `<p class="muted">Each service is a YAML file in <code>${esc(info.dir)}</code>. openField validates it,
      saves it and reloads the services; nothing needs a restart.</p>
    <div class="choices">${cards.map((c) => `<button type="button" class="choice" data-choice="${esc(c.id)}"${c.disabled ? " disabled" : ""}>
      <b>${esc(c.title)}</b><span>${esc(c.text)}${c.disabled ? " Only on the openField machine (localhost)." : ""}</span></button>`).join("")}
      <div class="choice"><b>Copy an existing service</b><span>Start from one that works.</span>
        <select id="copy-from" aria-label="Service to copy"><option value="">Choose…</option>${Object.values(services).filter((s) => s.file)
          .map((s) => `<option value="${esc(s.file)}">${esc(s.name)}</option>`).join("")}</select></div>
    </div>`;
}

document.addEventListener("change", (ev) => {
  if (ev.target.id === "copy-from" && ev.target.value) editServiceFile(ev.target.value, true);
});

function proxyFormHtml(info) {
  const secrets = info.local ? info.secrets : [];
  return `<form id="proxy-form" class="form">
    <div class="row2">
      <label class="field">Name *<input name="name" required placeholder="LM Studio"></label>
      <label class="field">Id *<input name="id" required pattern="[A-Za-z0-9_\\-]+" placeholder="lm-studio"><span class="hint">Letters, digits, - and _; the URL is /proxy/&lt;id&gt;/</span></label>
    </div>
    <label class="field">Description<textarea name="description" placeholder="What it serves, for people and agents"></textarea></label>
    <div class="row2">
      <label class="field">Upstream URL *<input name="upstream" required placeholder="http://127.0.0.1:1234"><span class="hint">Where the API runs now</span></label>
      <label class="field">Client base path<input name="client_base" placeholder="v1"><span class="hint">OpenAI-compatible APIs: v1</span></label>
      <label class="field">Category<select name="category"><option>llm</option><option>image</option><option>video</option><option>audio</option><option>other</option></select></label>
      <label class="field">Model<input name="model" placeholder="Shown in the status"></label>
    </div>
    <div class="row2">
      <label class="field">Upstream auth<select name="auth">
        <option value="none">None</option>
        <option value="passthrough">Forward the caller's key</option>
        ${secrets.length ? `<option value="headers">A secret from daemon.yaml</option>` : ""}
      </select><span class="hint">${info.local ? "Secrets are declared in daemon.yaml (secrets:)" : "Secrets can only be used from the openField machine"}</span></label>
      <label class="field auth-secret" hidden>Secret<select name="secret">${secrets.map((s) => `<option>${esc(s)}</option>`).join("")}</select></label>
      <label class="field auth-secret" hidden>Header<input name="header" value="Authorization"></label>
      <label class="field auth-secret" hidden>Value prefix<input name="prefix" value="Bearer "></label>
    </div>
    <div class="row2">
      <label class="field check-field"><span><input type="checkbox" name="gpu" checked> Takes a GPU slot</span><span class="hint">Calls share one slot and wait for image or video jobs (409)</span></label>
      <label class="field">Max concurrent calls<input name="max_concurrency" type="number" min="1" max="64" value="8"></label>
      <label class="field">Timeout (s)<input name="timeout" type="number" min="1" value="3600"></label>
    </div>
    <label class="field">Notes for agents<textarea name="notes" placeholder="One per line, e.g. List the models with GET <base URL>/models"></textarea></label>
    <div class="actions"><button type="submit">Save service</button><button type="button" class="secondary" id="proxy-as-yaml">Edit as YAML</button>
      <span id="editor-status" class="muted"></span></div>
  </form>`;
}

function proxyYaml(form) {
  const v = (n) => form.elements[n].value.trim();
  const lines = [`id: ${yq(v("id"))}`, `name: ${yq(v("name"))}`];
  if (v("description")) lines.push(`description: ${yq(v("description").replace(/\s+/g, " "))}`);
  lines.push(`category: ${yq(v("category"))}`, "kind: http_proxy");
  if (v("model")) lines.push(`model: ${yq(v("model"))}`);
  if (form.elements.gpu.checked) lines.push(`resources: [${editorMeta.resources[0] || "gpu"}]`, "lock: shared", `max_concurrency: ${Number(v("max_concurrency")) || 8}`);
  else lines.push("resources: []");
  lines.push("proxy:", `  upstream: ${yq(v("upstream"))}`, `  upstream_auth: ${v("auth")}`);
  if (v("auth") === "headers") lines.push(`  headers: {${yq(v("header") || "Authorization")}: ${JSON.stringify(form.elements.prefix.value + "{secret." + v("secret") + "}")}}`);
  if (v("client_base")) lines.push(`  client_base: ${yq(v("client_base").replace(/^\/+|\/+$/g, ""))}`);
  lines.push(`  timeout_s: ${Number(v("timeout")) || 3600}`);
  const notes = v("notes").split("\n").map((x) => x.trim()).filter(Boolean);
  if (notes.length) lines.push("agent_notes:", ...notes.map((n) => `  - ${yq(n)}`));
  return lines.join("\n") + "\n";
}

async function openProxyForm() {
  const info = await editorInfo();
  openDrawer("New service — API proxy");
  editorState = { mode: "form", file: null };
  $("#d-body").innerHTML = proxyFormHtml(info);
  const form = $("#proxy-form");
  let idTouched = false;
  form.elements.id.addEventListener("input", () => { idTouched = true; });
  form.elements.name.addEventListener("input", () => { if (!idTouched) form.elements.id.value = slug(form.elements.name.value); });
  form.elements.auth.addEventListener("change", () => form.querySelectorAll(".auth-secret").forEach((el) => { el.hidden = form.elements.auth.value !== "headers"; }));
  $("#proxy-as-yaml").addEventListener("click", () => {
    if (!form.reportValidity()) return;
    openYamlEditor({ file: null, text: proxyYaml(form), version: null, editable: true, title: "New service" });
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    await saveService(proxyYaml(form), null, null);
  });
}

function openYamlEditor({ file, text, version, editable, title, error }) {
  openDrawer(title);
  editorState = { mode: "yaml", file, version, editable };
  $("#d-body").innerHTML = `
    ${error ? `<div class="notice err">This file does not load: ${esc(error)}</div>` : ""}
    ${editable ? "" : `<div class="notice">Services that run commands or use the daemon's secrets are changed only on the openField machine (http://localhost). Read-only here.</div>`}
    <p class="muted">Fields are described in the README (Adding a service). The status line checks the file as you type.</p>
    <textarea id="yaml" class="yaml" spellcheck="false"${editable ? "" : " readonly"}>${esc(text)}</textarea>
    <div class="actions">${editable ? `<button type="button" id="yaml-save">Save service</button>` : ""}
      <span id="editor-status" class="muted"></span></div>`;
  const ta = $("#yaml");
  ta.addEventListener("keydown", (e) => {
    if (e.key !== "Tab" || e.shiftKey || ta.readOnly) return;
    e.preventDefault();
    ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
  });
  let timer = null;
  ta.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(validateYaml, 400); });
  if (editable) $("#yaml-save").addEventListener("click", () => saveService(ta.value, editorState.file, editorState.version));
  validateYaml();
}

async function validateYaml() {
  const st = editorState;
  const ta = $("#yaml");
  if (!st || !ta || !st.editable) return;
  try {
    const r = await api("/api/v1/admin/service-files/validate", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ text: ta.value, file: st.file }) });
    if (editorState !== st) return;
    $("#editor-status").innerHTML = r.ok
      ? `<span class="ok">✓ Valid: <code>${esc(r.id)}</code> (${esc(r.kind)})${r.renames ? `, saved as ${esc(r.file)}` : ""}</span>`
      : `<span class="error">✗ ${esc(r.error)}</span>`;
  } catch (e) { $("#editor-status").textContent = e.message; }
}

async function saveService(text, file, version) {
  const status = $("#editor-status");
  try {
    const r = await api(file ? `/api/v1/admin/service-files/${encodeURIComponent(file)}` : "/api/v1/admin/service-files", {
      method: file ? "PUT" : "POST", headers: JSON_HEADERS, body: JSON.stringify({ text, version }) });
    await loadServices();
    const s = r.service;
    openYamlEditor({ file: r.file.file, text: r.file.text, version: r.file.version, editable: r.file.editable, title: `Edit — ${s.name}` });
    $("#d-body").insertAdjacentHTML("afterbegin", `<div class="notice">Saved and loaded: ${s.available ? "available" : `unavailable (${esc(s.unavailable_reason)})`}.
      ${s.proxy_base_url ? `Endpoint <code>${esc(s.proxy_base_url)}</code>. <button type="button" class="link" data-svc-test="${esc(s.id)}">Test connection</button> <span class="muted test-result" id="test-${esc(s.id)}"></span>` : ""}</div>`);
  } catch (e) {
    if (status) status.innerHTML = `<span class="error">✗ ${esc(e.message)}</span>`;
  }
}

async function editServiceFile(file, duplicate) {
  let f;
  try { f = await api(`/api/v1/admin/service-files/${encodeURIComponent(file)}`); } catch (e) { return; }
  if (duplicate) {
    const text = f.text.replace(/^id:\s*(\S+)/m, (_, id) => `id: ${id}-copy`).replace(/^name:\s*(.+)$/m, (_, n) => `name: ${n.replace(/^["']|["']$/g, "")} (copy)`);
    openYamlEditor({ file: null, text, version: null, editable: true, title: "New service" });
    return;
  }
  const s = Object.values(services).find((x) => x.file === file);
  openYamlEditor({ file, text: f.text, version: f.version, editable: f.editable, title: s ? `Edit — ${s.name}` : `Edit — ${file}`, error: f.error });
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-choice],[data-svc-edit],[data-svc-dup],[data-svc-del],[data-svc-test],#new-service");
  if (!t || t.disabled) return;
  if (t.id === "new-service") openNewService();
  if (t.dataset.choice === "form") openProxyForm();
  if (t.dataset.choice?.startsWith("tpl:")) {
    const tpl = editorMeta.templates.find((x) => "tpl:" + x.kind === t.dataset.choice);
    openYamlEditor({ file: null, text: tpl.text, version: null, editable: true, title: `New service — ${tpl.title}` });
  }
  if (t.dataset.svcEdit) editServiceFile(t.dataset.svcEdit, false);
  if (t.dataset.svcDup) editServiceFile(t.dataset.svcDup, true);
  if (t.dataset.svcDel) {
    if (t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Confirm delete"; return; }
    try { await api(`/api/v1/admin/service-files/${encodeURIComponent(t.dataset.svcDel)}`, { method: "DELETE" }); $("#reload-msg").textContent = ""; }
    catch (e) { $("#reload-msg").textContent = e.message; }
    loadServices();
  }
  if (t.dataset.svcTest) {
    const out = document.getElementById(`test-${t.dataset.svcTest}`);
    if (out) out.textContent = "testing…";
    try {
      const r = await api(`/api/v1/admin/services/${encodeURIComponent(t.dataset.svcTest)}/test`, { method: "POST" });
      if (out) out.textContent = r.ok ? `answered HTTP ${r.status} in ${r.ms} ms (${r.url})` : `no answer: ${r.error}`;
    } catch (e) { if (out) out.textContent = e.message; }
  }
});

// ---------------------------------------------------------------- requests tab

let reqTimer = null;
async function loadRequests() {
  clearTimeout(reqTimer);
  if (activeTab !== "requests") return;
  try {
    const data = await api("/api/v1/requests?limit=200");
    $("#requests").innerHTML = data.requests.map((r) => `<tr><td>${esc(fmtTime(r.started_at))}</td><td>${esc(svcName(r.service))}</td>
      <td>${esc(r.method)} ${esc(r.path)}</td><td>${esc(r.model || "")}</td><td>${esc(r.status ?? "")}${r.error ? " " + esc(r.error) : ""}</td>
      <td>${esc(fmtDur(r.duration_s))}</td><td>${esc(r.client || "")}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">No requests yet.</td></tr>`;
  } catch { /* ignore */ }
  reqTimer = setTimeout(loadRequests, 5000);
}

// ---------------------------------------------------------------- system settings

let settings = null;
async function loadSettings() {
  try { settings = await api("/api/v1/settings"); } catch { return; }
  const box = $("#settings");
  box.innerHTML = Object.entries(settings.resources).map(([name, r]) => `
    <div class="card">
      <h3>Parallel jobs — ${esc(name.toUpperCase())}</h3>
      <p class="muted">${esc(r.description || "")}. How many jobs may use this resource at once. A new job starts only
        when a slot is free <b>and</b> there is enough free memory for its model (the service's memory plus
        ${esc(r.memory_margin_gb)} GB of margin, minus what jobs still loading will take). Otherwise the answer is 409
        and the request can be repeated later. Jobs running at once split the GPU: each one runs slower.</p>
      <div class="segmented" data-resource="${esc(name)}">${Array.from({ length: r.max }, (_, i) => i + 1).map((n) =>
        `<button type="button" data-slots="${n}" class="${n === r.slots ? "active" : ""}"${me?.admin ? "" : " disabled"}>${n}</button>`).join("")}</div>
      ${me?.admin ? "" : `<div class="muted" style="font-size:13px">Only an admin key changes this setting.</div>`}
      <div class="muted" style="font-size:13px">daemon.yaml default: ${esc(r.default)} · maximum ${esc(r.max)}</div>
      <div class="gauges" id="gauges-${esc(name)}"></div>
      <div id="settings-msg-${esc(name)}" class="muted" style="margin-top:8px"></div>
    </div>`).join("");
  renderGauges();
}
function renderGauges() {
  if (!settings || !lastStatus) return;
  for (const name of Object.keys(settings.resources)) {
    const el = document.getElementById(`gauges-${name}`);
    const r = (lastStatus.resources || {})[name];
    if (!el || !r) continue;
    const dots = Array.from({ length: r.slots }, (_, i) => `<i class="${i < r.used ? "on" : ""}"></i>`).join("");
    el.innerHTML = `
      <div class="gauge"><b>${r.used}/${r.slots}</b><span>slots in use</span><div class="slots">${dots}</div></div>
      <div class="gauge"><b>${esc(lastStatus.memory_available_gb)} GB</b><span>free memory now</span></div>
      <div class="gauge"><b>${esc(r.reserved_loading_gb)} GB</b><span>reserved by loading jobs</span></div>
      <div class="gauge"><b>${esc(r.holders.length ? holderNames(r) : "—")}</b><span>using it now</span></div>`;
  }
}
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest(".segmented button[data-slots]");
  if (!b) return;
  const resource = b.parentElement.dataset.resource;
  const msg = document.getElementById(`settings-msg-${resource}`);
  try {
    await api("/api/v1/settings", { method: "PUT", headers: JSON_HEADERS,
      body: JSON.stringify({ resources: { [resource]: { slots: Number(b.dataset.slots) } } }) });
    msg.textContent = `Saved: up to ${b.dataset.slots} job${b.dataset.slots === "1" ? "" : "s"} at once, from the next request on.`;
    await loadSettings();
    pollStatus();
  } catch (e) { msg.textContent = e.message; }
});

// ---------------------------------------------------------------- clipboard

const copyStore = new Map();
let copySeq = 0;
function reg(text) { const id = "c" + (++copySeq); copyStore.set(id, text); return id; }
function codeBlock(title, text) {
  return `<div class="code"><div class="code-head"><span>${esc(title)}</span>
    <button type="button" class="secondary small" data-copy="${reg(text)}">Copy</button></div><pre>${esc(text)}</pre></div>`;
}
async function copyText(text) {
  // navigator.clipboard needs a secure context (localhost or HTTPS); over plain http on the LAN, fall back.
  if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return; }
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta); ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  if (!ok) throw new Error("copy failed");
}
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-copy]");
  if (!b) return;
  const text = copyStore.get(b.dataset.copy);
  if (text == null) return;
  if (b.dataset.label == null) b.dataset.label = b.textContent;
  try { await copyText(text); b.textContent = "Copied ✓"; }
  catch {
    b.textContent = "Select and copy";
    const pre = b.closest(".code")?.querySelector("pre");
    if (pre) { const r = document.createRange(); r.selectNodeContents(pre); getSelection().removeAllRanges(); getSelection().addRange(r); }
  }
  clearTimeout(b._t); b._t = setTimeout(() => { b.textContent = b.dataset.label; }, 1600);
});

// ---------------------------------------------------------------- access: keys and agent onboarding

let connectInfo = null;
let keysById = {};
let connectState = null;

async function loadAccess() {
  try { connectInfo = await api("/api/v1/connect"); } catch { return; }
  $("#local-url").textContent = connectInfo.local_url;
  const rows = [["Base URL", connectInfo.base_url], ["MCP (streamable HTTP)", connectInfo.mcp_url],
    ["OpenAPI", connectInfo.openapi_url], ["Skill (SKILL.md)", connectInfo.skill_url],
    ...connectInfo.proxies.map((p) => [p.name, p.base_url])];
  $("#endpoints").innerHTML = rows.map(([k, v]) => `<div class="endpoint"><span class="muted">${esc(k)}</span>
    <code>${esc(v)}</code><button type="button" class="secondary small" data-copy="${reg(v)}">Copy</button></div>`).join("");
  $("#keys-card").hidden = !me.admin;
  $("#keys-readonly").hidden = !!me.admin;
  if (me.admin) await loadKeys();
}

async function loadKeys() {
  let data;
  try { data = await api("/api/v1/keys"); } catch (e) { keyMsg(e.message, true); return; }
  keysById = Object.fromEntries(data.keys.map((k) => [k.id, k]));
  $("#keys").innerHTML = data.keys.map((k) => {
    const own = k.source === "openfield";
    const role = own ? `<select data-key-role="${esc(k.id)}" aria-label="Role">${data.roles.map((r) =>
      `<option value="${r}"${r === k.role ? " selected" : ""}>${r}</option>`).join("")}</select>` : esc(k.role);
    const used = k.last_used_at ? `${fmtTime(k.last_used_at)}${k.last_ip ? " · " + k.last_ip : ""}` : "never";
    const actions = `<button type="button" class="secondary small" data-connect="${esc(k.id)}">Connect</button>` + (own
      ? `<button type="button" class="secondary small" data-key-toggle="${esc(k.id)}">${k.disabled ? "Enable" : "Disable"}</button>
         <button type="button" class="danger small" data-key-revoke="${esc(k.id)}">Revoke</button>`
      : `<span class="muted">managed in oMLX</span>`);
    return `<tr class="${k.disabled ? "off" : ""}">
      <td><b>${esc(k.name)}</b>${k.disabled ? ` <span class="chip">disabled</span>` : ""}${me.key_id === k.id ? ` <span class="chip loaded">you</span>` : ""}</td>
      <td>${role}</td><td>${own ? "openField" : "oMLX"}</td><td><code>${esc(k.prefix ? k.prefix + "…" : "—")}</code></td>
      <td>${esc(fmtTime(k.created_at))}</td><td>${esc(used)}</td><td><div class="row-actions">${actions}</div></td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">No keys yet.</td></tr>`;
}

function keyMsg(text, err) { $("#key-msg").innerHTML = text ? `<div class="notice${err ? " err" : ""}">${esc(text)}</div>` : ""; }

$("#key-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = ev.submitter; if (btn) btn.disabled = true;
  try {
    const r = await api("/api/v1/keys", { method: "POST", headers: JSON_HEADERS,
      body: JSON.stringify({ name: $("#key-name").value, role: $("#key-role").value }) });
    $("#key-name").value = ""; keyMsg("");
    await loadKeys();
    openConnect(r, r.key);
  } catch (e) { keyMsg(e.message, true); } finally { if (btn) btn.disabled = false; }
});

async function patchKey(id, body) {
  try {
    await api(`/api/v1/keys/${id}`, { method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(body) });
    keyMsg("");
  } catch (e) { keyMsg(e.message, true); }
  await loadKeys();
}
document.addEventListener("change", (ev) => {
  const sel = ev.target.closest("[data-key-role]");
  if (sel) patchKey(sel.dataset.keyRole, { role: sel.value });
});
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-connect],[data-key-toggle],[data-key-revoke],[data-ctab],#connect-generic");
  if (!t) return;
  if (t.id === "connect-generic") openConnect(null, null);
  if (t.dataset.connect) openConnect(keysById[t.dataset.connect], null);
  if (t.dataset.keyToggle) patchKey(t.dataset.keyToggle, { disabled: !keysById[t.dataset.keyToggle].disabled });
  if (t.dataset.keyRevoke) {
    if (t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Confirm revoke"; return; }
    try { await api(`/api/v1/keys/${t.dataset.keyRevoke}`, { method: "DELETE" }); keyMsg(""); } catch (e) { keyMsg(e.message, true); }
    loadKeys();
  }
  if (t.dataset.ctab && connectState) { connectState.tab = t.dataset.ctab; renderConnect(); }
});

async function openConnect(key, secret) {
  if (!connectInfo) { try { connectInfo = await api("/api/v1/connect"); } catch { return; } }
  openDrawer(key ? `Connect — ${key.name}` : "Connect an agent");
  const state = { key, secret, inline: !!secret, tab: "mcp", skill: null };
  connectState = state;
  renderConnect();
  try {
    const res = await fetch("/api/v1/skill", { credentials: "same-origin" });
    if (res.ok && connectState === state) { state.skill = await res.text(); if (state.tab === "skill") renderConnect(); }
  } catch { /* the preview stays empty */ }
}

function renderConnect() {
  const st = connectState, c = connectInfo;
  const K = st.inline && st.secret ? st.secret : null;
  const sh = K ?? "$OPENFIELD_KEY", js = K ?? "${OPENFIELD_KEY}", ps = K ?? "$env:OPENFIELD_KEY";
  const literal = K ?? "<paste the key here>";
  const head = st.secret
    ? `<div class="secret-box"><div><b>Key created.</b> Copy it now: it will not be shown again.</div>
        <div class="secret"><code>${esc(st.secret)}</code><button type="button" data-copy="${reg(st.secret)}">Copy key</button></div>
        <label class="check"><input type="checkbox" id="inline-key"${st.inline ? " checked" : ""}> Put the key in the snippets below</label></div>`
    : `<div class="notice">openField does not keep keys in readable form, so the snippets read the
        <code>OPENFIELD_KEY</code> environment variable. Set it on the agent's machine to the key${st.key?.prefix ? ` (it starts with <code>${esc(st.key.prefix)}</code>)` : ""}.</div>`;
  const tabs = `<div class="segmented connect-tabs">${[["mcp", "MCP"], ["api", "REST API"], ["skill", "Skill"]].map(([id, label]) =>
    `<button type="button" data-ctab="${id}" class="${st.tab === id ? "active" : ""}">${label}</button>`).join("")}</div>`;
  let body = "";
  if (st.tab === "mcp") {
    const json = JSON.stringify({ mcpServers: { openfield: { type: "http", url: c.mcp_url, headers: { "X-Token": js } } } }, null, 2);
    body = `<p class="muted">The agent gets openField's tools: <code>status</code>, <code>list_services</code>,
        <code>submit_job</code>, <code>wait_job</code>, <code>get_output</code>... Streamable HTTP transport; the server
        also accepts <code>Authorization: Bearer</code>.</p>
      ${codeBlock("Claude Code (all projects)", `claude mcp add --transport http --scope user openfield ${c.mcp_url} --header "X-Token: ${sh}"`)}
      ${codeBlock("Config JSON (mcpServers format: .mcp.json, Cursor and other clients)", json)}
      ${K ? "" : `<p class="muted">In the JSON, <code>\${OPENFIELD_KEY}</code> works only in clients that expand variables (Claude Code does); elsewhere, put the key there.</p>`}`;
  } else if (st.tab === "api") {
    const ex = Object.values(services).find((s) => s.available && s.kind !== "http_proxy" && s.params.prompt &&
      !Object.values(s.params).some((p) => p.required && (p.type === "file" || p.type === "files")));
    const job = ex ? JSON.stringify({ service: ex.id, params: { prompt: "a lighthouse at dusk, photo" } }) : "";
    body = `<p class="muted">REST with JSON (multipart when there are files). Full schema in the
        <a href="${esc(c.openapi_url)}" target="_blank" rel="noopener">OpenAPI</a> and the
        <a href="${esc(c.docs_url)}" target="_blank" rel="noopener">interactive docs</a>. For an agent, the Skill tab packs all of this.</p>
      ${codeBlock("Environment — macOS/Linux", `export OPENFIELD_URL=${c.base_url}\nexport OPENFIELD_KEY=${literal}`)}
      ${codeBlock("Environment — PowerShell", `$env:OPENFIELD_URL = "${c.base_url}"\n$env:OPENFIELD_KEY = "${literal}"`)}
      ${codeBlock("Test", `curl -s -H "X-Token: ${sh}" ${c.base_url}/api/v1/status`)}
      ${codeBlock("Large files: upload first (resumable), then reference the upload", [
        `# 1. create the upload; 2. send the file (if it breaks, resume with: curl -C <received> -T ...)`,
        `ID=$(curl -s -H "X-Token: ${sh}" -H 'Content-Type: application/json' -d '{"filename": "clip.mp4"}' ${c.base_url}/api/v1/uploads | jq -r .id)`,
        `curl -fsS -T clip.mp4 -H "X-Token: ${sh}" ${c.base_url}/api/v1/uploads/$ID`,
        `# 3. the job takes the upload id in place of the file`,
        `curl -s -H "X-Token: ${sh}" -H 'Content-Type: application/json' \\`,
        `  -d '{"service": "video-upscale", "params": {"scale": 2}, "uploads": {"video": "'$ID'"}}' ${c.base_url}/api/v1/jobs`,
        `# large outputs: resumable download, checked against the job's outputs[].sha256`,
        `curl -fL -C - -H "X-Token: ${sh}" -o out.mp4 ${c.base_url}/api/v1/jobs/<job id>/outputs/video`].join("\n"))}
      ${ex ? codeBlock(`Submit a job (${ex.name})`, `curl -s -H "X-Token: ${sh}" -H 'Content-Type: application/json' \\\n  -d '${job}' ${c.base_url}/api/v1/jobs`) : ""}
      ${c.proxies.map((p) => codeBlock(`${p.name} — OpenAI clients`, `export OPENAI_BASE_URL=${p.base_url}\nexport OPENAI_API_KEY=${K ?? "$OPENFIELD_KEY"}`)).join("")}`;
  } else {
    const install = (dir) => `mkdir -p ${dir}/openfield && curl -fsSL -H "X-Token: ${sh}" ${c.skill_url} -o ${dir}/openfield/SKILL.md`;
    const prompt = [`Set up access to openField, an AI services server on the local network (${c.base_url}).`,
      `1. The access key goes in the OPENFIELD_KEY environment variable${K ? `; its value is ${K}` : " (ask the user for its value)"}. ` +
        "Keep it set for future sessions (in the shell profile) and do not write it into any other file.",
      `2. Install the skill: mkdir -p ~/.claude/skills/openfield && curl -fsSL -H "X-Token: $OPENFIELD_KEY" ${c.skill_url} -o ~/.claude/skills/openfield/SKILL.md`,
      "   (if your harness uses another skills folder, save the file there as openfield/SKILL.md).",
      `3. Test: curl -s -H "X-Token: $OPENFIELD_KEY" ${c.base_url}/api/v1/status`,
      "Then use the skill whenever you need to generate or edit images, generate or process videos, or call the local LLMs."].join("\n");
    body = `<p class="muted">An Agent Skills <code>SKILL.md</code> generated from the services configured now: addresses,
        each service's parameters, the API flow and what to do with 409 and 503. It reads the key from
        <code>OPENFIELD_KEY</code> and never stores it in the file. Download it again after adding services.</p>
      ${codeBlock("Claude Code — macOS/Linux", install("~/.claude/skills"))}
      ${codeBlock("Claude Code — PowerShell", `New-Item -ItemType Directory -Force "$HOME\\.claude\\skills\\openfield" | Out-Null\nInvoke-RestMethod -Headers @{ "X-Token" = "${ps}" } ${c.skill_url} -OutFile "$HOME\\.claude\\skills\\openfield\\SKILL.md"`)}
      ${codeBlock("Pi — macOS/Linux", install("~/.pi/agent/skills"))}
      ${codeBlock("Paste into any agent's chat", prompt)}
      <div class="actions">
        <button type="button" class="secondary"${st.skill ? ` data-copy="${reg(st.skill)}"` : " disabled"}>Copy SKILL.md</button>
        <a href="/api/v1/skill?download=true"><button type="button" class="secondary">Download SKILL.md</button></a>
      </div>
      <details class="preview"><summary>SKILL.md preview</summary><pre>${esc(st.skill || "loading…")}</pre></details>`;
  }
  $("#d-body").innerHTML = head + tabs + body;
  const box = $("#inline-key");
  if (box) box.addEventListener("change", () => { connectState.inline = box.checked; renderConnect(); });
}

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  activeTab = b.dataset.tab;
  document.querySelectorAll(".tab").forEach((t) => { t.hidden = t.id !== "tab-" + activeTab; });
  if (activeTab === "requests") loadRequests();
  if (activeTab === "jobs") loadJobs(true);
  if (activeTab === "system") loadSettings();
  if (activeTab === "access") loadAccess();
}));

start();
