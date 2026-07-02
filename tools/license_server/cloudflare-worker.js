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
 *   STRIPE
 *     POST /stripe/webhook     -> Stripe Checkout completion handler.
 *                                  Verifies the signature against
 *                                  env.STRIPE_WEBHOOK_SECRET, mints a
 *                                  fresh license key, writes it to KV,
 *                                  and emails it to the customer via
 *                                  Resend (env.RESEND_API_KEY +
 *                                  env.LICENSE_FROM_EMAIL).
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
 *     "edition": "paid",
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
    // Public read of the trial banner config. The desktop app polls
    // this on launch so a single source of truth (this Worker) drives
    // whether the in-app "Free trial - N days remaining" banner shows
    // and on what end-date.
    if (path === "/trial-config" && request.method === "GET") {
      return getTrialConfig(env);
    }
    // Stripe -> Worker webhook. Mints a license key on successful
    // checkout and emails it to the customer via Resend. See the
    // detailed setup in the file header.
    if (path === "/stripe/webhook" && request.method === "POST") {
      return handleStripeWebhook(request, env);
    }

    // --- admin UI (HTML) ---
    if (path === "/admin/login" && request.method === "POST") {
      return handleAdminLogin(request, env);
    }
    if (path === "/admin/logout" && request.method === "POST") {
      return handleAdminLogout();
    }
    // Root + /admin both land on the admin UI - login page if there's
    // no valid cookie, dashboard otherwise. Anyone hitting the bare
    // worker URL therefore gets the admin login, not a 404.
    if (path === "/admin" || path === "/") {
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
      if (path === "/admin/api/trial" && request.method === "GET") {
        return getTrialConfig(env);
      }
      if (path === "/admin/api/trial" && request.method === "PUT") {
        return setTrialConfig(request, env);
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
    return jsonResponse({
      ok: false,
      reason: "unknown",
      message: "Unknown license key.",
    });
  }
  let entry;
  try {
    entry = JSON.parse(raw);
  } catch (e) {
    return jsonResponse({
      ok: false,
      reason: "corrupt",
      message: "License entry is corrupted - contact support.",
    });
  }

  if (entry.revoked) {
    return jsonResponse({
      ok: false,
      reason: "revoked",
      message: "This license has been revoked.",
    });
  }

  const allowedTenant = entry.tenant_id || "*";
  if (allowedTenant !== "*" && allowedTenant !== tenantId) {
    return jsonResponse({
      ok: false,
      reason: "tenant_mismatch",
      message: "This license is not authorized for your tenant.",
    });
  }

  const expiresAt = entry.expires_at || "";
  if (expiresAt && new Date(expiresAt) < new Date()) {
    return jsonResponse({
      ok: false,
      reason: "expired",
      expires_at: expiresAt,
      edition: entry.edition || "paid",
      message: "This license has expired. Renew to keep using the app.",
    });
  }

  // Machine binding: the first computer to verify a key claims it.
  // Subsequent verify calls from any other computer are rejected
  // until an admin clears the binding ("Unbind" in the admin UI).
  const sentMachineId = ((body && body.machine_id) || "").trim();
  const boundMachineId = (entry.machine_id || "").trim();
  if (sentMachineId) {
    if (!boundMachineId) {
      entry.machine_id = sentMachineId;
      entry.bound_at = isoNow();
    } else if (boundMachineId !== sentMachineId) {
      return jsonResponse({
        ok: false,
        reason: "machine_mismatch",
        expires_at: expiresAt,
        edition: entry.edition || "paid",
        message: "This license is already activated on another computer. Contact support to transfer it.",
      });
    }
  }

  // Best-effort: stamp last_verified_at (+ the new binding above) so
  // the admin page shows recent usage. Failures here must NOT block
  // the verdict - the next /verify call will simply re-bind.
  try {
    entry.last_verified_at = isoNow();
    await env.LICENSE_KEYS.put(key, JSON.stringify(entry));
  } catch (e) { /* ignore */ }

  return jsonResponse({
    ok: true,
    expires_at: expiresAt,
    edition: entry.edition || "paid",
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
      "Location": "/",
      "Set-Cookie": [
        COOKIE_NAME + "=" + encodeURIComponent(token),
        "HttpOnly",
        "Secure",
        "SameSite=Strict",
        // Path=/ so the cookie is visible to both / (the new default
        // landing page) and every /admin/* sub-route.
        "Path=/",
        "Max-Age=" + COOKIE_MAX_AGE,
      ].join("; "),
    },
  });
}

function handleAdminLogout() {
  return new Response(null, {
    status: 302,
    headers: {
      "Location": "/",
      "Set-Cookie": COOKIE_NAME + "=; Path=/; Max-Age=0",
    },
  });
}


// =====================================================================
// Admin API
// =====================================================================

// Reserved KV-key prefix. License keys are EUM-style, so this can't
// collide. Used today for the trial-banner config; future settings
// (e.g. branding, feature flags) slot in alongside.
const CONFIG_PREFIX = "__config__:";
const CONFIG_KEY_TRIAL = CONFIG_PREFIX + "trial";
const EVENT_PREFIX = "__event__:";

// Any KV row whose name starts with "__" is internal bookkeeping
// (config, Stripe-event dedupe markers, etc.) and must be hidden from
// the admin license views.
function isInternalKVKey(name) {
  return typeof name === "string" && name.startsWith("__");
}


async function getTrialConfig(env) {
  const raw = await env.LICENSE_KEYS.get(CONFIG_KEY_TRIAL);
  // buy_url is the Stripe Payment Link the desktop app's "Buy a
  // license" button sends customers to. Lives on the same config row
  // as the trial banner because both are admin-controlled and both
  // need to ship to every install on the next poll.
  let cfg = { enabled: true, ends_at: "", buy_url: "", updated_at: "" };
  if (raw) {
    try { Object.assign(cfg, JSON.parse(raw)); } catch (e) { /* keep defaults */ }
  }
  return jsonResponse({ ok: true, ...cfg });
}

async function setTrialConfig(request, env) {
  let body;
  try { body = await request.json(); } catch (e) {
    return jsonResponse({ ok: false, message: "Invalid JSON body." }, 400);
  }
  const cfg = {
    enabled: Boolean(body.enabled),
    ends_at: body.ends_at || "",
    buy_url: (body.buy_url || "").trim(),
    updated_at: isoNow(),
  };
  await env.LICENSE_KEYS.put(CONFIG_KEY_TRIAL, JSON.stringify(cfg));
  return jsonResponse({ ok: true, ...cfg });
}


async function adminStats(env) {
  const list = await env.LICENSE_KEYS.list();
  let total = 0, active = 0, revoked = 0, expired = 0;
  const now = new Date();
  // KV's list() returns up to 1000 entries; for a license server that's
  // plenty. If the customer base outgrows that, paginate by cursor.
  const reads = await Promise.all(
    list.keys
      .filter((k) => !isInternalKVKey(k.name))
      .map((k) => env.LICENSE_KEYS.get(k.name).then((raw) => [k.name, raw]))
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
    list.keys
      .filter((k) => !isInternalKVKey(k.name))
      .map((k) => env.LICENSE_KEYS.get(k.name).then((raw) => [k.name, raw]))
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
  // Default-and-pin policy: only one "paid" tier ships today, and the
  // license is valid for exactly one year from issuance unless the
  // admin explicitly set a date in the form.
  const entry = {
    tenant_id: body.tenant_id || "*",
    expires_at: body.expires_at || isoOneYearFromNow(),
    edition: body.edition || "paid",
    revoked: Boolean(body.revoked),
    note: body.note || "",
    created_at: isoNow(),
  };
  if (!replaceAll) {
    // Preserve last_verified_at + machine binding across upserts so
    // admin actions don't wipe out usage info or accidentally unbind
    // the key from the customer's computer.
    const existing = await env.LICENSE_KEYS.get(key);
    if (existing) {
      try {
        const e = JSON.parse(existing);
        if (e.last_verified_at) entry.last_verified_at = e.last_verified_at;
        if (e.created_at) entry.created_at = e.created_at;
        if (e.machine_id) entry.machine_id = e.machine_id;
        if (e.bound_at) entry.bound_at = e.bound_at;
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
  // Apply partial updates - only the fields the client sent. Sending
  // "machine_id" as an empty string clears the binding (= "Unbind" in
  // the admin UI), which lets the customer reactivate the key from a
  // different computer after a hardware change.
  for (const f of ["tenant_id", "expires_at", "edition", "revoked", "note", "machine_id"]) {
    if (body[f] !== undefined) entry[f] = body[f];
  }
  if (body.machine_id === "") {
    entry.bound_at = "";
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
// Stripe webhook -> mint key -> email via Resend
// =====================================================================
//
// Required secrets (Settings -> Variables and Secrets on the worker):
//   STRIPE_WEBHOOK_SECRET  - "whsec_..." from Stripe Dashboard ->
//                            Developers -> Webhooks -> your endpoint
//   RESEND_API_KEY         - "re_..." from https://resend.com (free tier
//                            covers 3000 emails / month)
//   LICENSE_FROM_EMAIL     - the verified-on-Resend sender address,
//                            e.g. "licenses@yourdomain.com"
//
// In Stripe -> Developers -> Webhooks add an endpoint pointing at
//   https://kfc-licenses.<sub>.workers.dev/stripe/webhook
// and tick `checkout.session.completed`. Set up the webhook in *test
// mode* first; the secret is environment-specific. When you flip to
// live mode, just update STRIPE_WEBHOOK_SECRET.

async function handleStripeWebhook(request, env) {
  if (!env.STRIPE_WEBHOOK_SECRET) {
    return jsonResponse({
      ok: false,
      message: "Stripe webhook secret not configured on this Worker.",
    }, 500);
  }
  const sigHeader = request.headers.get("Stripe-Signature") || "";
  const body = await request.text();
  const valid = await verifyStripeSignature(sigHeader, body, env.STRIPE_WEBHOOK_SECRET);
  if (!valid) {
    return jsonResponse({ ok: false, message: "Invalid signature." }, 400);
  }
  let event;
  try { event = JSON.parse(body); } catch (e) {
    return jsonResponse({ ok: false, message: "Body is not JSON." }, 400);
  }

  // Only the "checkout completed" event mints keys today. We acknowledge
  // every other event with 200 so Stripe doesn't retry.
  if (event.type !== "checkout.session.completed") {
    return jsonResponse({ ok: true, message: "Event ignored: " + event.type });
  }

  // Idempotency: Stripe re-delivers webhooks until they get a 2xx, and
  // a key per Stripe-event-id makes double-charge / duplicate-key
  // impossible across those retries.
  const dedupeKey = EVENT_PREFIX + event.id;
  if (await env.LICENSE_KEYS.get(dedupeKey)) {
    return jsonResponse({ ok: true, message: "Already processed." });
  }

  const session = event.data && event.data.object || {};
  const customerEmail =
    (session.customer_details && session.customer_details.email) ||
    session.customer_email ||
    "";
  if (!customerEmail) {
    return jsonResponse({
      ok: false,
      message: "Checkout session has no customer email - aborting.",
    }, 400);
  }

  // Mint the license + write to KV.
  const key = generateLicenseKey();
  const entry = {
    tenant_id: "*",
    expires_at: isoOneYearFromNow(),
    edition: "paid",
    revoked: false,
    note: "Auto-issued: " + customerEmail + " (Stripe " + (session.id || "?") + ")",
    created_at: isoNow(),
    customer_email: customerEmail,
    stripe_session_id: session.id || "",
  };
  await env.LICENSE_KEYS.put(key, JSON.stringify(entry));
  // Dedupe entry expires after 7 days - past Stripe's retry window.
  await env.LICENSE_KEYS.put(dedupeKey, "1", { expirationTtl: 7 * 24 * 3600 });

  // Best-effort: email the key. Resend failures don't fail the
  // webhook (key is already in KV and visible on /admin), but they
  // do show in the worker logs so the operator can re-send manually.
  let emailStatus = "skipped";
  if (env.RESEND_API_KEY && env.LICENSE_FROM_EMAIL) {
    try {
      await sendLicenseEmail(env, customerEmail, key, entry);
      emailStatus = "sent";
    } catch (e) {
      console.error("Resend send failed:", e && e.message || e);
      emailStatus = "failed: " + (e && e.message || "unknown");
    }
  }
  return jsonResponse({ ok: true, key, email: emailStatus });
}


async function verifyStripeSignature(sigHeader, body, secret) {
  if (!sigHeader) return false;
  let timestamp = "";
  const signatures = [];
  for (const part of sigHeader.split(",")) {
    const [k, v] = part.split("=");
    if (k === "t") timestamp = v;
    if (k === "v1") signatures.push(v);
  }
  if (!timestamp || !signatures.length) return false;
  // Reject events older than 5 minutes (Stripe's default replay window).
  const age = Math.floor(Date.now() / 1000) - parseInt(timestamp, 10);
  if (!isFinite(age) || age > 300 || age < -300) return false;
  const expected = await hmacSha256Hex(secret, timestamp + "." + body);
  for (const sig of signatures) {
    if (constantTimeEqual(sig, expected)) return true;
  }
  return false;
}


async function hmacSha256Hex(secret, data) {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw", enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]
  );
  const sigBuf = await crypto.subtle.sign("HMAC", cryptoKey, enc.encode(data));
  return Array.from(new Uint8Array(sigBuf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}


function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) {
    r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return r === 0;
}


async function sendLicenseEmail(env, toEmail, key, entry) {
  const expiresDate = (entry.expires_at || "").slice(0, 10);
  const html = `<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1d1d1f;line-height:1.55;max-width:560px;margin:24px auto;padding:0 16px;">
  <h2 style="color:#0f6cbd;margin-top:0;">Your Entra User Manager license</h2>
  <p>Thanks for your purchase! Here&rsquo;s your license key &mdash; keep this email, you&rsquo;ll need the key if you ever reinstall:</p>
  <p style="background:#f5f5f7;border:1px solid #d2d2d7;border-radius:8px;padding:14px 18px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:18px;font-weight:600;letter-spacing:1px;text-align:center;">${escapeHtml(key)}</p>
  <p><b>To activate:</b></p>
  <ol>
    <li>Open the Entra User Manager app.</li>
    <li>Wait until the free-trial banner expires, or click the license page link at the bottom of the banner.</li>
    <li>Paste the key into the <b>License key</b> field and click <b>Save &amp; verify</b>.</li>
  </ol>
  <p>Your license is valid until <b>${escapeHtml(expiresDate)}</b> (one year from today). It works in any tenant you sign in to.</p>
  <hr style="border:none;border-top:1px solid #d2d2d7;margin:24px 0;">
  <p style="color:#6e6e73;font-size:13px;margin:0;">Anything wrong with the key? Just reply to this email.</p>
</body></html>`;
  const text =
    "Your Entra User Manager license key:\n\n" + key + "\n\n" +
    "To activate: paste this key into the License field in the app and " +
    "click Save & verify. Valid until " + expiresDate + ".\n\n" +
    "Keep this email - you'll need the key if you reinstall on another machine.";

  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + env.RESEND_API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: "ENTRA LICENSE MANAGER <" + env.LICENSE_FROM_EMAIL + ">",
      to: toEmail,
      subject: "[ENTRA LICENSE MANAGER] Your license key inside",
      html: html,
      text: text,
    }),
  });
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error("Resend API " + resp.status + ": " + errText);
  }
  return resp.json();
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

function isoOneYearFromNow() {
  const d = new Date();
  d.setUTCFullYear(d.getUTCFullYear() + 1);
  // 23:59 UTC so the customer gets the whole final day before they roll.
  d.setUTCHours(23, 59, 0, 0);
  return d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function generateLicenseKey() {
  // EUM-XXXX-XXXX-XXXX, A-Z + 0-9, cryptographically random.
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
  return "EUM-" + groups.join("-");
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
    <div class="brand-mark">EUM</div>
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
    --accent: #0f6cbd;
    --accent-dark: #0a4f8a;
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
  a { color: var(--accent); text-decoration: none; }
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
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .btn-primary:hover { background: var(--accent-dark); }
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
    background: var(--accent); color: white;
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
  .login-form input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .login-form .btn { width: 100%; }
  .err {
    background: #fbe6e2; color: var(--err);
    padding: 10px 12px; border-radius: 8px;
    margin-bottom: 16px;
  }

  /* dashboard */
  .topbar {
    background: var(--accent); color: white;
    padding: 12px 24px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .topbar .brand-title { color: white; }
  .topbar .brand-mark { background: white; color: var(--accent); }
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

  /* settings card (trial banner) */
  .settings-card {
    background: white;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 16px;
  }
  .settings-card h3 { margin: 0 0 4px; }
  .settings-card .small { margin-bottom: 12px; }
  .settings-row { margin: 10px 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .checkbox-row { display: flex; align-items: center; gap: 8px; }
  .field-inline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .field-inline span { font-weight: 500; }
  .field-inline input[type=date] {
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 14px;
  }

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
  .toolbar input[type=search]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }

  .table-wrap { background: white; border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
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

  .row-actions { white-space: nowrap; text-align: right; }
  .row-actions .btn { margin-left: 4px; }
  .row-actions .icon-btn { margin-left: 2px; }

  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px; height: 30px;
    border-radius: 7px;
    border: 1px solid transparent;
    background: transparent;
    color: var(--muted);
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
    padding: 0;
    transition: background .12s, color .12s, border-color .12s;
  }
  .icon-btn:hover {
    background: var(--bg);
    color: var(--ink);
    border-color: var(--line);
  }
  .icon-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .icon-btn[disabled] { opacity: 0.4; cursor: not-allowed; }
  .icon-btn-ok:hover { color: var(--ok); border-color: #c4e8d0; background: #e6f5ec; }
  .icon-btn-warn:hover { color: #8a6300; border-color: #ead78e; background: #fff7d6; }
  .icon-btn-danger:hover { color: var(--err); border-color: #f3c1c8; background: #fbe6e2; }

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
    outline: 2px solid var(--accent); outline-offset: 1px;
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
    <div class="brand-mark">EUM</div>
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

  <section class="settings-card">
    <h3>Free-trial banner</h3>
    <p class="muted small">
      Controls the yellow &ldquo;Free trial &mdash; N day(s) remaining&rdquo; banner
      shown on every install. Changes are picked up on each app launch
      (the desktop app caches the config for 30&nbsp;minutes).
    </p>
    <div class="settings-row">
      <label class="checkbox-row">
        <input type="checkbox" id="trial-enabled">
        <span><b>Show free-trial banner</b>
          <small> &mdash; off = installs hit the licence page immediately without a trial</small>
        </span>
      </label>
    </div>
    <div class="settings-row">
      <label class="field-inline">
        <span>Trial ends on</span>
        <input type="date" id="trial-ends-at">
        <small> &mdash; blank = each install gets its own 14-day clock from first launch</small>
      </label>
    </div>
    <div class="settings-row">
      <label class="field-inline" style="flex: 1; min-width: 320px;">
        <span>Stripe payment link</span>
        <input type="url" id="buy-url" placeholder="https://buy.stripe.com/..." style="flex: 1; padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px; font-size: 14px; min-width: 280px;">
        <small> &mdash; the &ldquo;Buy a license&rdquo; button on every install sends customers here</small>
      </label>
    </div>
    <div class="settings-row">
      <button class="btn btn-primary" id="trial-save">Save banner settings</button>
      <span class="muted small" id="trial-status">&nbsp;</span>
    </div>
  </section>

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
          <th>Edition</th>
          <th>Expires</th>
          <th>Status</th>
          <th>Note</th>
          <th>Machine</th>
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
          <small>blank = auto-generate (EUM-XXXX-XXXX-XXXX)</small>
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
          <small>(default policy is 1 year; adjust here for refunds / manual extensions)</small>
        </label>
        <input type="date" name="expires_at" id="f-expires">
      </div>
      <div class="field">
        <label>Edition
          <small>(only one tier - "paid" - is sold today)</small>
        </label>
        <select name="edition" id="f-edition">
          <option value="paid" selected>Paid</option>
        </select>
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
      const isBound = !!(e.machine_id && e.machine_id.length);
      const bindCell = isBound
        ? '<span class="mono small" title="Bound ' + esc(fmtDateTime(e.bound_at)) + '">' +
            esc(e.machine_id.slice(0, 10)) + '&hellip;</span>'
        : '<span class="muted small">unbound</span>';
      const unbindBtn = isBound
        ? '<button class="icon-btn" data-unbind="' + esc(e.key) + '" title="Unbind from machine">&#8855;</button>'
        : '';
      const toggleBtn = isRevoked
        ? '<button class="icon-btn icon-btn-ok" data-toggle="' + esc(e.key) + '" title="Restore">&#8634;</button>'
        : '<button class="icon-btn icon-btn-warn" data-toggle="' + esc(e.key) + '" title="Revoke">&#8856;</button>';
      return (
        '<tr>' +
          '<td><span class="mono">' + esc(e.key) + '</span></td>' +
          '<td>' + esc(e.edition || 'pro') + '</td>' +
          '<td>' + esc(fmtDate(e.expires_at)) + '</td>' +
          '<td><span class="badge ' + cls + '">' + label + '</span></td>' +
          '<td>' + esc(e.note || '') + '</td>' +
          '<td>' + bindCell + '</td>' +
          '<td class="mono small">' + esc(fmtDateTime(e.last_verified_at)) + '</td>' +
          '<td class="row-actions">' +
            '<button class="icon-btn" data-edit="' + esc(e.key) + '" title="Edit">&#9998;</button>' +
            toggleBtn +
            unbindBtn +
            '<button class="icon-btn icon-btn-danger" data-delete="' + esc(e.key) + '" title="Delete permanently">&#10006;</button>' +
          '</td>' +
        '</tr>'
      );
    }).join('');

    $$('[data-edit]').forEach((b) => b.addEventListener('click', () => openEdit(b.dataset.edit)));
    $$('[data-toggle]').forEach((b) => b.addEventListener('click', () => toggleRevoke(b.dataset.toggle)));
    $$('[data-unbind]').forEach((b) => b.addEventListener('click', () => unbindKey(b.dataset.unbind)));
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

  async function unbindKey(key) {
    if (!confirm('Unbind ' + key + '? The next computer that verifies the key claims the new binding - use this when a customer reinstalls or replaces a machine.')) return;
    try {
      await fetchJson('/admin/api/keys/' + encodeURIComponent(key), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ machine_id: '' }),
      });
      toast('Unbound.', 'ok');
      await loadKeys();
    } catch (err) {
      toast('Unbind failed: ' + err.message, 'err');
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

  // --- trial banner settings ---
  async function loadTrialConfig() {
    try {
      const r = await fetchJson('/admin/api/trial');
      $('#trial-enabled').checked = !!r.enabled;
      $('#trial-ends-at').value = r.ends_at ? r.ends_at.slice(0, 10) : '';
      $('#buy-url').value = r.buy_url || '';
      if (r.updated_at) {
        $('#trial-status').textContent = 'Last saved ' + fmtDateTime(r.updated_at);
      } else {
        $('#trial-status').textContent = '';
      }
    } catch (e) {
      toast('Trial config load failed: ' + e.message, 'err');
    }
  }
  $('#trial-save').addEventListener('click', async () => {
    const ends = $('#trial-ends-at').value;
    const body = {
      enabled: $('#trial-enabled').checked,
      ends_at: ends
        ? (new Date(ends + 'T23:59:00Z')).toISOString().replace(/\\.\\d{3}Z$/, '+00:00')
        : '',
      buy_url: $('#buy-url').value.trim(),
    };
    $('#trial-save').disabled = true;
    try {
      await fetchJson('/admin/api/trial', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      toast('Trial settings saved.', 'ok');
      await loadTrialConfig();
    } catch (e) {
      toast('Save failed: ' + e.message, 'err');
    } finally {
      $('#trial-save').disabled = false;
    }
  });

  loadStats();
  loadKeys();
  loadTrialConfig();
})();
</script>
</body></html>`;
