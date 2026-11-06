"use strict";
// The owner credential is held only in memory. All dynamic data is rendered as text nodes.
const state = { token: "", accounts: [], clients: [], sessions: {}, retention: null };
let sessionTimer = null;
let sessionGeneration = 0;
let idleTimer = null;
const IDLE_TIMEOUT_MS = 15 * 60 * 1000;
const $ = (selector) => document.querySelector(selector);
const make = (tag, className, value) => { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = value; return node; };
const pathPart = (value) => encodeURIComponent(value);
const notice = (text, isError = false) => { const el = $("#notice"); el.textContent = text; el.className = isError ? "notice error" : "notice"; el.hidden = false; };
const clearNotice = () => { $("#notice").hidden = true; };
async function request(path, options = {}) {
  const generation = sessionGeneration;
  const headers = { Authorization: `Bearer ${state.token}` };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method: options.method || "GET", headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), credentials: "omit", redirect: "error", cache: "no-store", referrerPolicy: "no-referrer" });
  let data; try { data = await response.json(); } catch { data = { error: "invalid_response" }; }
  if (generation !== sessionGeneration || !state.token) throw new Error("session ended");
  if (!response.ok) { const error = new Error(typeof data.error === "string" ? data.error : "request_failed"); error.status = response.status; throw error; }
  return data;
}
function report(error) { if (error.status === 401 && state.token) { lockWorkspace("Your owner session is no longer valid. Sign in again."); return; } notice(`Request failed: ${error.message || "unavailable"}.`, true); }
function showView(view) {
  document.querySelectorAll(".view").forEach((element) => element.classList.toggle("active", element.id === `view-${view}`));
  document.querySelectorAll(".nav-item").forEach((element) => element.classList.toggle("active", element.dataset.view === view));
  const names = { overview: "Overview", accounts: "Accounts", clients: "Clients", policies: "Access policies", retention: "Retention", audit: "Audit trail" };
  $("#top-title").textContent = names[view] || "Overview";
  clearNotice();
}
document.querySelectorAll("[data-view], [data-go]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view || button.dataset.go)));
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); sessionGeneration += 1; state.token = $("#owner-token").value.trim(); $("#owner-token").value = ""; $("#login-error").textContent = "";
  try { await refreshAll(); $("#login").hidden = true; $("#app").hidden = false; sessionTimer = setInterval(() => refreshSessions().catch(report), 30000); resetIdleTimer(); }
  catch { state.token = ""; $("#login-error").textContent = "Unable to unlock the workspace. Check the credential and gateway."; }
});
function lockWorkspace(reason = "") {
  sessionGeneration += 1;
  state.token = ""; state.accounts = []; state.clients = []; state.sessions = {}; state.retention = null;
  clearInterval(sessionTimer); clearTimeout(idleTimer); sessionTimer = null; idleTimer = null;
  document.querySelectorAll("form").forEach((form) => form.reset());
  for (const selector of ["#account-list", "#client-list", "#recent-audit", "#audit-list", "#policy-client", "#policy-account"]) $(selector).replaceChildren();
  $("#stat-accounts").textContent = "0"; $("#stat-clients").textContent = "0";
  $("#policy-form").hidden = true; $("#issued-credential").value = ""; $("#credential-box").hidden = true;
  clearNotice(); $("#session-alert").hidden = true; $("#session-alert").textContent = ""; $("#app").hidden = true; $("#login").hidden = false; $("#login-error").textContent = reason; $("#owner-token").focus();
}
function resetIdleTimer() { if (!state.token) return; clearTimeout(idleTimer); idleTimer = setTimeout(() => lockWorkspace("Signed out after 15 minutes of inactivity."), IDLE_TIMEOUT_MS); }
for (const event of ["pointerdown", "keydown", "input"]) document.addEventListener(event, resetIdleTimer, { passive: true });
$("#logout").addEventListener("click", () => lockWorkspace());
$("#refresh").addEventListener("click", () => refreshAll().then(() => notice("Workspace refreshed.")).catch(report));
async function refreshAll() {
  const [accounts, clients, settings, audit, storage] = await Promise.all([request("/v1/admin/accounts"), request("/v1/admin/clients"), request("/v1/admin/settings"), request("/v1/admin/audit"), request("/v1/admin/storage-status")]);
  state.accounts = accounts.accounts; state.clients = clients.clients;
  $("#stat-accounts").textContent = String(state.accounts.length); $("#stat-clients").textContent = String(state.clients.filter((client) => client.enabled).length);
  renderAccounts(); renderClients(); populateSelectors(); renderSettings(settings); renderAudit(audit.events);
  $("#storage-status").textContent = storage.synthetic_persistent ? "Encrypted synthetic state is configured to persist across restarts. Backups are still required." : "Synthetic state is ephemeral: all messages and media are lost at restart, even without automatic expiry.";
  await refreshSessions();
}
async function refreshSessions() {
  const generation = sessionGeneration;
  const active = state.accounts.filter((account) => account.enabled);
  let statuses;
  try { statuses = await Promise.all(active.map((account) => request(`/v1/admin/accounts/${pathPart(account.id)}/status`))); }
  catch (error) {
    if (generation === sessionGeneration && state.token) { $("#session-alert").textContent = "Unable to verify account session status. Check the gateway and try again."; $("#session-alert").hidden = false; }
    throw error;
  }
  if (generation !== sessionGeneration || !state.token) return;
  state.sessions = Object.fromEntries(active.map((account, index) => [account.id, statuses[index]]));
  renderAccounts();
  const intervention = statuses.some((status) => status.intervention_required);
  const disconnected = statuses.some((status) => status.state !== "ready");
  $("#session-alert").textContent = intervention ? "A synthetic account session requires owner intervention. Review Accounts." : "A synthetic account session is disconnected. Review Accounts and reconnect if needed.";
  $("#session-alert").hidden = !(intervention || disconnected);
}
function addAction(parent, text, handler) { const button = make("button", "mini-button", text); button.type = "button"; button.addEventListener("click", () => handler().catch(report)); parent.append(button); }
function empty(parent, text) { parent.replaceChildren(make("div", "empty", text)); }
function renderAccounts() {
  const root = $("#account-list"); root.replaceChildren(); if (!state.accounts.length) { empty(root, "No accounts yet. Add a synthetic account to begin."); return; }
  for (const account of state.accounts) {
    const row = make("div", "list-row"), icon = make("div", "list-icon", "A"), main = make("div", "list-main"), actions = make("div", "list-actions");
    const session = state.sessions[account.id];
    main.append(make("strong", "", account.alias), make("small", "", `${account.phone_number} · ${session ? session.state : "not checked"} · ${account.id}`));
    const badge = make("span", `badge${account.enabled ? "" : " off"}`, account.enabled ? "ENABLED" : "DISABLED");
    addAction(actions, account.enabled ? "Disable" : "Enable", async () => { await request(`/v1/admin/accounts/${pathPart(account.id)}`, { method: "PATCH", body: { enabled: !account.enabled } }); await refreshAll(); notice("Account updated."); });
    if (account.enabled) {
      addAction(actions, "Session", async () => { const status = await request(`/v1/admin/accounts/${pathPart(account.id)}/status`); notice(`Session: ${status.state}${status.intervention_required ? " · owner intervention required" : ""}.`); });
      addAction(actions, "Reconnect", async () => { const status = await request(`/v1/admin/accounts/${pathPart(account.id)}/reconnect`, { method: "POST" }); await refreshSessions(); notice(`Session: ${status.state}${status.intervention_required ? " · owner intervention required" : ""}.`, status.intervention_required); });
    }
    row.append(icon, main, badge, actions); root.append(row);
  }
}
$("#account-form").addEventListener("submit", async (event) => { event.preventDefault(); try { await request("/v1/admin/accounts", { method: "POST", body: { alias: $("#account-alias").value, phone_number: $("#account-phone").value } }); event.target.reset(); await refreshAll(); notice("Synthetic account added."); } catch (error) { report(error); } });
function showCredential(value) { $("#issued-credential").value = value; $("#credential-box").hidden = false; showView("clients"); $("#issued-credential").focus(); $("#issued-credential").select(); }
$("#dismiss-credential").addEventListener("click", () => { $("#issued-credential").value = ""; $("#credential-box").hidden = true; });
function renderClients() {
  const root = $("#client-list"); root.replaceChildren(); if (!state.clients.length) { empty(root, "No clients yet. Create one to issue a credential."); return; }
  for (const client of state.clients) {
    const row = make("div", "list-row"), icon = make("div", "list-icon", "C"), main = make("div", "list-main"), actions = make("div", "list-actions");
    main.append(make("strong", "", client.name), make("small", "", client.id));
    const badge = make("span", `badge${client.enabled ? "" : " off"}`, client.enabled ? "ACTIVE" : "REVOKED");
    if (client.enabled) {
      addAction(actions, "Rotate", async () => { const result = await request(`/v1/admin/clients/${pathPart(client.id)}/rotate`, { method: "POST" }); showCredential(result.credential); notice("Previous credential revoked. Save the new one now."); });
      addAction(actions, "Revoke", async () => { await request(`/v1/admin/clients/${pathPart(client.id)}/revoke`, { method: "POST" }); await refreshAll(); notice("Client revoked."); });
    }
    row.append(icon, main, badge, actions); root.append(row);
  }
}
$("#client-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await request("/v1/admin/clients", { method: "POST", body: { name: $("#client-name").value } });
    event.target.reset();
    // The credential is available once: reveal it before any unrelated refresh can fail.
    showCredential(result.credential); notice("Client created with no access. Save its credential now.");
    try { await refreshAll(); } catch { notice("Client created. Save the credential; refreshing workspace data failed.", true); }
  } catch (error) { report(error); }
});
function populate(select, items, placeholder) { const previous = select.value; select.replaceChildren(); const initial = make("option", "", placeholder); initial.value = ""; select.append(initial); for (const item of items) { const option = make("option", "", item.name || item.alias); option.value = item.id; select.append(option); } if (items.some((item) => item.id === previous)) select.value = previous; }
function populateSelectors() { populate($("#policy-client"), state.clients.filter((client) => client.enabled), "Choose a client"); populate($("#policy-account"), state.accounts, "Choose an account"); }
const descriptions = { read: "Discovery, search, history and media access.", write: "Send, edit, delete, forward and media writes.", admin: "Telegram administrative operations only." };
function scopeCard(name) {
  const card = make("article", "panel scope-card"), heading = make("div", "scope-title"), title = make("h2", "", name[0].toUpperCase() + name.slice(1));
  heading.append(title, make("span", "stat-icon green", name === "read" ? "R" : name === "write" ? "W" : "A")); card.append(heading, make("p", "scope-desc", descriptions[name]));
  function check(field, label) { const row = make("label", "check-row"), box = document.createElement("input"); box.type = "checkbox"; box.id = `${name}-${field}`; row.append(box, document.createTextNode(label)); card.append(row); }
  function label(text) { card.append(make("label", "field-label", text)); }
  function area(field, placeholder) { const node = document.createElement("textarea"); node.id = `${name}-${field}`; node.rows = 3; node.placeholder = placeholder; node.setAttribute("aria-label", `${name} ${field.replaceAll("-", " ")}`); card.append(node); }
  check("enabled", "Enable permission"); label("ALLOW PUBLIC TYPES"); check("channels", "Public channels"); check("groups", "Public groups"); label("EXPLICIT CHAT IDS"); area("allow", "One chat ID per line"); label("EXCLUDE TYPES"); check("exclude-channels", "Public channels"); check("exclude-groups", "Public groups"); check("exclude-private-groups", "Private groups"); check("exclude-direct", "Private direct chats"); label("EXCLUDE CHAT IDS"); area("exclude", "One chat ID per line"); return card;
}
for (const name of ["read", "write", "admin"]) $("#scope-grid").append(scopeCard(name));
const list = (id) => $(id).value.split("\n").map((line) => line.trim()).filter(Boolean);
function readScope(name) { const allowed = [], excluded = []; if ($(`#${name}-channels`).checked) allowed.push("public_channel"); if ($(`#${name}-groups`).checked) allowed.push("public_group"); for (const [field, type] of [["channels", "public_channel"], ["groups", "public_group"], ["private-groups", "private_group"], ["direct", "private_direct"]]) if ($(`#${name}-exclude-${field}`).checked) excluded.push(type); return { enabled: $(`#${name}-enabled`).checked, allow_types: allowed, allow_chats: list(`#${name}-allow`), exclude_types: excluded, exclude_chats: list(`#${name}-exclude`) }; }
function fillScope(name, scope = {}) { $(`#${name}-enabled`).checked = !!scope.enabled; $(`#${name}-channels`).checked = (scope.allow_types || []).includes("public_channel"); $(`#${name}-groups`).checked = (scope.allow_types || []).includes("public_group"); for (const [field, type] of [["channels", "public_channel"], ["groups", "public_group"], ["private-groups", "private_group"], ["direct", "private_direct"]]) $(`#${name}-exclude-${field}`).checked = (scope.exclude_types || []).includes(type); $(`#${name}-allow`).value = (scope.allow_chats || []).join("\n"); $(`#${name}-exclude`).value = (scope.exclude_chats || []).join("\n"); }
function policyPath() { const client = $("#policy-client").value, account = $("#policy-account").value; if (!client || !account) throw new Error("choose a client and an account"); return `/v1/admin/clients/${pathPart(client)}/accounts/${pathPart(account)}/policy`; }
$("#load-policy").addEventListener("click", async () => { try { let data; try { data = await request(policyPath()); } catch (error) { if (error.status !== 404) throw error; data = {}; } for (const name of ["read", "write", "admin"]) fillScope(name, data[name]); $("#policy-form").hidden = false; $("#policy-state").textContent = "Policy loaded. Changes are not applied until saved."; clearNotice(); } catch (error) { report(error); } });
for (const id of ["#policy-client", "#policy-account"]) $(id).addEventListener("change", () => { $("#policy-form").hidden = true; $("#policy-state").textContent = "Selection changed. Load the policy before editing."; });
$("#policy-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const data = {}; for (const name of ["read", "write", "admin"]) data[name] = readScope(name); await request(policyPath(), { method: "PUT", body: data }); $("#policy-state").textContent = "Policy saved. Changes apply to subsequent requests immediately."; notice("Access policy saved."); } catch (error) { report(error); } });
function renderSettings(settings) { state.retention = settings; $("#audit-days").value = settings.audit_retention_days; $("#cache-seconds").value = settings.cache_ttl_seconds; $("#media-permanent").checked = settings.media_retention_days === null; $("#media-days").value = settings.media_retention_days || ""; $("#media-days").disabled = $("#media-permanent").checked; }
$("#media-permanent").addEventListener("change", () => { $("#media-days").disabled = $("#media-permanent").checked; });
$("#retention-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const media = $("#media-permanent").checked ? null : Number($("#media-days").value); if (media !== null && state.retention && (state.retention.media_retention_days === null || media < state.retention.media_retention_days) && !window.confirm("A shorter retention period may immediately delete expired synthetic media. Continue?")) return; const settings = await request("/v1/admin/settings", { method: "PUT", body: { audit_retention_days: Number($("#audit-days").value), cache_ttl_seconds: Number($("#cache-seconds").value), media_retention_days: media } }); renderSettings(settings); notice("Retention settings saved."); } catch (error) { report(error); } });
$("#prune-audit").addEventListener("click", async () => { if (!window.confirm("Permanently delete audit records older than the configured retention period?")) return; try { const result = await request("/v1/admin/maintenance/prune-audit", { method: "POST" }); notice(`Pruned ${result.deleted} expired audit records.`); } catch (error) { report(error); } });
function renderAudit(events) { for (const selector of ["#recent-audit", "#audit-list"]) { const root = $(selector); root.replaceChildren(); const subset = selector === "#recent-audit" ? events.slice(0, 5) : events; if (!subset.length) { empty(root, "No activity recorded yet."); continue; } for (const event of subset) { const row = make("div", "audit-row"); const decision = event.decision || "unknown"; row.append(make("span", "event-name", event.event.replaceAll("_", " ")), make("span", "event-time", event.timestamp), make("span", "event-actor", event.actor_kind + (event.actor_id ? ` · ${event.actor_id}` : "")), make("span", `badge${["failure", "denied"].includes(decision) ? " off" : ""}`, decision.toUpperCase())); root.append(row); } } }
$("#reload-audit").addEventListener("click", async () => { try { const result = await request("/v1/admin/audit"); renderAudit(result.events); notice("Audit trail refreshed."); } catch (error) { report(error); } });
