/**
 * Cloudflare Workers port of the license server + an embedded admin UI.
 *
 * Endpoints
 * ---------
 *   PUBLIC
 *     GET  /health             -> {"ok": true}
 *     POST /verify             -> license verdict matching the desktop
 *                                  app's current_entitlement() shape.
 *
 *   ADMIN (HTML)
 *     GET  /admin              -> the management UI (login if no cookie)
 *     POST /admin/login        -> form post, sets `admin_token` cookie
 *     POST /admin/logout       -> clears cookie, returns to login
 *
 *   ADMIN API (cookie OR X-Admin-Token header)
 *     GET    /admin/api/stats          -> {total, active, revoked, expired}
 *     GET    /admin/api/keys?cursor=&q=
 *                                       -> paginated key list (with values)
 *     GET    /admin/api/keys/<key>     -> single key
 *     POST   /admin/api/keys           -> create/upsert a key
 *     PUT    /admin/api/keys/<key>     -> update fields on an existing key
 *     DELETE /admin/api/keys/<key>     -> remove a key entirely
 *
 *   LEGACY (webhook-friendly)
 *     POST /admin/issue        -> same as POST /admin/api/keys but
 *                                  requires only the X-Admin-Token header
 *                                  (kept for backwards compatibility with
 *                                  any existing Stripe / Gumroad hooks).
 *
 * KV layout
 * ---------
 * Each license is one KV entry. The key is the license string the customer
 * pastes (e.g. "KFC-YUMAU-PROD-2026"); the value is JSON of the shape:
 *
 *   {
 *     "tenant_id": "<entra tenant guid>" | "*",
 *     "expires_at": "2027-12-31T00:00:00+00:00",
 *     "edition": "pro",
 *     "revoked": false,
 *     "note": "Yum! Australia",
 *     "created_at": "2026-06-16T11:39:00+00:00",   // set by admin UI
 *     "last_verified_at": "2026-06-16T11:42:00+00:00"  // set by /verify
 *   }
 *
 * The admin UI is a single self-contained HTML page (CSS + JS inline) so the
 * whole stack lives in this one file - no build step, no deps.
 */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    // --- public endpoints ---
    if (path === "/health") {
      return jsonResponse({ ok: true });
    }
    if (path === "/verify" && request.method === "POST") {
      return handleVerify(request, env);
    }

    // --- admin UI (HTML) ---
    if (path === "/admin/login" && request.method === "POST") {
      return handleAdminLogin(request, env);
    }
    if (path === "/admin/logout" && request.method === "POST") {
      return handleAdminLogout();
    }
    if (path === "/admin") {
      if (isAdminAuthenticated(request, env)) {
        return htmlResponse(ADMIN_DASHBOARD_HTML);
      }
      return htmlResponse(adminLoginHtml(""));
    }

    // --- admin API ---
    if (path.startsWith("/admin/api/")) {
      if (!isAdminAuthenticated(request, env)) {
        return jsonResponse({ ok: false, message: "Unauthorized." }, 401);
      }
      if (path === "/admin/api/stats" && request.method === "GET") {
        return adminStats(env);
      }
      if (path === "/admin/api/keys" && request.method === "GET") {
        return adminListKeys(request, env);
      }
      if (path === "/admin/api/keys" && request.method === "POST") {
        return adminUpsertKey(request, env, /*replaceAll=*/ false);
      }
      const m = path.match(/^\/admin\/api\/keys\/(.+)$/);
      if (m) {
        const key = decodeURIComponent(m[1]);
        if (request.method === "GET") return adminGetKey(env, key);
        if (request.method === "PUT") return adminUpdateKey(request, env, key);
        if (request.method === "DELETE") return adminDeleteKey(env, key);
      }
      return jsonResponse({ ok: false, message: "Not found." }, 404);
    }

    // --- legacy webhook-friendly endpoint ---
    if (path === "/admin/issue" && request.method === "POST") {
      const token = request.headers.get("X-Admin-Token") || "";
      if (!env.ADMIN_TOKEN || token !== env.ADMIN_TOKEN) {
        return jsonResponse({ ok: false, message: "Unauthorized." }, 401);
      }
      return adminUpsertKey(request, env, /*replaceAll=*/ true);
    }

    return jsonResponse({ ok: false, message: "Not found." }, 404);
  },
};


// =====================================================================
// /verify  (public)
// =====================================================================

async function handleVerify(request, env) {
  let body;
  try {
    body = await request.json();
  } catch (e) {
    return jsonResponse({ ok: false, message: "Invalid JSON body." });
  }
  const key = ((body && body.key) || "").trim();
  const tenantId = ((body && body.tenant_id) || "").trim();
  if (!key) {
    return jsonResponse({ ok: false, message: "No key provided." });
  }

  const raw = await env.LICENSE_KEYS.get(key);
  if (!raw) {
    return jsonResponse({ ok: false, message: "Unknown license key." });
  }
  let entry;
  try {
    entry = JSON.parse(raw);
  } catch (e) {
    return jsonResponse({
      ok: false,
      message: "License entry is corrupted - contact support.",
    });
  }

  if (entry.revoked) {
    return jsonResponse({
      ok: false,
      message: "This license has been revoked.",
    });
  }

  const allowedTenant = entry.tenant_id || "*";
  if (allowedTenant !== "*" && allowedTenant !== tenantId) {
    return jsonResponse({
      ok: false,
      message: "This license is not authorized for your tenant.",
    });
  }

  const expiresAt = entry.expires_at || "";
  if (expiresAt && new Date(expiresAt) < new Date()) {
    return jsonResponse({
      ok: false,
      expires_at: expiresAt,
      edition: entry.edition || "pro",
      message: "This license has expired.",
    });
  }

  // Best-effort: stamp the last_verified_at so the admin page shows
  // recent usage. Failures here must NOT block the verdict.
  try {
    entry.last_verified_at = isoNow();
    await env.LICENSE_KEYS.put(key, JSON.stringify(entry));
  } catch (e) { /* ignore */ }

  return jsonResponse({
    ok: true,
    expires_at: expiresAt,
    edition: entry.edition || "pro",
    message: entry.note || "Valid.",
  });
}


// =====================================================================
// Admin auth (cookie-based for HTML, header-based for API)
// =====================================================================

const COOKIE_NAME = "kfc_admin_token";
const COOKIE_MAX_AGE = 60 * 60 * 24;  // 24 hours

function isAdminAuthenticated(request, env) {
  if (!env.ADMIN_TOKEN) return false;
  // Header path - useful for curl / webhook calls.
  const headerToken = request.headers.get("X-Admin-Token");
  if (headerToken && headerToken === env.ADMIN_TOKEN) return true;
  // Cookie path - set after a successful /admin/login.
  const cookie = request.headers.get("Cookie") || "";
  const match = cookie.match(new RegExp("(?:^|; )" + COOKIE_NAME + "=([^;]+)"));
  if (match && decodeURIComponent(match[1]) === env.ADMIN_TOKEN) return true;
  return false;
}

async function handleAdminLogin(request, env) {
  const form = await request.formData();
  const token = (form.get("token") || "").toString().trim();
  if (!env.ADMIN_TOKEN) {
    return htmlResponse(
      adminLoginHtml(
        "Admin is disabled on this Worker. Set ADMIN_TOKEN as a secret " +
        "(Settings -> Variables and Secrets) to enable it."
      ),
      401
    );
  }
  if (!token || token !== env.ADMIN_TOKEN) {
    return htmlResponse(adminLoginHtml("Wrong admin token."), 401);
  }
  return new Response(null, {
    status: 302,
    headers: {
      "Location": "/admin",
      "Set-Cookie": [
        COOKIE_NAME + "=" + encodeURIComponent(token),
        "HttpOnly",
        "Secure",
        "SameSite=Strict",
        "Path=/admin",
        "Max-Age=" + COOKIE_MAX_AGE,
      ].join("; "),
    },
  });
}

function handleAdminLogout() {
  return new Response(null, {
    status: 302,
    headers: {
      "Location": "/admin",
      "Set-Cookie": COOKIE_NAME + "=; Path=/admin; Max-Age=0",
    },
  });
}


// =====================================================================
// Admin API
// =====================================================================

async function adminStats(env) {
  const list = await env.LICENSE_KEYS.list();
  let total = 0, active = 0, revoked = 0, expired = 0;
  const now = new Date();
  // KV's list() returns up to 1000 entries; for a license server that's
  // plenty. If the customer base outgrows that, paginate by cursor.
  const reads = await Promise.all(
    list.keys.map((k) => env.LICENSE_KEYS.get(k.name).then((raw) => [k.name, raw]))
  );
  for (const [name, raw] of reads) {
    if (!raw) continue;
    total += 1;
    let e; try { e = JSON.parse(raw); } catch (err) { continue; }
    if (e.revoked) { revoked += 1; continue; }
    if (e.expires_at && new Date(e.expires_at) < now) { expired += 1; continue; }
    active += 1;
  }
  return jsonResponse({ ok: true, total, active, revoked, expired });
}

async function adminListKeys(request, env) {
  const url = new URL(request.url);
  const cursor = url.searchParams.get("cursor") || undefined;
  const q = (url.searchParams.get("q") || "").trim().toLowerCase();
  const list = await env.LICENSE_KEYS.list({ cursor });
  const reads = await Promise.all(
    list.keys.map((k) => env.LICENSE_KEYS.get(k.name).then((raw) => [k.name, raw]))
  );
  const entries = [];
  for (const [name, raw] of reads) {
    if (!raw) continue;
    let value; try { value = JSON.parse(raw); } catch (e) { continue; }
    if (q) {
      const hay = (
        name + " " + (value.tenant_id || "") + " " + (value.edition || "") +
        " " + (value.note || "")
      ).toLowerCase();
      if (!hay.includes(q)) continue;
    }
    entries.push({ key: name, ...value });
  }
  // Newest first by created_at if present, otherwise alphabetical.
  entries.sort((a, b) => {
    const ca = a.created_at || "", cb = b.created_at || "";
    if (ca && cb) return cb.localeCompare(ca);
    return a.key.localeCompare(b.key);
  });
  return jsonResponse({
    ok: true,
    entries,
    cursor: list.list_complete ? null : list.cursor,
  });
}

async function adminGetKey(env, key) {
  const raw = await env.LICENSE_KEYS.get(key);
  if (!raw) return jsonResponse({ ok: false, message: "Not found." }, 404);
  let value; try { value = JSON.parse(raw); } catch (e) { value = {}; }
  return jsonResponse({ ok: true, key, ...value });
}

async function adminUpsertKey(request, env, replaceAll) {
  let body;
  try { body = await request.json(); } catch (e) {
    return jsonResponse({ ok: false, message: "Invalid JSON body." }, 400);
  }
  let key = (body.key || "").trim();
  if (!key) key = generateLicenseKey();
  const entry = {
    tenant_id: body.tenant_id || "*",
    expires_at: body.expires_at || "",
    edition: body.edition || "pro",
    revoked: Boolean(body.revoked),
    note: body.note || "",
    created_at: isoNow(),
  };
  if (!replaceAll) {
    // Preserve last_verified_at across upserts so admin actions don't
    // wipe out usage info.
    const existing = await env.LICENSE_KEYS.get(key);
    if (existing) {
      try {
        const e = JSON.parse(existing);
        if (e.last_verified_at) entry.last_verified_at = e.last_verified_at;
        if (e.created_at) entry.created_at = e.created_at;
      } catch (e) { /* ignore */ }
    }
  }
  await env.LICENSE_KEYS.put(key, JSON.stringify(entry));
  return jsonResponse({ ok: true, key, ...entry });
}

async function adminUpdateKey(request, env, key) {
  let body;
  try { body = await request.json(); } catch (e) {
    return jsonResponse({ ok: false, message: "Invalid JSON body." }, 400);
  }
  const existing = await env.LICENSE_KEYS.get(key);
  if (!existing) {
    return jsonResponse({ ok: false, message: "Not found." }, 404);
  }
  let entry; try { entry = JSON.parse(existing); } catch (e) { entry = {}; }
  // Apply partial updates - only the fields the client sent.
  for (const f of ["tenant_id", "expires_at", "edition", "revoked", "note"]) {
    if (body[f] !== undefined) entry[f] = body[f];
  }
  await env.LICENSE_KEYS.put(key, JSON.stringify(entry));
  return jsonResponse({ ok: true, key, ...entry });
}

async function adminDeleteKey(env, key) {
  const existed = await env.LICENSE_KEYS.get(key);
  if (!existed) {
    return jsonResponse({ ok: false, message: "Not found." }, 404);
  }
  await env.LICENSE_KEYS.delete(key);
  return jsonResponse({ ok: true, key });
}


// =====================================================================
// Helpers
// =====================================================================

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}

function htmlResponse(html, status = 200) {
  return new Response(html, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function isoNow() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function generateLicenseKey() {
  // KFC-XXXX-XXXX-XXXX, A-Z + 0-9, cryptographically random.
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  const groups = [];
  for (let g = 0; g < 3; g++) {
    let s = "";
    for (let i = 0; i < 4; i++) {
      s += alphabet[bytes[g * 4 + i] % alphabet.length];
    }
    groups.push(s);
  }
  return "KFC-" + groups.join("-");
}


// =====================================================================
// HTML - login + dashboard
// =====================================================================

function adminLoginHtml(errorMessage) {
  const err = errorMessage
    ? `<div class="err">${escapeHtml(errorMessage)}</div>`
    : "";
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>License admin - sign in</title>
<style>${BASE_CSS}</style>
</head><body>
<main class="login-shell">
  <div class="brand">
    <div class="brand-mark">KFC</div>
    <div class="brand-title">Entra Manager &middot; License admin</div>
  </div>
  ${err}
  <form method="post" action="/admin/login" class="login-form">
    <label>
      <span>Admin token</span>
      <input type="password" name="token" autocomplete="current-password" autofocus required>
    </label>
    <button type="submit" class="btn btn-primary">Sign in</button>
    <p class="muted small">
      The admin token is the secret you set on the Worker via
      <code>Settings &rarr; Variables and Secrets &rarr; ADMIN_TOKEN</code>.
    </p>
  </form>
</main>
</body></html>`;
}

const BASE_CSS = `
  :root {
    --kfc-red: #e4002b;
    --kfc-red-dark: #b80022;
    --ink: #1d1d1f;
    --muted: #6e6e73;
    --bg: #f5f5f7;
    --line: #d2d2d7;
    --ok: #178b3a;
    --warn: #ad6c00;
    --err: #c8102e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--ink);
    background: var(--bg);
    font-size: 14px;
  }
  a { color: var(--kfc-red); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; background: #eee; padding: 1px 5px; border-radius: 3px; }
  hr { border: none; border-top: 1px solid var(--line); margin: 18px 0; }

  .btn {
    display: inline-block;
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 14px;
    border: 1px solid var(--line);
    background: white;
    color: var(--ink);
    cursor: pointer;
    font-weight: 500;
  }
  .btn:hover { background: #fafafa; }
  .btn-primary {
    background: var(--kfc-red);
    color: white;
    border-color: var(--kfc-red);
  }
  .btn-primary:hover { background: var(--kfc-red-dark); }
  .btn-danger { color: var(--err); border-color: #f3c1c8; }
  .btn-danger:hover { background: #fbe6e2; }
  .btn-tiny { padding: 4px 10px; font-size: 12px; }
  .btn[disabled] { opacity: 0.5; cursor: not-allowed; }

  /* login */
  .login-shell {
    max-width: 380px;
    margin: 80px auto;
    background: white;
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 32px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.06);
  }
  .brand { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
  .brand-mark {
    width: 44px; height: 44px;
    background: var(--kfc-red); color: white;
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; letter-spacing: 1px;
  }
  .brand-title { font-weight: 600; font-size: 16px; }
  .login-form label { display: block; margin-bottom: 14px; }
  .login-form span { display: block; margin-bottom: 6px; font-weight: 500; }
  .login-form input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 14px;
  }
  .login-form input:focus { outline: 2px solid var(--kfc-red); outline-offset: 1px; }
  .login-form .btn { width: 100%; }
  .err {
    background: #fbe6e2; color: var(--err);
    padding: 10px 12px; border-radius: 8px;
    margin-bottom: 16px;
  }

  /* dashboard */
  .topbar {
    background: var(--kfc-red); color: white;
    padding: 12px 24px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .topbar .brand-title { color: white; }
  .topbar .brand-mark { background: white; color: var(--kfc-red); }
  .topbar .who { font-size: 13px; opacity: 0.85; margin-right: 12px; }
  .topbar .btn { background: rgba(255,255,255,0.18); color: white; border-color: transparent; }
  .topbar .btn:hover { background: rgba(255,255,255,0.28); }

  .container { max-width: 1200px; margin: 24px auto; padding: 0 24px; }

  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat {
    background: white;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 14px 18px;
  }
  .stat-num { font-size: 26px; font-weight: 600; }
  .stat-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .stat.ok .stat-num { color: var(--ok); }
  .stat.warn .stat-num { color: var(--warn); }
  .stat.err .stat-num { color: var(--err); }

  .toolbar {
    display: flex; gap: 10px; align-items: center;
    margin-bottom: 14px;
  }
  .toolbar input[type=search] {
    flex: 1;
    padding: 9px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 14px;
  }
  .toolbar input[type=search]:focus { outline: 2px solid var(--kfc-red); outline-offset: 1px; }

  .table-wrap { background: white; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }
  th { background: #fafafa; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; font-size: 11px; }
  tr:last-child td { border-bottom: none; }
  .empty { padding: 60px 24px; text-align: center; color: var(--muted); }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 500;
    background: #eee;
    color: var(--muted);
  }
  .badge.active { background: #d6f5e0; color: var(--ok); }
  .badge.expired { background: #fde7c8; color: var(--warn); }
  .badge.revoked { background: #fbe6e2; color: var(--err); }

  .row-actions { white-space: nowrap; }
  .row-actions .btn { margin-left: 4px; }

  .modal-backdrop {
    position: fixed; inset: 0;
    background: rgba(15, 15, 17, 0.45);
    display: none;
    align-items: center; justify-content: center;
    z-index: 100;
  }
  .modal-backdrop.show { display: flex; }
  .modal {
    background: white;
    border-radius: 14px;
    padding: 24px;
    width: 100%;
    max-width: 560px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.2);
  }
  .modal h2 { margin-top: 0; margin-bottom: 4px; }
  .modal .subtitle { color: var(--muted); margin-bottom: 18px; font-size: 13px; }
  .field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
  .field label { font-weight: 500; font-size: 13px; }
  .field small { color: var(--muted); font-weight: 400; }
  .field input, .field select, .field textarea {
    padding: 9px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
  }
  .field input:focus, .field select:focus, .field textarea:focus {
    outline: 2px solid var(--kfc-red); outline-offset: 1px;
  }
  .field .checkbox-row {
    display: flex; align-items: center; gap: 8px;
  }
  .modal-actions {
    display: flex; justify-content: flex-end; gap: 8px;
    margin-top: 18px;
  }

  .toast-stack { position: fixed; top: 16px; right: 16px; z-index: 200; display: flex; flex-direction: column; gap: 8px; }
  .toast {
    background: var(--ink); color: white;
    padding: 10px 16px;
    border-radius: 8px;
    box-shadow: 0 6px 18px rgba(0,0,0,0.2);
    animation: toast-in 0.2s ease;
    max-width: 320px;
    font-size: 13px;
  }
  .toast.ok { background: var(--ok); }
  .toast.err { background: var(--err); }
  .toast.warn { background: var(--warn); }
  @keyframes toast-in {
    from { transform: translateY(-10px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }
`;

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

const ADMIN_DASHBOARD_HTML = `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>License admin</title>
<style>${BASE_CSS}</style>
</head><body>
<header class="topbar">
  <div style="display:flex; align-items:center; gap:12px;">
    <div class="brand-mark">KFC</div>
    <div class="brand-title">Entra Manager &middot; License admin</div>
  </div>
  <div>
    <span class="who">signed in</span>
    <form method="post" action="/admin/logout" style="display:inline;">
      <button type="submit" class="btn btn-tiny">Sign out</button>
    </form>
  </div>
</header>

<main class="container">
  <div class="stats">
    <div class="stat"><div class="stat-num" id="stat-total">&mdash;</div><div class="stat-label">Total keys</div></div>
    <div class="stat ok"><div class="stat-num" id="stat-active">&mdash;</div><div class="stat-label">Active</div></div>
    <div class="stat warn"><div class="stat-num" id="stat-expired">&mdash;</div><div class="stat-label">Expired</div></div>
    <div class="stat err"><div class="stat-num" id="stat-revoked">&mdash;</div><div class="stat-label">Revoked</div></div>
  </div>

  <div class="toolbar">
    <input type="search" id="search" placeholder="Search by key, tenant, edition, note...">
    <button class="btn" id="refresh-btn">Refresh</button>
    <button class="btn btn-primary" id="new-btn">+ New key</button>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Key</th>
          <th>Tenant</th>
          <th>Edition</th>
          <th>Expires</th>
          <th>Status</th>
          <th>Note</th>
          <th>Last verified</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="rows">
        <tr><td colspan="8" class="empty">Loading&hellip;</td></tr>
      </tbody>
    </table>
  </div>
</main>

<div class="modal-backdrop" id="modal-back">
  <div class="modal">
    <h2 id="modal-title">New license key</h2>
    <div class="subtitle" id="modal-subtitle">Leave the key blank to auto-generate.</div>
    <form id="key-form">
      <input type="hidden" name="originalKey" id="originalKey" value="">
      <div class="field">
        <label>License key
          <small>blank = auto-generate (KFC-XXXX-XXXX-XXXX)</small>
        </label>
        <input type="text" name="key" id="f-key" placeholder="auto-generate">
      </div>
      <div class="field">
        <label>Tenant ID
          <small>* = works in any tenant; otherwise paste the Entra tenant GUID</small>
        </label>
        <input type="text" name="tenant_id" id="f-tenant" value="*">
      </div>
      <div class="field">
        <label>Expires at
          <small>(date - the time is set to 23:59 UTC of the chosen day)</small>
        </label>
        <input type="date" name="expires_at" id="f-expires">
      </div>
      <div class="field">
        <label>Edition</label>
        <input type="text" name="edition" id="f-edition" value="pro" list="edition-options">
        <datalist id="edition-options">
          <option value="pro"></option>
          <option value="enterprise"></option>
          <option value="trial-extension"></option>
        </datalist>
      </div>
      <div class="field">
        <label>Note <small>(internal - customer name, invoice ref, etc.)</small></label>
        <textarea name="note" id="f-note" rows="2"></textarea>
      </div>
      <div class="field">
        <label class="checkbox-row">
          <input type="checkbox" name="revoked" id="f-revoked">
          <span>Revoked</span>
        </label>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" id="modal-cancel">Cancel</button>
        <button type="submit" class="btn btn-primary" id="modal-save">Save</button>
      </div>
    </form>
  </div>
</div>

<div class="toast-stack" id="toasts"></div>

<script>
(function () {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  function toast(msg, kind) {
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || 'ok');
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toISOString().slice(0, 10);
  }
  function fmtDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toISOString().replace('T', ' ').replace(/\\..*$/, '') + ' UTC';
  }
  function statusOf(entry) {
    if (entry.revoked) return ['revoked', 'Revoked'];
    if (entry.expires_at && new Date(entry.expires_at) < new Date()) return ['expired', 'Expired'];
    return ['active', 'Active'];
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  let allEntries = [];
  let nextCursor = null;
  let searchQuery = '';

  async function fetchJson(url, opts) {
    const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
    let data;
    try { data = await r.json(); }
    catch (e) { throw new Error('Non-JSON response (HTTP ' + r.status + ')'); }
    if (!r.ok || data.ok === false) {
      throw new Error(data.message || ('HTTP ' + r.status));
    }
    return data;
  }

  async function loadStats() {
    try {
      const s = await fetchJson('/admin/api/stats');
      $('#stat-total').textContent = s.total;
      $('#stat-active').textContent = s.active;
      $('#stat-expired').textContent = s.expired;
      $('#stat-revoked').textContent = s.revoked;
    } catch (e) {
      toast('Stats failed: ' + e.message, 'err');
    }
  }

  async function loadKeys() {
    $('#rows').innerHTML = '<tr><td colspan="8" class="empty">Loading…</td></tr>';
    try {
      const q = encodeURIComponent(searchQuery);
      const data = await fetchJson('/admin/api/keys?q=' + q);
      allEntries = data.entries;
      nextCursor = data.cursor;
      renderRows();
    } catch (e) {
      $('#rows').innerHTML = '<tr><td colspan="8" class="empty">' + esc(e.message) + '</td></tr>';
      toast('Load failed: ' + e.message, 'err');
    }
  }

  function renderRows() {
    if (!allEntries.length) {
      $('#rows').innerHTML = '<tr><td colspan="8" class="empty">No keys yet. Click "+ New key" to issue one.</td></tr>';
      return;
    }
    $('#rows').innerHTML = allEntries.map((e) => {
      const [cls, label] = statusOf(e);
      const isRevoked = !!e.revoked;
      return (
        '<tr>' +
          '<td><span class="mono">' + esc(e.key) + '</span></td>' +
          '<td><span class="mono">' + esc(e.tenant_id || '*') + '</span></td>' +
          '<td>' + esc(e.edition || 'pro') + '</td>' +
          '<td>' + esc(fmtDate(e.expires_at)) + '</td>' +
          '<td><span class="badge ' + cls + '">' + label + '</span></td>' +
          '<td>' + esc(e.note || '') + '</td>' +
          '<td class="mono small">' + esc(fmtDateTime(e.last_verified_at)) + '</td>' +
          '<td class="row-actions">' +
            '<button class="btn btn-tiny" data-edit="' + esc(e.key) + '">Edit</button>' +
            '<button class="btn btn-tiny" data-toggle="' + esc(e.key) + '">' +
              (isRevoked ? 'Restore' : 'Revoke') + '</button>' +
            '<button class="btn btn-tiny btn-danger" data-delete="' + esc(e.key) + '">Delete</button>' +
          '</td>' +
        '</tr>'
      );
    }).join('');

    $$('[data-edit]').forEach((b) => b.addEventListener('click', () => openEdit(b.dataset.edit)));
    $$('[data-toggle]').forEach((b) => b.addEventListener('click', () => toggleRevoke(b.dataset.toggle)));
    $$('[data-delete]').forEach((b) => b.addEventListener('click', () => deleteKey(b.dataset.delete)));
  }

  // --- modal ---
  function openModal(title, subtitle) {
    $('#modal-title').textContent = title;
    $('#modal-subtitle').textContent = subtitle;
    $('#modal-back').classList.add('show');
    setTimeout(() => $('#f-key').focus(), 50);
  }
  function closeModal() {
    $('#modal-back').classList.remove('show');
    $('#key-form').reset();
    $('#originalKey').value = '';
    $('#f-key').readOnly = false;
  }
  $('#modal-back').addEventListener('click', (e) => { if (e.target.id === 'modal-back') closeModal(); });
  $('#modal-cancel').addEventListener('click', closeModal);

  function openNew() {
    $('#originalKey').value = '';
    $('#f-key').value = '';
    $('#f-key').readOnly = false;
    $('#f-tenant').value = '*';
    // Default expiry: 1 year from today.
    const d = new Date(); d.setFullYear(d.getFullYear() + 1);
    $('#f-expires').value = d.toISOString().slice(0, 10);
    $('#f-edition').value = 'pro';
    $('#f-note').value = '';
    $('#f-revoked').checked = false;
    openModal('New license key', 'Leave the key blank to auto-generate.');
  }

  function openEdit(key) {
    const e = allEntries.find((x) => x.key === key);
    if (!e) return;
    $('#originalKey').value = e.key;
    $('#f-key').value = e.key;
    $('#f-key').readOnly = true;  // can't rename a key without delete+recreate
    $('#f-tenant').value = e.tenant_id || '*';
    $('#f-expires').value = e.expires_at ? e.expires_at.slice(0, 10) : '';
    $('#f-edition').value = e.edition || 'pro';
    $('#f-note').value = e.note || '';
    $('#f-revoked').checked = !!e.revoked;
    openModal('Edit license key', 'Updates apply on the next /verify call.');
  }

  $('#new-btn').addEventListener('click', openNew);

  $('#key-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const original = $('#originalKey').value;
    const body = {
      key: $('#f-key').value.trim() || undefined,
      tenant_id: $('#f-tenant').value.trim() || '*',
      expires_at: $('#f-expires').value
        ? (new Date($('#f-expires').value + 'T23:59:00Z')).toISOString().replace(/\\.\\d{3}Z$/, '+00:00')
        : '',
      edition: $('#f-edition').value.trim() || 'pro',
      note: $('#f-note').value,
      revoked: $('#f-revoked').checked,
    };
    $('#modal-save').disabled = true;
    try {
      if (original) {
        await fetchJson('/admin/api/keys/' + encodeURIComponent(original), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        toast('Saved.', 'ok');
      } else {
        const res = await fetchJson('/admin/api/keys', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        toast('Created ' + res.key, 'ok');
      }
      closeModal();
      await Promise.all([loadStats(), loadKeys()]);
    } catch (e) {
      toast('Save failed: ' + e.message, 'err');
    } finally {
      $('#modal-save').disabled = false;
    }
  });

  async function toggleRevoke(key) {
    const e = allEntries.find((x) => x.key === key);
    if (!e) return;
    const newState = !e.revoked;
    if (newState && !confirm('Revoke ' + key + '? The license stops verifying immediately for new calls and within 24h for cached installs.')) return;
    try {
      await fetchJson('/admin/api/keys/' + encodeURIComponent(key), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revoked: newState }),
      });
      toast(newState ? 'Revoked.' : 'Restored.', 'ok');
      await Promise.all([loadStats(), loadKeys()]);
    } catch (err) {
      toast('Update failed: ' + err.message, 'err');
    }
  }

  async function deleteKey(key) {
    if (!confirm('Permanently delete ' + key + '? This cannot be undone.')) return;
    try {
      await fetchJson('/admin/api/keys/' + encodeURIComponent(key), { method: 'DELETE' });
      toast('Deleted.', 'ok');
      await Promise.all([loadStats(), loadKeys()]);
    } catch (err) {
      toast('Delete failed: ' + err.message, 'err');
    }
  }

  let searchTimer = null;
  $('#search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      searchQuery = $('#search').value;
      loadKeys();
    }, 200);
  });
  $('#refresh-btn').addEventListener('click', () => { loadStats(); loadKeys(); });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('#modal-back').classList.contains('show')) closeModal();
  });

  loadStats();
  loadKeys();
})();
</script>
</body></html>`;
