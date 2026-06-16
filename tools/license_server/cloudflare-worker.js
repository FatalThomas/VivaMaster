/**
 * Cloudflare Workers port of the license server.
 *
 * Reads keys from a KV namespace bound as LICENSE_KEYS. Each KV entry
 * is keyed by the license string (e.g. "KFC-YUMAU-PROD-2026") and its
 * value is JSON matching the Flask reference's keys.json shape:
 *
 *   {
 *     "tenant_id": "<entra tenant guid>" | "*",
 *     "expires_at": "2027-12-31T00:00:00+00:00",
 *     "edition": "pro",
 *     "revoked": false,
 *     "note": "Yum! Australia"
 *   }
 *
 * Endpoints:
 *   GET  /health         -> {"ok": true}
 *   POST /verify         -> license verdict matching the desktop app's
 *                            current_entitlement() expectations
 *   POST /admin/issue    -> issue a new key. Header X-Admin-Token must
 *                            match env.ADMIN_TOKEN; useful for Stripe /
 *                            Gumroad webhooks. Body:
 *                            { key, tenant_id?, expires_at, edition?, note? }
 */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path === "/health") {
      return jsonResponse({ ok: true });
    }
    if (path === "/verify" && request.method === "POST") {
      return handleVerify(request, env);
    }
    if (path === "/admin/issue" && request.method === "POST") {
      return handleIssue(request, env);
    }
    return jsonResponse({ ok: false, message: "Not found." }, 404);
  },
};


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

  return jsonResponse({
    ok: true,
    expires_at: expiresAt,
    edition: entry.edition || "pro",
    message: entry.note || "Valid.",
  });
}


async function handleIssue(request, env) {
  const token = request.headers.get("X-Admin-Token") || "";
  if (!env.ADMIN_TOKEN || token !== env.ADMIN_TOKEN) {
    return jsonResponse({ ok: false, message: "Unauthorized." }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch (e) {
    return jsonResponse({ ok: false, message: "Invalid JSON body." });
  }
  const key = ((body && body.key) || "").trim();
  if (!key) {
    return jsonResponse({ ok: false, message: "No key in body." });
  }
  const entry = {
    tenant_id: body.tenant_id || "*",
    expires_at: body.expires_at || "",
    edition: body.edition || "pro",
    revoked: false,
    note: body.note || "",
  };
  await env.LICENSE_KEYS.put(key, JSON.stringify(entry));
  return jsonResponse({ ok: true, key, entry });
}


function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}
