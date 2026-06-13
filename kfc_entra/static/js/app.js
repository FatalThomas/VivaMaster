/* Shared UI machinery: toasts, confirm modal, SSE streaming, bulk progress.
   Vanilla JS only - no frameworks. */
(function () {
  "use strict";

  /* ---------- toasts ---------- */
  function toastHost() {
    var host = document.querySelector(".toast-host");
    if (!host) {
      host = document.createElement("div");
      host.className = "toast-host";
      document.body.appendChild(host);
    }
    return host;
  }

  function kfcToast(message, kind) {
    var el = document.createElement("div");
    el.className = "toast toast-" + (kind || "info");
    el.textContent = message;
    toastHost().appendChild(el);
    requestAnimationFrame(function () { el.classList.add("toast-show"); });
    setTimeout(function () {
      el.classList.remove("toast-show");
      setTimeout(function () { el.remove(); }, 300);
    }, 4200);
  }

  /* ---------- confirm modal ---------- */
  function kfcConfirm(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true">' +
        '  <h2 class="modal-title"></h2>' +
        '  <div class="modal-body"></div>' +
        '  <div class="modal-actions">' +
        '    <button type="button" class="btn btn-secondary modal-cancel">Cancel</button>' +
        '    <button type="button" class="btn btn-primary modal-ok"></button>' +
        '  </div>' +
        '</div>';
      overlay.querySelector(".modal-title").textContent = opts.title || "Are you sure?";
      overlay.querySelector(".modal-body").innerHTML = opts.bodyHtml || "";
      overlay.querySelector(".modal-ok").textContent = opts.confirmLabel || "Confirm";
      function close(answer) {
        overlay.remove();
        document.removeEventListener("keydown", onKey);
        resolve(answer);
      }
      function onKey(e) { if (e.key === "Escape") close(false); }
      overlay.querySelector(".modal-cancel").addEventListener("click", function () { close(false); });
      overlay.querySelector(".modal-ok").addEventListener("click", function () { close(true); });
      overlay.addEventListener("click", function (e) { if (e.target === overlay) close(false); });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(overlay);
      overlay.querySelector(".modal-ok").focus();
    });
  }

  /* ---------- SSE over fetch (works for GET and POST) ---------- */
  function kfcStream(url, options, onEvent) {
    options = options || {};
    var fetchOpts = {
      method: options.method || "GET",
      headers: { "Accept": "text/event-stream" },
      credentials: "same-origin",
    };
    if (options.signal) fetchOpts.signal = options.signal;
    if (options.body !== undefined) {
      fetchOpts.headers["Content-Type"] = "application/json";
      fetchOpts.body = JSON.stringify(options.body);
    }
    return fetch(url, fetchOpts).then(function (resp) {
      if (!resp.ok || !resp.body) {
        throw new Error("Stream failed to start (HTTP " + resp.status + ")");
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffer += decoder.decode(chunk.value, { stream: true });
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (part) {
            part.split("\n").forEach(function (line) {
              if (line.indexOf("data: ") === 0) {
                try { onEvent(JSON.parse(line.slice(6))); } catch (e) { /* skip */ }
              }
            });
          });
          return pump();
        });
      }
      return pump();
    });
  }

  /* ---------- CSV download ----------
     pywebview's WebView2 backend swallows the default browser
     download flow, so when running inside the desktop exe we hand the
     file to the Python JS-API which opens a native save-as dialog.
     Falls back to the legacy POST-to-/downloads/csv path in a plain
     browser (which then handles Content-Disposition itself). */
  function kfcCsvString(headers, rows) {
    function esc(v) {
      var s = (v == null ? "" : String(v));
      if (s.indexOf('"') >= 0 || s.indexOf(',') >= 0 || s.indexOf('\n') >= 0 || s.indexOf('\r') >= 0) {
        s = '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }
    var lines = [];
    if (headers && headers.length) lines.push(headers.map(esc).join(","));
    (rows || []).forEach(function (r) { lines.push(r.map(esc).join(",")); });
    return lines.join("\r\n") + "\r\n";
  }

  /* Build a re-uploadable mini-report from a failures list. Each row
     carries enough context (set in bulk.py's pending dict) for the
     Report page's parse_report to bucket it back into the right
     Franchisee / Store at retry time. */
  function kfcFailuresAsReport(failedList) {
    var headers = [
      "EMAIL", "FIRSTNAME", "LASTNAME", "FRANCHISEID", "STOREID", "STORE",
      "JOBROLE", "STATUS", "PRIMARY_BRAND", "COUNTRY", "REASON",
    ];
    var rows = failedList.map(function (f) {
      var full = (f.name || "").trim();
      var parts = full ? full.split(/\s+/) : [];
      var first = parts.shift() || "";
      var last = parts.join(" ");
      // FRANCHISEID: explicit franchisee_id from the failure event
      // (set in store mode) wins; otherwise the bucket code is the
      // franchisee itself (franchisee mode).
      var fz = f.franchisee_id || f.franchisee || "";
      var store = f.store || "";
      var storeId = f.store_id || "";
      var email = (f.email || f.user || "").toString();
      return [
        email, first, last, fz, storeId, store,
        f.job_role || "", "Active", "KFC", "Australia",
        f.reason || "",
      ];
    });
    return { headers: headers, rows: rows };
  }

  function kfcDownloadCsv(filename, headers, rows) {
    var safeName = (filename || "download.csv");
    if (!/\.csv$/i.test(safeName)) safeName = safeName + ".csv";

    // Desktop path: pywebview JS-API gives us a real save dialog.
    if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.save_csv === "function") {
      var content = kfcCsvString(headers, rows);
      window.pywebview.api.save_csv(safeName, content)
        .then(function (res) {
          if (res && res.ok) {
            kfcToast("Saved to " + res.path, "success");
          } else if (res && res.cancelled) {
            // user cancelled - silent
          } else {
            kfcToast("Couldn't save CSV: " + (res && res.error || "unknown error"), "error");
          }
        })
        .catch(function (err) {
          kfcToast("Save dialog failed: " + (err && err.message || err), "error");
        });
      return;
    }

    // Browser fallback: hidden-form POST to /downloads/csv. The server
    // sends back Content-Disposition: attachment so the browser
    // prompts a save. (Won't fire reliably in a frozen WebView2.)
    var form = document.createElement("form");
    form.method = "POST";
    form.action = "/downloads/csv";
    form.style.display = "none";
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "payload";
    input.value = JSON.stringify({
      filename: safeName,
      headers: headers,
      rows: rows,
    });
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
    setTimeout(function () { form.remove(); }, 500);
  }

  /* ---------- bulk progress panel ----------
     Drives a .bulk-panel element through start -> progress -> done. */
  function kfcRunBulk(cfg) {
    var panel = cfg.panel;
    panel.hidden = false;
    panel.innerHTML =
      '<div class="bulk-head">' +
      '  <span class="spinner"></span>' +
      '  <span class="bulk-status">Starting...</span>' +
      '  <button type="button" class="btn btn-tiny btn-secondary bulk-cancel">Cancel</button>' +
      '</div>' +
      '<div class="bulk-bar"><div class="bulk-bar-fill" style="width:0%"></div></div>' +
      '<div class="bulk-log" aria-live="polite"></div>';
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });

    var statusEl = panel.querySelector(".bulk-status");
    var fillEl = panel.querySelector(".bulk-bar-fill");
    var logEl = panel.querySelector(".bulk-log");
    var cancelBtn = panel.querySelector(".bulk-cancel");
    var total = 0;
    var failures = [];
    var lastProgress = null;
    var verb = cfg.verb || "Converting";

    var controller = ("AbortController" in window) ? new AbortController() : null;
    var cancelled = false;
    cancelBtn.addEventListener("click", function () {
      if (cancelled) return;
      cancelled = true;
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Cancelling...";
      statusEl.textContent = "Cancelling - waiting for current item to finish...";
      if (controller) controller.abort();
    });

    function logLine(cls, text, reason) {
      var line = document.createElement("div");
      line.className = "bulk-line " + cls;
      line.textContent = text + (reason ? " - " + reason : "");
      logEl.appendChild(line);
      logEl.scrollTop = logEl.scrollHeight;
    }

    function onEvent(ev) {
      if (ev.type === "phase") {
        statusEl.textContent = ev.message;
      } else if (ev.type === "start") {
        total = ev.total;
        statusEl.textContent = total
          ? verb + " 0 of " + total + "..."
          : "Nothing to do.";
      } else if (ev.type === "group") {
        if (ev.status === "created") {
          logLine("line-ok", "✓ Created group \"" + ev.group_name + "\" for " + ev.franchisee);
        } else if (ev.status === "failed") {
          logLine("line-fail", "✗ Group for " + ev.franchisee, ev.reason);
        } else {
          logLine("line-skip", "→ " + ev.franchisee + " → " + ev.group_name);
        }
      } else if (ev.type === "progress") {
        lastProgress = ev;
        if (!cancelled) {
          statusEl.textContent = verb + " " + ev.current + " of " + ev.total + "...";
        }
        fillEl.style.width = (ev.total ? (100 * ev.current / ev.total) : 100) + "%";
        if (ev.status === "ok" || ev.status === "added") {
          logLine("line-ok", "✓ " + ev.user);
        } else if (ev.status === "invited") {
          logLine("line-ok", "✉ " + ev.user, ev.reason || "Guest invitation sent");
        } else if (ev.status === "already_member") {
          logLine("line-skip", "✓ " + ev.user, "already in the group");
        } else if (ev.status === "removed") {
          logLine("line-ok", "− " + ev.user, ev.reason || "removed from the group");
        } else if (ev.status === "owner_added") {
          logLine("line-ok", "♛ " + ev.user, ev.reason || "promoted to community admin");
        } else if (ev.status === "already_owner") {
          logLine("line-skip", "♛ " + ev.user, ev.reason || "already an owner");
        } else if (ev.status === "disabled") {
          logLine("line-ok", "⊘ " + ev.user, ev.reason || "account disabled");
        } else if (ev.status === "deleted") {
          logLine("line-ok", "🗑 " + ev.user, ev.reason || "deleted from tenant");
        } else if (ev.status === "already_gone") {
          logLine("line-skip", "○ " + ev.user, ev.reason || "already deleted");
        } else if (ev.status === "not_in_group") {
          logLine("line-skip", "○ " + ev.user, ev.reason || "wasn't in the group");
        } else if (ev.status === "skipped") {
          logLine("line-skip", "○ " + ev.user, ev.reason);
        } else {
          logLine("line-fail", "✗ " + ev.user, ev.reason);
          failures.push(ev);
        }
      } else if (ev.type === "done") {
        if (cancelBtn) cancelBtn.hidden = true;
        renderSummary(ev.summary);
      } else if (ev.type === "cancelled") {
        cancelled = true;
        renderCancelled();
      } else if (ev.type === "error") {
        statusEl.textContent = "Failed";
        panel.querySelector(".spinner").remove();
        logLine("line-fail", "✗ " + ev.message);
        kfcToast(ev.message, "error");
        if (cfg.onDone) cfg.onDone(null);
      }
    }

    function renderSummary(summary) {
      fillEl.style.width = "100%";
      var head = panel.querySelector(".bulk-head");
      head.innerHTML = '<span class="bulk-status">Finished</span>';
      var card = document.createElement("div");
      card.className = "summary-card";
      var ok = summary.succeeded !== undefined ? summary.succeeded : (summary.added || 0);
      var hasRemoves = (summary.removed || 0) + (summary.not_in_group || 0) > 0;
      var hasDeletes = (summary.deleted || 0) + (summary.already_gone || 0) > 0;
      var hasOwners  = (summary.owner_added || 0) + (summary.already_owner || 0) > 0;
      var failedList = summary.failures || [];
      var html =
        '<div class="summary-stats">' +
        '<div class="stat stat-ok"><b>' + ok + '</b><span>added</span></div>' +
        (summary.invited
          ? '<div class="stat stat-ok"><b>' + summary.invited + '</b><span>invited</span></div>' : "") +
        (summary.already_member !== undefined
          ? '<div class="stat stat-skip"><b>' + summary.already_member + '</b><span>already member</span></div>' : "") +
        (summary.owner_added
          ? '<div class="stat stat-ok"><b>' + summary.owner_added + '</b><span>promoted (admin)</span></div>' : "") +
        (summary.already_owner
          ? '<div class="stat stat-skip"><b>' + summary.already_owner + '</b><span>already admin</span></div>' : "") +
        (summary.removed
          ? '<div class="stat stat-ok"><b>' + summary.removed + '</b><span>removed</span></div>' : "") +
        (summary.not_in_group
          ? '<div class="stat stat-skip"><b>' + summary.not_in_group + '</b><span>not in group</span></div>' : "") +
        (summary.deleted
          ? '<div class="stat stat-ok"><b>' + summary.deleted + '</b><span>deleted</span></div>' : "") +
        (summary.already_gone
          ? '<div class="stat stat-skip"><b>' + summary.already_gone + '</b><span>already gone</span></div>' : "") +
        (summary.skipped !== undefined
          ? '<div class="stat stat-skip"><b>' + summary.skipped + '</b><span>skipped</span></div>' : "") +
        '<div class="stat stat-fail"><b>' + (summary.failed || 0) + '</b><span>failed</span></div>' +
        '<div class="stat"><b>' + summary.total + '</b><span>total</span></div>' +
        '</div>';
      if (summary.per_franchisee) {
        var th = '<th>Bucket</th><th>Added</th><th>Already</th>' +
                 (hasOwners  ? '<th>Promoted</th><th>Already admin</th>' : '') +
                 (hasRemoves ? '<th>Removed</th><th>Not in group</th>' : '') +
                 (hasDeletes ? '<th>Deleted</th><th>Already gone</th>' : '') +
                 '<th>Skipped</th><th>Failed</th>';
        html += '<table class="mini-table"><thead><tr>' + th + '</tr></thead><tbody>';
        Object.keys(summary.per_franchisee).sort().forEach(function (code) {
          var s = summary.per_franchisee[code];
          html += "<tr><td>" + code + "</td><td>" + (s.added||0) + "</td><td>" + (s.already_member||0) + "</td>";
          if (hasOwners)  html += "<td>" + (s.owner_added||0) + "</td><td>" + (s.already_owner||0) + "</td>";
          if (hasRemoves) html += "<td>" + (s.removed||0) + "</td><td>" + (s.not_in_group||0) + "</td>";
          if (hasDeletes) html += "<td>" + (s.deleted||0) + "</td><td>" + (s.already_gone||0) + "</td>";
          html += "<td>" + (s.skipped||0) + "</td><td>" + (s.failed||0) + "</td></tr>";
        });
        html += "</tbody></table>";
      }
      card.innerHTML = html;
      if (failedList.length) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-secondary";
        btn.textContent = "Download failures CSV (" + failedList.length + ")";
        btn.addEventListener("click", function () {
          // Emit a re-uploadable report-format CSV (EMAIL, FIRSTNAME,
          // LASTNAME, FRANCHISEID, STOREID, STORE, JOBROLE, STATUS,
          // PRIMARY_BRAND, COUNTRY, REASON). The Report page accepts
          // it directly, so retrying a failed batch in a fresh session
          // is just "download CSV -> Report -> upload".
          var bundle = kfcFailuresAsReport(failedList);
          kfcDownloadCsv(
            cfg.failuresCsvName || "failures.csv",
            bundle.headers,
            bundle.rows
          );
        });
        card.appendChild(btn);

        // Retry failed only - works for any job-managed apply (reports).
        // Sends the failed emails back to the same start endpoint with
        // retry_emails set, which filters the row work list to just
        // those emails. Skips reconcile / delete (server-side safety).
        if (cfg.body && cfg.body.assignments) {
          var failedEmails = failedList
            .map(function (f) { return (f.user || "").toLowerCase(); })
            .filter(function (e) { return e && e.indexOf("@") > 0; });
          if (failedEmails.length) {
            var retryBtn = document.createElement("button");
            retryBtn.type = "button";
            retryBtn.className = "btn btn-primary";
            retryBtn.style.marginLeft = "8px";
            retryBtn.textContent = "Retry failed (" + failedEmails.length + ")";
            retryBtn.addEventListener("click", function () {
              var retryBody = JSON.parse(JSON.stringify(cfg.body || {}));
              retryBody.retry_emails = failedEmails;
              retryBody.remove_missing = false;
              retryBody.delete_missing = false;
              // Spin up a fresh panel below the summary so the user
              // can see the retry without losing this run's results.
              var retryPanel = document.createElement("div");
              retryPanel.className = "bulk-panel";
              retryPanel.style.marginTop = "16px";
              panel.parentElement.insertBefore(retryPanel, panel.nextSibling);
              kfcRunBulk({
                url: cfg.url,
                method: cfg.method,
                body: retryBody,
                panel: retryPanel,
                verb: "Retrying",
                failuresCsvName: (cfg.failuresCsvName || "failures.csv").replace(/\.csv$/, "-retry.csv"),
              });
              retryBtn.disabled = true;
              retryBtn.textContent = "Retry started";
            });
            card.appendChild(retryBtn);
          }
        }
      }
      panel.appendChild(card);
      var kind = (summary.failed || 0) > 0 ? "warning" : "success";
      kfcToast("Finished: " + ok + " succeeded, " + (summary.failed || 0) + " failed.", kind);
      if (cfg.onDone) cfg.onDone(summary);
    }

    // Two callers shapes:
    //   1. POST + JSON {job_id, stream_url}  -> job-managed (resumable
    //      via /jobs/<id>/stream, survives WiFi blips, shows in the
    //      dock). Used by the report apply routes.
    //   2. GET or POST with text/event-stream -> the legacy direct-SSE
    //      pattern (no job manager). Used by /users/convert/stream and
    //      /offboard/apply/stream. Reusing it keeps those callers
    //      working until they're migrated too; cancel stops via the
    //      AbortController only.
    var subscription = null;
    var jobId = null;
    var method = (cfg.method || "GET").toUpperCase();
    var legacyController = null;

    cancelBtn.addEventListener("click", function () {
      // Job-managed: tell the server thread to stop. Legacy: abort the
      // local fetch (which kills the SSE stream).
      if (jobId) {
        fetch("/jobs/" + jobId + "/cancel", {
          method: "POST", credentials: "same-origin",
        }).catch(function () { /* server already gone? fine */ });
      }
      if (legacyController) {
        try { legacyController.abort(); } catch (e) {}
      }
    });

    var fetchOpts = {
      method: method,
      headers: { "Accept": "application/json, text/event-stream" },
      credentials: "same-origin",
    };
    if (method !== "GET" && method !== "HEAD") {
      fetchOpts.headers["Content-Type"] = "application/json";
      fetchOpts.body = JSON.stringify(cfg.body || {});
    }

    return fetch(cfg.url, fetchOpts).then(function (resp) {
      if (!resp.ok) {
        // Try JSON error envelope first, fall back to plain text.
        return resp.text().then(function (txt) {
          var msg = txt;
          try { var j = JSON.parse(txt); msg = j.error || j.message || txt; } catch (e) {}
          throw new Error(msg || ("Start failed (HTTP " + resp.status + ")"));
        });
      }
      var ct = (resp.headers.get("Content-Type") || "").toLowerCase();

      // Branch on response content type. JSON = job-managed; SSE =
      // legacy direct stream.
      if (ct.indexOf("application/json") === 0) {
        return resp.json().then(function (info) {
          jobId = info.job_id;
          kfcTrackJob(jobId, cfg.verb);
          subscription = kfcSubscribeJob(info.stream_url, onEvent, {
            onReconnect: function () {
              var hint = document.createElement("span");
              hint.className = "bulk-reconnect-hint";
              hint.textContent = " (network blip - resuming...)";
              var head = panel.querySelector(".bulk-head");
              if (head && !head.querySelector(".bulk-reconnect-hint")) {
                head.appendChild(hint);
                setTimeout(function () { hint.remove(); }, 4000);
              }
            },
          });
        });
      }

      // Legacy direct-SSE response. Read it inline; no job manager.
      legacyController = ("AbortController" in window) ? new AbortController() : null;
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffer += decoder.decode(chunk.value, { stream: true });
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (part) {
            part.split("\n").forEach(function (line) {
              if (line.indexOf("data: ") === 0) {
                try { onEvent(JSON.parse(line.slice(6))); } catch (e) {}
              }
            });
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (cancelled || (err && err.name === "AbortError")) {
        renderCancelled();
        return;
      }
      statusEl.textContent = "Stream error";
      var sp = panel.querySelector(".spinner");
      if (sp) sp.remove();
      logLine("line-fail", "✗ " + err.message);
      kfcToast(err.message, "error");
      if (cfg.onDone) cfg.onDone(null);
    });

    function renderCancelled() {
      var sp = panel.querySelector(".spinner");
      if (sp) sp.remove();
      if (cancelBtn) cancelBtn.remove();
      var done = lastProgress ? lastProgress.current : 0;
      var of = lastProgress ? lastProgress.total : total;
      statusEl.textContent = "Cancelled" + (of ? " after " + done + " of " + of : "");
      logLine("line-skip", "○ Cancelled by user. Items already finished are not rolled back.");
      if (failures.length) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-secondary";
        btn.textContent = "Download failures CSV (" + failures.length + ")";
        btn.addEventListener("click", function () {
          var bundle = kfcFailuresAsReport(failures);
          kfcDownloadCsv(
            cfg.failuresCsvName || "failures.csv",
            bundle.headers,
            bundle.rows
          );
        });
        panel.appendChild(btn);
      }
      kfcToast("Cancelled.", "warning");
      if (cfg.onDone) cfg.onDone(null);
    }
  }

  /* ---------- buttons: disable + spinner while busy ---------- */
  function kfcBusy(button, busy) {
    if (!button) return;
    if (busy) {
      button.dataset.label = button.innerHTML;
      button.disabled = true;
      button.innerHTML = '<span class="spinner spinner-sm"></span> Working...';
    } else {
      button.disabled = false;
      if (button.dataset.label) button.innerHTML = button.dataset.label;
    }
  }

  /* ---------- top-of-page loading banner ----------
     Used while navigating to slow pages (Users / Groups) so the
     8-second first-load doesn't feel like the app froze. The current
     page keeps showing this banner until the new page replaces the DOM,
     and it auto-fades after the duration if the new page never arrives. */
  function kfcLoadingBanner(message, durationMs) {
    var existing = document.querySelector(".loading-banner");
    if (existing) existing.remove();
    var el = document.createElement("div");
    el.className = "loading-banner";
    el.innerHTML = '<span class="spinner spinner-sm"></span> ' +
      String(message == null ? "" : message)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("loading-banner-show"); });
    setTimeout(function () {
      el.classList.remove("loading-banner-show");
      setTimeout(function () { el.remove(); }, 300);
    }, durationMs || 5000);
  }

  /* Auto-fire the banner whenever the user clicks a link to /users.
     Skips middle-click / cmd-click / ctrl-click so new-tab opens don't
     show the banner on the current page. */
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a");
    if (!a) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target === "_blank") return;
    if (a.pathname === "/users") {
      kfcLoadingBanner("Loading Users page…", 5000);
    }
  });

  /* ---------- resumable job subscription ----------
     Opens an SSE stream to /jobs/<id>/stream?since=<offset>, tracks the
     last offset seen via SSE id: lines, and on network error / clean
     close reconnects with that offset so missed events get replayed.
     Stops when a stream_end event arrives or the caller calls .stop(). */
  function kfcSubscribeJob(streamUrl, onEvent, opts) {
    opts = opts || {};
    var since = 0;
    var stopped = false;
    var ctrl = null;
    var retries = 0;
    var verb = opts.verb || "";

    function connect() {
      ctrl = ("AbortController" in window) ? new AbortController() : null;
      var url = streamUrl + (streamUrl.indexOf("?") >= 0 ? "&" : "?") + "since=" + since;
      var fetchOpts = {
        method: "GET",
        headers: { "Accept": "text/event-stream" },
        credentials: "same-origin",
      };
      if (ctrl) fetchOpts.signal = ctrl.signal;

      fetch(url, fetchOpts).then(function (resp) {
        if (!resp.ok || !resp.body) {
          throw new Error("Stream failed (HTTP " + resp.status + ")");
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var pendingId = null;

        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) return;
            buffer += decoder.decode(chunk.value, { stream: true });
            var parts = buffer.split("\n\n");
            buffer = parts.pop();
            parts.forEach(function (part) {
              var dataLine = null;
              part.split("\n").forEach(function (line) {
                if (line.indexOf("id: ") === 0) {
                  pendingId = parseInt(line.slice(4), 10);
                } else if (line.indexOf("data: ") === 0) {
                  dataLine = line.slice(6);
                }
              });
              if (dataLine != null) {
                try {
                  var ev = JSON.parse(dataLine);
                  if (pendingId != null) {
                    since = Math.max(since, pendingId + 1);
                    pendingId = null;
                  }
                  if (ev.type === "stream_end") {
                    stopped = true;
                    if (opts.onClose) opts.onClose(ev);
                    return;
                  }
                  onEvent(ev);
                } catch (e) { /* malformed JSON - skip */ }
              }
            });
            if (stopped) return;
            return pump();
          });
        }
        retries = 0;  // any successful read resets the backoff
        return pump();
      }).then(function () {
        // Clean end-of-body without seeing stream_end - server may have
        // crashed mid-stream. Treat as a reconnect.
        if (!stopped) scheduleReconnect();
      }).catch(function (err) {
        if (stopped) return;
        scheduleReconnect();
      });
    }

    function scheduleReconnect() {
      retries += 1;
      if (retries > 30) {
        // ~5 min of solid failures - give up and surface an error.
        stopped = true;
        onEvent({ type: "error", message: "Lost connection to the server. Refresh the page to retry." });
        return;
      }
      // Cap backoff at 8 s so a long outage doesn't snowball.
      var delay = Math.min(8000, 1000 * Math.pow(1.7, Math.min(retries, 6)));
      if (opts.onReconnect) opts.onReconnect({ retries: retries, delay: delay });
      setTimeout(function () {
        if (!stopped) connect();
      }, delay);
    }

    connect();

    return {
      stop: function () {
        stopped = true;
        if (ctrl) try { ctrl.abort(); } catch (e) {}
      },
    };
  }

  /* ---------- active-actions dock ----------
     Floating panel showing currently-running server jobs so the user can
     navigate to other pages while a 50k-row apply is running. Polls
     /jobs/active every 4 s; clicking "Open" navigates back to the page
     that launched the job (so the original .bulk-panel resumes). */
  var KFC_DOCK_POLL_MS = 4000;

  function kfcTrackJob(jobId, verb) {
    // Cache job_id + verb in localStorage so the dock can render an
    // expected entry even before the next poll arrives (avoids a flash
    // of "no active jobs" on the page that started the work).
    try {
      var key = "kfc-active-jobs";
      var current = JSON.parse(localStorage.getItem(key) || "[]");
      if (current.indexOf(jobId) < 0) current.push(jobId);
      localStorage.setItem(key, JSON.stringify(current));
      kfcRefreshDock();  // immediate
    } catch (e) {}
  }

  function kfcDockEl() {
    var el = document.getElementById("kfc-dock");
    if (el) return el;
    el = document.createElement("div");
    el.id = "kfc-dock";
    el.hidden = true;
    document.body.appendChild(el);
    return el;
  }

  function kfcRenderDock(jobs) {
    var el = kfcDockEl();
    if (!jobs || !jobs.length) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    var html = '<div class="kfc-dock-head">' +
      '<span class="spinner spinner-sm"></span>' +
      ' Active actions <span class="kfc-dock-count">(' + jobs.length + ')</span>' +
      '</div><div class="kfc-dock-body">';
    jobs.forEach(function (j) {
      var label = (j.label || "Running").replace(/</g, "&lt;");
      var pct = j.percent || 0;
      var onCurrent = j.started_from && location.pathname === new URL(j.started_from, location.origin).pathname;
      html +=
        '<div class="kfc-dock-job" data-job="' + j.id + '">' +
          '<div class="kfc-dock-label">' + label + '</div>' +
          '<div class="kfc-dock-progress"><div class="kfc-dock-fill" style="width:' + pct + '%"></div></div>' +
          '<div class="kfc-dock-meta">' +
            (j.total ? (j.current + " of " + j.total + " (" + pct + "%)") : "Starting...") +
          '</div>' +
          '<div class="kfc-dock-actions">' +
            (onCurrent ? "" :
              '<a class="btn btn-tiny btn-secondary" href="' + (j.started_from || "/") + '">Open page</a>') +
            ' <button type="button" class="btn btn-tiny" data-cancel="' + j.id + '">Cancel</button>' +
          '</div>' +
        '</div>';
    });
    html += "</div>";
    el.innerHTML = html;
    el.querySelectorAll("[data-cancel]").forEach(function (b) {
      b.addEventListener("click", function () {
        var jid = b.dataset.cancel;
        if (!confirm("Cancel this action? Items already finished are not rolled back.")) return;
        fetch("/jobs/" + jid + "/cancel", { method: "POST", credentials: "same-origin" });
        b.disabled = true;
        b.textContent = "Cancelling...";
      });
    });
  }

  function kfcRefreshDock() {
    fetch("/jobs/active", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { jobs: [] }; })
      .then(function (data) {
        kfcRenderDock(data.jobs || []);
        // Sync localStorage to the server's truth
        try {
          var ids = (data.jobs || []).map(function (j) { return j.id; });
          localStorage.setItem("kfc-active-jobs", JSON.stringify(ids));
          localStorage.setItem("kfc-active-jobs-snapshot", JSON.stringify(data.jobs || []));
        } catch (e) {}
      })
      .catch(function () { /* silent - we'll retry on the next interval */ });
  }

  function kfcDockOptimisticRender() {
    // Show the dock immediately on page load from the last known good
    // snapshot in localStorage, so a fresh page after navigation doesn't
    // flash an empty dock for the ~4 s until the first poll lands.
    try {
      var raw = localStorage.getItem("kfc-active-jobs-snapshot");
      if (!raw) return;
      var jobs = JSON.parse(raw);
      if (Array.isArray(jobs) && jobs.length) kfcRenderDock(jobs);
    } catch (e) {}
  }

  function kfcHasActiveJobsLocal() {
    try {
      var raw = localStorage.getItem("kfc-active-jobs-snapshot");
      if (!raw) return false;
      var jobs = JSON.parse(raw);
      return Array.isArray(jobs) && jobs.length > 0;
    } catch (e) { return false; }
  }

  // Surface a tiny toast when the user clicks a nav link while a job is
  // active, so the transition "loses" the bulk panel without the user
  // worrying that the apply stopped. The dock on the new page will pick
  // up where it left off.
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a");
    if (!a) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target === "_blank") return;
    if (!a.href || a.href.indexOf(location.origin) !== 0) return;
    if (a.pathname === location.pathname) return;
    if (!kfcHasActiveJobsLocal()) return;
    kfcToast("Apply continues in the dock (bottom-right). Track it from any page.", "info");
  });

  // Don't poll the dock on the login / device-code pages - no point and
  // /jobs/active would just 302 to login anyway.
  if (document.body && document.body.dataset && document.body.dataset.dockOff !== "1") {
    kfcDockOptimisticRender();  // instant render from cached snapshot
    kfcRefreshDock();
    setInterval(kfcRefreshDock, KFC_DOCK_POLL_MS);
  }

  window.kfcToast = kfcToast;
  window.kfcConfirm = kfcConfirm;
  window.kfcStream = kfcStream;
  window.kfcRunBulk = kfcRunBulk;
  window.kfcBusy = kfcBusy;
  window.kfcDownloadCsv = kfcDownloadCsv;
  window.kfcLoadingBanner = kfcLoadingBanner;
  window.kfcSubscribeJob = kfcSubscribeJob;
  window.kfcTrackJob = kfcTrackJob;
  window.kfcRefreshDock = kfcRefreshDock;
})();
