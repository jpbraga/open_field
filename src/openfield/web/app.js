"use strict";

const $ = (sel) => document.querySelector(sel);
const STATE_PT = { running: "Executando", queued: "Na fila", done: "Concluído", error: "Erro", cancelled: "Cancelado" };
const MODEL_PT = { loading: "carregando modelo", loaded: "modelo carregado", released: "modelo liberado", "per stage": "por etapa" };
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
  const hm = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return today ? hm : d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) + " " + hm;
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

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401) { showLogin(); throw new Error("unauthorized"); }
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const err = new Error(typeof body?.detail === "string" ? body.detail : (body?.detail?.reason || body?.detail?.error || res.statusText));
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
    $("#who").textContent = me.local ? "acesso local (sem chave)" : `${me.client}${me.admin ? " · admin" : ""}`;
    $("#logout").hidden = !!me.local;
    $("#reload").hidden = !me.admin;
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
    await api("/api/v1/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: $("#login-key").value }) });
    $("#login-key").value = "";
    start();
  } catch (e) { $("#login-error").textContent = "Chave não aceita."; $("#login-error").hidden = false; }
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
  return [...r.holders.map((h) => svcName(h.service) + (h.calls > 1 ? ` (${h.calls} chamadas)` : "")),
    ...(r.external || []).map((e) => `${e} fora do openField`)].join(", ");
}
function renderStatus(s) {
  lastStatus = s;
  const pill = $("#gpu-pill");
  const gpu = (s.resources || {}).gpu || Object.values(s.resources || {})[0];
  if (!gpu || gpu.used === 0) {
    pill.className = "pill free";
    pill.textContent = gpu && gpu.slots > 1 ? `GPU livre · ${gpu.slots} vagas` : "GPU livre";
  } else if (gpu.free === 0) {
    pill.className = "pill busy";
    pill.textContent = `GPU ${gpu.slots > 1 ? `${gpu.used}/${gpu.slots}` : "ocupada"} — ${holderNames(gpu)}`;
  } else {
    pill.className = "pill partial";
    pill.textContent = `GPU ${gpu.used}/${gpu.slots} — ${holderNames(gpu)}`;
  }
  $("#mem").textContent = `${s.memory_available_gb} GB livres`;
  if (activeTab === "system") renderGauges();
  const box = $("#running");
  if (!s.running.length) { box.innerHTML = ""; return; }
  box.innerHTML = s.running.map((j) => {
    const pct = Math.round((j.progress || 0) * 100);
    const ms = j.model_state ? `<span class="chip ${esc(j.model_state)}">${esc(MODEL_PT[j.model_state] || j.model_state)}</span>` : "";
    return `<div class="card run">
      <div class="run-head"><span class="svc">${esc(svcName(j.service))}</span><span class="muted">${esc(j.stage || "")}</span>
        <span class="right">${ms}<button class="danger" data-cancel="${esc(j.id)}">Cancelar</button>
        <button class="secondary" data-open="${esc(j.id)}">Detalhes</button></span></div>
      <div class="bar"><div style="width:${pct}%"></div></div>
      <div class="run-meta"><span>${pct}%</span><span>ETA ${fmtDur(j.eta_s)}</span>
        <span>decorrido ${fmtDur(Date.now() / 1000 - j.started_at)}</span>${j.model ? `<span>${esc(j.model)}</span>` : ""}
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
    if (t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Confirmar exclusão"; return; }
    try { await api(`/api/v1/jobs/${t.dataset.delete}`, { method: "DELETE" }); closeDrawer(); loadJobs(true); } catch (e) { alertBox(e.message); }
  }
});
function alertBox(msg) { const n = $("#n-result"); if (n) n.innerHTML = `<div class="notice err">${esc(msg)}</div>`; }

// ---------------------------------------------------------------- jobs list

function thumb(j) {
  const o = (j.outputs || [])[0];
  if (!o || j.outputs_purged) return `<div class="thumb">${j.outputs_purged ? "removido" : esc(STATE_PT[j.state] || "")}</div>`;
  if ((o.media_type || "").startsWith("image/")) return `<img class="thumb" loading="lazy" src="${outUrl(j, o)}" alt="">`;
  if ((o.media_type || "").startsWith("video/")) return `<video class="thumb" preload="metadata" muted src="${outUrl(j, o)}#t=0.5"></video>`;
  return `<div class="thumb">arquivo</div>`;
}

async function loadJobs(reset) {
  if (reset) jobsOffset = 0;
  const q = new URLSearchParams({ limit: "40", offset: String(jobsOffset) });
  if ($("#f-service").value) q.set("service", $("#f-service").value);
  if ($("#f-state").value) q.set("state", $("#f-state").value);
  let data;
  try { data = await api(`/api/v1/jobs?${q}`); } catch { return; }
  const html = data.jobs.map((j) => {
    const sub = j.prompt || (j.state === "cancelled" ? "cancelado" : j.error ? "Erro: " + j.error
      : (j.outputs || []).map((o) => `${o.name} ${o.width ? o.width + "×" + o.height : ""}`).join(", "));
    return `<div class="job" data-open="${esc(j.id)}">${thumb(j)}
      <div><div class="title">${esc(svcName(j.service))}</div>
        <div class="sub">${esc(fmtTime(j.created_at))} · ${esc(fmtDur(j.elapsed_s ?? (j.state === "running" ? Date.now() / 1000 - j.started_at : null)))} · ${esc(j.client || "")} — ${esc(sub || "")}</div></div>
      <span class="state ${esc(j.state)}">${esc(STATE_PT[j.state] || j.state)}</span></div>`;
  }).join("");
  if (reset) $("#jobs").innerHTML = html || `<p class="muted">Nenhum job.</p>`; else $("#jobs").insertAdjacentHTML("beforeend", html);
  jobsOffset += data.jobs.length;
  $("#jobs-count").textContent = `${data.total} job(s)`;
  $("#more").hidden = jobsOffset >= data.total;
}
$("#more").addEventListener("click", () => loadJobs(false));
$("#f-service").addEventListener("change", () => loadJobs(true));
$("#f-state").addEventListener("change", () => loadJobs(true));

// ---------------------------------------------------------------- job detail

let drawerTimer = null;
async function openJob(id) {
  openJobId = id;
  clearTimeout(drawerTimer);
  let j;
  try { j = await api(`/api/v1/jobs/${id}`); } catch (e) { return; }
  if (openJobId !== id) return;
  $("#drawer").hidden = false;
  $("#d-title").textContent = `${svcName(j.service)} — ${STATE_PT[j.state] || j.state}`;
  const outputs = (j.outputs || []).map((o) => {
    const media = (o.media_type || "");
    const view = j.outputs_purged ? `<p class="muted">Arquivo removido pela retenção.</p>`
      : media.startsWith("image/") ? `<img src="${outUrl(j, o)}" alt="${esc(o.name)}">`
      : media.startsWith("video/") ? `<video src="${outUrl(j, o)}" controls preload="metadata"></video>` : "";
    const facts = [o.width ? `${o.width}×${o.height}` : "", o.duration_s ? `${o.duration_s.toFixed(2)} s` : "",
      o.frames ? `${o.frames} quadros` : "", o.fps ? `${o.fps} fps` : "", o.audio ? "com áudio" : "", fmtBytes(o.size_bytes)].filter(Boolean).join(" · ");
    return `<div>${view}<div class="actions"><span class="muted">${esc(o.name)} — ${esc(facts)}</span>
      ${j.outputs_purged ? "" : `<a href="${outUrl(j, o, true)}"><button class="secondary">Baixar</button></a>`}</div></div>`;
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
    ${inputs ? `<h3>Entradas</h3><div class="inputs">${inputs}</div>` : ""}
    <dl class="kv">
      <dt>Job</dt><dd>${esc(j.id)}</dd><dt>Cliente</dt><dd>${esc(j.client || "")}</dd>
      <dt>Modelo</dt><dd>${esc(j.model || "—")} ${j.model_state ? `<span class="chip ${esc(j.model_state)}">${esc(MODEL_PT[j.model_state] || j.model_state)}</span>` : ""}</dd>
      <dt>Criado</dt><dd>${esc(fmtTime(j.created_at))}</dd><dt>Duração</dt><dd>${esc(fmtDur(j.elapsed_s))} (estimativa ${esc(fmtDur(j.estimate_s))})</dd>
    </dl>
    <h3>Parâmetros</h3><dl class="kv">${params}</dl>
    ${j.result ? `<h3>Resultado</h3><pre>${esc(JSON.stringify(j.result, null, 2))}</pre>` : ""}
    ${j.log_tail ? `<h3>Log</h3><pre>${esc(j.log_tail)}</pre>` : ""}
    <div class="actions">${j.state === "running" ? `<button class="danger" data-cancel="${esc(j.id)}">Cancelar</button>` :
      `<button class="danger" data-delete="${esc(j.id)}">Excluir job e arquivos</button>`}</div>`;
  if (j.state === "running") drawerTimer = setTimeout(() => openJob(id), 2000);
}
function closeDrawer() { openJobId = null; connectState = null; clearTimeout(drawerTimer); $("#drawer").hidden = true; }
$("#d-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ---------------------------------------------------------------- services and new job

async function loadServices() {
  const data = await api("/api/v1/services");
  services = Object.fromEntries(data.services.map((s) => [s.id, s]));
  const opts = data.services.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  const cur = $("#f-service").value;
  $("#f-service").innerHTML = `<option value="">Todos os serviços</option>` + opts;
  $("#f-service").value = cur;
  const runnable = data.services.filter((s) => s.kind !== "http_proxy");
  const curN = $("#n-service").value;
  $("#n-service").innerHTML = runnable.map((s) => `<option value="${esc(s.id)}"${s.available ? "" : " disabled"}>${esc(s.name)}${s.available ? "" : " (indisponível)"}</option>`).join("");
  if (curN && services[curN]) $("#n-service").value = curN;
  buildForm();
  $("#services").innerHTML = data.services.map((s) => `<div class="card service">
      <h3>${esc(s.name)}</h3>
      <div class="ids">${esc(s.id)} · ${esc(s.kind)} · ${esc(s.category)} · recursos: ${esc(s.resources.join(", ") || "nenhum")} (${esc(s.lock)})</div>
      <div>${s.available ? `<span class="chip loaded">disponível</span>` : `<span class="chip loading">indisponível: ${esc(s.unavailable_reason)}</span>`}
        ${s.model ? `<span class="chip">${esc(s.model)}</span>` : ""}</div>
      <p class="muted">${esc(s.description)}</p>
      ${s.proxy_path ? `<p class="params">Endpoint: <code>${esc(location.origin + s.proxy_path)}</code></p>` : ""}
      <div class="params">${Object.entries(s.params).map(([k, p]) => `<code>${esc(k)}</code> ${esc(p.type)}${p.required ? "*" : ""}`).join(" · ")}</div>
    </div>`).join("") + (data.config_errors ? `<div class="card"><h3>Erros de configuração</h3><pre>${esc(JSON.stringify(data.config_errors, null, 2))}</pre></div>` : "");
}
$("#reload").addEventListener("click", async () => {
  try { const r = await api("/api/v1/admin/reload", { method: "POST" }); $("#reload-msg").textContent = `${r.services.length} serviços carregados${r.config_errors ? " — há erros" : ""}`; await loadServices(); }
  catch (e) { $("#reload-msg").textContent = e.message; }
});

function fieldHtml(name, p) {
  const label = `${name}${p.required ? " *" : ""}`;
  const hint = [p.description, p.min != null || p.max != null ? `${p.min ?? ""}–${p.max ?? ""}` : "", p.multiple_of ? `múltiplo de ${p.multiple_of}` : "",
    p.default != null && p.default !== "random" ? `padrão ${p.default}` : (p.default === "random" ? "aleatória se vazio" : "")].filter(Boolean).join(" · ");
  let input;
  if (p.type === "string" && name.includes("prompt")) input = `<textarea name="${esc(name)}"${p.required ? " required" : ""}></textarea>`;
  else if (p.type === "enum") input = `<select name="${esc(name)}"><option value="">${p.default != null ? "padrão (" + esc(p.default) + ")" : "—"}</option>${(p.values || []).map((v) => `<option>${esc(v)}</option>`).join("")}</select>`;
  else if (p.type === "bool") input = `<select name="${esc(name)}"><option value="">padrão (${esc(p.default)})</option><option value="true">sim</option><option value="false">não</option></select>`;
  else if (p.type === "file" || p.type === "files") input = `<input type="file" name="${esc(name)}"${p.type === "files" ? " multiple" : ""}${(p.accept || []).includes("image") ? ' accept="image/*"' : (p.accept || []).includes("video") ? ' accept="video/*"' : ""}${p.required ? " required" : ""}>`;
  else if (p.type === "json") input = `<textarea name="${esc(name)}" placeholder="JSON"></textarea>`;
  else input = `<input name="${esc(name)}" ${p.type === "int" || p.type === "float" ? `type="number" step="${p.type === "int" ? (p.multiple_of || 1) : "any"}"` : 'type="text"'}${p.required ? " required" : ""}>`;
  return `<label class="field">${esc(label)}${input}<span class="hint">${esc(hint)}</span></label>`;
}

function buildForm() {
  const s = services[$("#n-service").value];
  $("#n-result").innerHTML = "";
  if (!s) { $("#n-form").innerHTML = ""; return; }
  $("#n-desc").textContent = s.description;
  const entries = Object.entries(s.params);
  const big = entries.filter(([, p]) => ["file", "files", "json"].includes(p.type) || (p.type === "string" && p.max_length > 600));
  const small = entries.filter((e) => !big.includes(e));
  $("#n-form").innerHTML = big.map(([k, p]) => fieldHtml(k, p)).join("") +
    `<div class="row2">${small.map(([k, p]) => fieldHtml(k, p)).join("")}</div><div><button type="submit">Enviar job</button></div>`;
}
$("#n-service").addEventListener("change", buildForm);
$("#n-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const s = services[$("#n-service").value];
  const form = new FormData();
  const params = {};
  for (const [name, p] of Object.entries(s.params)) {
    const el = $("#n-form").elements[name];
    if (!el) continue;
    if (p.type === "file" || p.type === "files") { for (const f of el.files) form.append(name, f); continue; }
    const v = el.value.trim();
    if (v === "") continue;
    if (p.type === "int" || p.type === "float") params[name] = Number(v);
    else if (p.type === "bool") params[name] = v === "true";
    else if (p.type === "json") { try { params[name] = JSON.parse(v); } catch { $("#n-result").innerHTML = `<div class="notice err">${esc(name)}: JSON inválido</div>`; return; } }
    else if (p.type === "enum") params[name] = (p.values || []).find((x) => String(x) === v) ?? v;
    else params[name] = v;
  }
  form.append("job", JSON.stringify({ service: s.id, params }));
  const btn = ev.submitter; if (btn) btn.disabled = true;
  try {
    const r = await api("/api/v1/jobs", { method: "POST", body: form });
    $("#n-result").innerHTML = `<div class="notice">Job aceito (estimativa ${esc(fmtDur(r.estimate_s))}). <button class="link" data-open="${esc(r.id)}">Acompanhar</button></div>`;
    pollStatus();
  } catch (e) {
    const busy = e.status === 409 ? ` Tente de novo em ${fmtDur(Number(e.retryAfter))}.` : "";
    $("#n-result").innerHTML = `<div class="notice err">${esc(e.message)}${esc(busy)}</div>`;
  } finally { if (btn) btn.disabled = false; }
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
      <td>${esc(fmtDur(r.duration_s))}</td><td>${esc(r.client || "")}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">Nenhuma requisição.</td></tr>`;
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
      <h3>Jobs em paralelo — ${esc(name.toUpperCase())}</h3>
      <p class="muted">${esc(r.description || "")}. Quantos jobs podem usar este recurso ao mesmo tempo. Cada job novo
        só começa se houver uma vaga livre <b>e</b> memória livre suficiente para o modelo dele (a memória do serviço
        mais ${esc(r.memory_margin_gb)} GB de margem, descontando o que os jobs ainda carregando vão ocupar). Sem isso, a
        resposta é 409 e o pedido pode ser repetido depois. Mais de um job ao mesmo tempo divide a GPU: cada um fica mais lento.</p>
      <div class="segmented" data-resource="${esc(name)}">${Array.from({ length: r.max }, (_, i) => i + 1).map((n) =>
        `<button type="button" data-slots="${n}" class="${n === r.slots ? "active" : ""}"${me?.admin ? "" : " disabled"}>${n}</button>`).join("")}</div>
      ${me?.admin ? "" : `<div class="muted" style="font-size:13px">Só uma chave de administrador muda este ajuste.</div>`}
      <div class="muted" style="font-size:13px">Padrão do daemon.yaml: ${esc(r.default)} · máximo ${esc(r.max)}</div>
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
      <div class="gauge"><b>${r.used}/${r.slots}</b><span>vagas em uso</span><div class="slots">${dots}</div></div>
      <div class="gauge"><b>${esc(lastStatus.memory_available_gb)} GB</b><span>memória livre agora</span></div>
      <div class="gauge"><b>${esc(r.reserved_loading_gb)} GB</b><span>reservados por jobs carregando</span></div>
      <div class="gauge"><b>${esc(r.holders.length ? holderNames(r) : "—")}</b><span>usando agora</span></div>`;
  }
}
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest(".segmented button[data-slots]");
  if (!b) return;
  const resource = b.parentElement.dataset.resource;
  const msg = document.getElementById(`settings-msg-${resource}`);
  try {
    await api("/api/v1/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resources: { [resource]: { slots: Number(b.dataset.slots) } } }) });
    msg.textContent = `Salvo: até ${b.dataset.slots} job(s) em paralelo. Vale para os próximos pedidos.`;
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
    <button type="button" class="secondary small" data-copy="${reg(text)}">Copiar</button></div><pre>${esc(text)}</pre></div>`;
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
  try { await copyText(text); b.textContent = "Copiado ✓"; }
  catch {
    b.textContent = "Selecione e copie";
    const pre = b.closest(".code")?.querySelector("pre");
    if (pre) { const r = document.createRange(); r.selectNodeContents(pre); getSelection().removeAllRanges(); getSelection().addRange(r); }
  }
  clearTimeout(b._t); b._t = setTimeout(() => { b.textContent = b.dataset.label; }, 1600);
});

// ---------------------------------------------------------------- access: keys and agent onboarding

let connectInfo = null;
let keysById = {};
let connectState = null;
const ROLE_PT = { agent: "agente", admin: "admin" };

async function loadAccess() {
  try { connectInfo = await api("/api/v1/connect"); } catch { return; }
  $("#local-url").textContent = connectInfo.local_url;
  const rows = [["URL base", connectInfo.base_url], ["MCP (streamable HTTP)", connectInfo.mcp_url],
    ["OpenAPI", connectInfo.openapi_url], ["Skill (SKILL.md)", connectInfo.skill_url],
    ...connectInfo.proxies.map((p) => [p.name, p.base_url])];
  $("#endpoints").innerHTML = rows.map(([k, v]) => `<div class="endpoint"><span class="muted">${esc(k)}</span>
    <code>${esc(v)}</code><button type="button" class="secondary small" data-copy="${reg(v)}">Copiar</button></div>`).join("");
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
    const role = own ? `<select data-key-role="${esc(k.id)}" aria-label="Papel">${data.roles.map((r) =>
      `<option value="${r}"${r === k.role ? " selected" : ""}>${ROLE_PT[r] || r}</option>`).join("")}</select>` : esc(ROLE_PT[k.role] || k.role);
    const used = k.last_used_at ? `${fmtTime(k.last_used_at)}${k.last_ip ? " · " + k.last_ip : ""}` : "nunca";
    const actions = `<button type="button" class="secondary small" data-connect="${esc(k.id)}">Conectar</button>` + (own
      ? `<button type="button" class="secondary small" data-key-toggle="${esc(k.id)}">${k.disabled ? "Ativar" : "Desativar"}</button>
         <button type="button" class="danger small" data-key-revoke="${esc(k.id)}">Revogar</button>`
      : `<span class="muted">gerida no oMLX</span>`);
    return `<tr class="${k.disabled ? "off" : ""}">
      <td><b>${esc(k.name)}</b>${k.disabled ? ` <span class="chip">desativada</span>` : ""}${me.key_id === k.id ? ` <span class="chip loaded">você</span>` : ""}</td>
      <td>${role}</td><td>${own ? "openField" : "oMLX"}</td><td><code>${esc(k.prefix ? k.prefix + "…" : "—")}</code></td>
      <td>${esc(fmtTime(k.created_at))}</td><td>${esc(used)}</td><td><div class="row-actions">${actions}</div></td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">Nenhuma chave ainda.</td></tr>`;
}

function keyMsg(text, err) { $("#key-msg").innerHTML = text ? `<div class="notice${err ? " err" : ""}">${esc(text)}</div>` : ""; }

$("#key-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = ev.submitter; if (btn) btn.disabled = true;
  try {
    const r = await api("/api/v1/keys", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("#key-name").value, role: $("#key-role").value }) });
    $("#key-name").value = ""; keyMsg("");
    await loadKeys();
    openConnect(r, r.key);
  } catch (e) { keyMsg(e.message, true); } finally { if (btn) btn.disabled = false; }
});

async function patchKey(id, body) {
  try {
    await api(`/api/v1/keys/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
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
    if (t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Confirmar revogação"; return; }
    try { await api(`/api/v1/keys/${t.dataset.keyRevoke}`, { method: "DELETE" }); keyMsg(""); } catch (e) { keyMsg(e.message, true); }
    loadKeys();
  }
  if (t.dataset.ctab && connectState) { connectState.tab = t.dataset.ctab; renderConnect(); }
});

async function openConnect(key, secret) {
  if (!connectInfo) { try { connectInfo = await api("/api/v1/connect"); } catch { return; } }
  openJobId = null; clearTimeout(drawerTimer);
  const state = { key, secret, inline: !!secret, tab: "mcp", skill: null };
  connectState = state;
  $("#d-title").textContent = key ? `Conectar — ${key.name}` : "Conectar um agente";
  $("#drawer").hidden = false;
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
  const literal = K ?? "<cole a chave aqui>";
  const head = st.secret
    ? `<div class="secret-box"><div><b>Chave criada.</b> Copie agora: ela não aparece de novo.</div>
        <div class="secret"><code>${esc(st.secret)}</code><button type="button" data-copy="${reg(st.secret)}">Copiar chave</button></div>
        <label class="check"><input type="checkbox" id="inline-key"${st.inline ? " checked" : ""}> Incluir a chave nos exemplos abaixo</label></div>`
    : `<div class="notice">A chave não fica guardada no openField: os exemplos leem a variável de ambiente
        <code>OPENFIELD_KEY</code>. Defina-a na máquina do agente com o valor da chave${st.key?.prefix ? ` (começa com <code>${esc(st.key.prefix)}</code>)` : ""}.</div>`;
  const tabs = `<div class="segmented connect-tabs">${[["mcp", "MCP"], ["api", "API REST"], ["skill", "Skill"]].map(([id, label]) =>
    `<button type="button" data-ctab="${id}" class="${st.tab === id ? "active" : ""}">${label}</button>`).join("")}</div>`;
  let body = "";
  if (st.tab === "mcp") {
    const json = JSON.stringify({ mcpServers: { openfield: { type: "http", url: c.mcp_url, headers: { "X-Token": js } } } }, null, 2);
    body = `<p class="muted">O agente ganha as ferramentas do openField: <code>status</code>, <code>list_services</code>,
        <code>submit_job</code>, <code>wait_job</code>, <code>get_output</code>... Transporte streamable HTTP; o servidor também
        aceita <code>Authorization: Bearer</code>.</p>
      ${codeBlock("Claude Code (todos os projetos)", `claude mcp add --transport http --scope user openfield ${c.mcp_url} --header "X-Token: ${sh}"`)}
      ${codeBlock("JSON de configuração (formato mcpServers: .mcp.json, Cursor e outros clientes)", json)}
      ${K ? "" : `<p class="muted">No JSON, <code>\${OPENFIELD_KEY}</code> só funciona em clientes que expandem variáveis (o Claude Code expande); nos outros, ponha a chave no lugar.</p>`}`;
  } else if (st.tab === "api") {
    const ex = Object.values(services).find((s) => s.available && s.kind !== "http_proxy" && s.params.prompt &&
      !Object.values(s.params).some((p) => p.required && (p.type === "file" || p.type === "files")));
    const job = ex ? JSON.stringify({ service: ex.id, params: { prompt: "a lighthouse at dusk, photo" } }) : "";
    body = `<p class="muted">REST com JSON (multipart quando há arquivos). Esquema completo em
        <a href="${esc(c.openapi_url)}" target="_blank" rel="noopener">OpenAPI</a> e na
        <a href="${esc(c.docs_url)}" target="_blank" rel="noopener">documentação interativa</a>. Para um agente, a aba Skill entrega tudo isso pronto.</p>
      ${codeBlock("Variáveis de ambiente — macOS/Linux", `export OPENFIELD_URL=${c.base_url}\nexport OPENFIELD_KEY=${literal}`)}
      ${codeBlock("Variáveis de ambiente — PowerShell", `$env:OPENFIELD_URL = "${c.base_url}"\n$env:OPENFIELD_KEY = "${literal}"`)}
      ${codeBlock("Teste", `curl -s -H "X-Token: ${sh}" ${c.base_url}/api/v1/status`)}
      ${ex ? codeBlock(`Enviar um job (${ex.name})`, `curl -s -H "X-Token: ${sh}" -H 'Content-Type: application/json' \\\n  -d '${job}' ${c.base_url}/api/v1/jobs`) : ""}
      ${c.proxies.map((p) => codeBlock(`${p.name} — clientes OpenAI`, `export OPENAI_BASE_URL=${p.base_url}\nexport OPENAI_API_KEY=${K ?? "$OPENFIELD_KEY"}`)).join("")}`;
  } else {
    const install = (dir) => `mkdir -p ${dir}/openfield && curl -fsSL -H "X-Token: ${sh}" ${c.skill_url} -o ${dir}/openfield/SKILL.md`;
    const prompt = [`Configure o acesso ao openField, um servidor de serviços de IA na rede local (${c.base_url}).`,
      `1. A chave de acesso fica na variável de ambiente OPENFIELD_KEY${K ? `; o valor é ${K}` : " (peça o valor ao usuário)"}. ` +
        "Deixe-a definida para as próximas sessões (no perfil do shell) e não a grave em nenhum outro arquivo.",
      `2. Instale a skill: mkdir -p ~/.claude/skills/openfield && curl -fsSL -H "X-Token: $OPENFIELD_KEY" ${c.skill_url} -o ~/.claude/skills/openfield/SKILL.md`,
      "   (se o seu harness usa outra pasta de skills, salve o arquivo lá como openfield/SKILL.md).",
      `3. Teste: curl -s -H "X-Token: $OPENFIELD_KEY" ${c.base_url}/api/v1/status`,
      "Depois, use a skill sempre que precisar gerar ou editar imagens, gerar ou processar vídeos, ou chamar os LLMs locais."].join("\n");
    body = `<p class="muted">Uma skill no padrão Agent Skills (<code>SKILL.md</code>), gerada a partir dos serviços configurados agora:
        endereços, parâmetros de cada serviço, o fluxo da API e o que fazer com 409 e 503. Ela lê a chave da variável
        <code>OPENFIELD_KEY</code> e nunca a guarda no arquivo. Baixe de novo quando adicionar serviços.</p>
      ${codeBlock("Claude Code — macOS/Linux", install("~/.claude/skills"))}
      ${codeBlock("Claude Code — PowerShell", `New-Item -ItemType Directory -Force "$HOME\\.claude\\skills\\openfield" | Out-Null\nInvoke-RestMethod -Headers @{ "X-Token" = "${ps}" } ${c.skill_url} -OutFile "$HOME\\.claude\\skills\\openfield\\SKILL.md"`)}
      ${codeBlock("Pi — macOS/Linux", install("~/.pi/agent/skills"))}
      ${codeBlock("Para colar no chat de qualquer agente", prompt)}
      <div class="actions">
        <button type="button" class="secondary"${st.skill ? ` data-copy="${reg(st.skill)}"` : " disabled"}>Copiar SKILL.md</button>
        <a href="/api/v1/skill?download=true"><button type="button" class="secondary">Baixar SKILL.md</button></a>
      </div>
      <details class="preview"><summary>Prévia do SKILL.md</summary><pre>${esc(st.skill || "carregando…")}</pre></details>`;
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
