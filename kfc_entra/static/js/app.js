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
     pywebview's WebView2 backend silently blocks JS-initiated Blob URL
     downloads, so we POST to /downloads/csv which sends back a real
     Content-Disposition attachment - the browser handles that natively. */
  function kfcDownloadCsv(filename, headers, rows) {
    var form = document.createElement("form");
    form.method = "POST";
    form.action = "/downloads/csv";
    form.style.display = "none";
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "payload";
    input.value = JSON.stringify({
      filename: filename,
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
        } else if (ev.status === "already_member") {
          logLine("line-skip", "✓ " + ev.user, "already in the group");
        } else if (ev.status === "removed") {
          logLine("line-ok", "− " + ev.user, ev.reason || "removed from the group");
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
      var failedList = summary.failures || [];
      var html =
        '<div class="summary-stats">' +
        '<div class="stat stat-ok"><b>' + ok + '</b><span>added</span></div>' +
        (summary.already_member !== undefined
          ? '<div class="stat stat-skip"><b>' + summary.already_member + '</b><span>already member</span></div>' : "") +
        (summary.removed
          ? '<div class="stat stat-ok"><b>' + summary.removed + '</b><span>removed</span></div>' : "") +
        (summary.not_in_group
          ? '<div class="stat stat-skip"><b>' + summary.not_in_group + '</b><span>not in group</span></div>' : "") +
        (summary.skipped !== undefined
          ? '<div class="stat stat-skip"><b>' + summary.skipped + '</b><span>skipped</span></div>' : "") +
        '<div class="stat stat-fail"><b>' + (summary.failed || 0) + '</b><span>failed</span></div>' +
        '<div class="stat"><b>' + summary.total + '</b><span>total</span></div>' +
        '</div>';
      if (summary.per_franchisee) {
        var th = '<th>Franchisee</th><th>Added</th><th>Already</th>' +
                 (hasRemoves ? '<th>Removed</th><th>Not in group</th>' : '') +
                 '<th>Skipped</th><th>Failed</th>';
        html += '<table class="mini-table"><thead><tr>' + th + '</tr></thead><tbody>';
        Object.keys(summary.per_franchisee).sort().forEach(function (code) {
          var s = summary.per_franchisee[code];
          html += "<tr><td>" + code + "</td><td>" + (s.added||0) + "</td><td>" + (s.already_member||0) + "</td>";
          if (hasRemoves) html += "<td>" + (s.removed||0) + "</td><td>" + (s.not_in_group||0) + "</td>";
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
          kfcDownloadCsv(
            cfg.failuresCsvName || "failures.csv",
            ["user", "franchisee", "reason"],
            failedList.map(function (f) { return [f.user, f.franchisee || "", f.reason]; })
          );
        });
        card.appendChild(btn);
      }
      panel.appendChild(card);
      var kind = (summary.failed || 0) > 0 ? "warning" : "success";
      kfcToast("Finished: " + ok + " succeeded, " + (summary.failed || 0) + " failed.", kind);
      if (cfg.onDone) cfg.onDone(summary);
    }

    return kfcStream(
      cfg.url,
      { method: cfg.method, body: cfg.body, signal: controller && controller.signal },
      onEvent
    ).catch(function (err) {
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
          kfcDownloadCsv(
            cfg.failuresCsvName || "failures.csv",
            ["user", "franchisee", "reason"],
            failures.map(function (f) { return [f.user, f.franchisee || "", f.reason]; })
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

  window.kfcToast = kfcToast;
  window.kfcConfirm = kfcConfirm;
  window.kfcStream = kfcStream;
  window.kfcRunBulk = kfcRunBulk;
  window.kfcBusy = kfcBusy;
  window.kfcDownloadCsv = kfcDownloadCsv;
})();
