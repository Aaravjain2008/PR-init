/**
 * RepoGuard Popup UI Controller
 */

let currentPageContext = null;
let pollTimer = null;
let pollStart = 0;
const MAX_POLL_TIME_MS = 60000;

document.addEventListener("DOMContentLoaded", () => {
  initPopup();

  document.getElementById("btn-retry-conn")?.addEventListener("click", () => {
    initPopup();
  });

  document.getElementById("btn-start-scan")?.addEventListener("click", () => {
    startScan();
  });

  document.getElementById("btn-rescan-repo")?.addEventListener("click", () => {
    startScan();
  });

  document.getElementById("btn-open-dashboard")?.addEventListener("click", () => {
    chrome.tabs.create({ url: "http://127.0.0.1:18329" });
  });

  document.getElementById("btn-toggle-settings")?.addEventListener("click", () => {
    const panel = document.getElementById("settings-panel");
    if (panel) {
      panel.style.display = panel.style.display === "none" ? "block" : "none";
    }
  });

  document.getElementById("btn-save-secret")?.addEventListener("click", () => {
    const secret = document.getElementById("input-api-secret")?.value.trim() || "";
    chrome.runtime.sendMessage({ action: "SAVE_SECRET", secret }, () => {
      document.getElementById("settings-panel").style.display = "none";
      initPopup();
    });
  });

  // Load saved secret
  chrome.runtime.sendMessage({ action: "GET_SECRET" }, (res) => {
    if (res?.secret) {
      const input = document.getElementById("input-api-secret");
      if (input) input.value = res.secret;
    }
  });
});

async function initPopup() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  showState("loading", "Connecting to local engine...");

  // 1. Get active tab
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !tab.url.includes("github.com")) {
    setContextBanner("Not on GitHub", "Navigate to an open-source issue or pull request.");
    showState("ready", null, "Navigate to a GitHub issue or pull request to verify claims.");
    setEngineStatus(false, "Engine Ready");
    return;
  }

  // 2. Query page context from content script
  try {
    const res = await chrome.tabs.sendMessage(tab.id, { action: "EXTRACT_PAGE_CONTEXT" });
    currentPageContext = res?.context;
  } catch (e) {
    currentPageContext = parseUrlFallback(tab.url);
  }

  if (currentPageContext && currentPageContext.repository) {
    const itemLabel = currentPageContext.number
      ? `${currentPageContext.itemType === "pull_request" ? "Pull Request" : "Issue"} #${currentPageContext.number}`
      : "Repository Overview";
    setContextBanner(currentPageContext.repository, itemLabel);
  } else {
    setContextBanner("GitHub", "No specific repository detected");
  }

  // 3. Query background script for engine status and existing scan results
  chrome.runtime.sendMessage(
    { action: "GET_CONTEXT_STATUS", context: currentPageContext },
    (response) => {
      if (chrome.runtime.lastError || !response) {
        showState("error", "Cannot connect to extension background service worker.");
        setEngineStatus(false, "Offline");
        return;
      }

      if (response.status === "engine_unavailable") {
        showState("error", response.message);
        setEngineStatus(false, "Offline");
      } else if (response.status === "auth_required") {
        showState("error", response.message);
        document.getElementById("settings-panel").style.display = "block";
        setEngineStatus(false, "Auth Required");
      } else if (response.status === "results_ready" && response.targetItem) {
        setEngineStatus(true, "Connected");
        renderSingleItemResults(response.scan, response.targetItem);
      } else if (response.status === "repo_results_ready" && response.scan) {
        setEngineStatus(true, "Connected");
        renderRepoOverviewResults(response.scan);
      } else if (response.status === "item_not_scanned") {
        setEngineStatus(true, "Connected");
        showState("ready", null, response.message || `No scan covers #${currentPageContext?.number}.`);
      } else {
        setEngineStatus(true, "Connected");
        showState("ready", null, `Ready to scan ${currentPageContext?.repository || "repository"}`);
      }
    }
  );
}

function parseUrlFallback(urlStr) {
  try {
    const url = new URL(urlStr);
    const parts = url.pathname.replace(/^\/|\/$/g, "").split("/");
    if (parts.length >= 2) {
      const res = { repository: `${parts[0]}/${parts[1]}` };
      if (parts.length >= 4 && /^\d+$/.test(parts[3])) {
        res.itemType = parts[2] === "pull" ? "pull_request" : "issue";
        res.number = parseInt(parts[3], 10);
      }
      return res;
    }
  } catch (e) {}
  return null;
}

function setContextBanner(repo, item) {
  document.getElementById("context-repo").textContent = repo;
  document.getElementById("context-item").textContent = item;
}

function setEngineStatus(isOnline, label) {
  const pill = document.getElementById("engine-status-pill");
  const text = document.getElementById("engine-status-label");
  pill.className = `engine-indicator ${isOnline ? "engine-online" : "engine-offline"}`;
  text.textContent = label;
}

function showState(stateName, msgText, readyPrompt) {
  const states = ["loading", "error", "ready", "results", "repo-overview"];
  states.forEach((s) => {
    const el = document.getElementById(`state-${s}`);
    if (el) el.style.display = s === stateName ? (s === "results" || s === "repo-overview" ? "block" : "flex") : "none";
  });

  const progressEl = document.getElementById("progress-steps");
  if (progressEl && stateName !== "loading") {
    progressEl.style.display = "none";
  }

  if (stateName === "loading" && msgText) {
    document.getElementById("state-loading-text").textContent = msgText;
  }
  if (stateName === "error" && msgText) {
    document.getElementById("error-message").textContent = msgText;
  }
  if (stateName === "ready" && readyPrompt) {
    document.getElementById("ready-prompt").textContent = readyPrompt;
  }
}

function startScan() {
  if (!currentPageContext || !currentPageContext.repository) {
    alert("No repository detected to scan.");
    return;
  }

  showState("loading", "Initiating scan...");
  document.getElementById("state-loading-title").textContent = "Analyzing Technical Claims";
  const progressEl = document.getElementById("progress-steps");
  if (progressEl) progressEl.style.display = "flex";
  updateProgressStep("fetching");

  chrome.runtime.sendMessage(
    { action: "TRIGGER_SCAN", context: currentPageContext },
    (res) => {
      if (chrome.runtime.lastError || res?.error) {
        showState("error", res?.error || "Scan request failed.");
        return;
      }
      pollStart = Date.now();
      pollTimer = setInterval(pollScanProgress, 1000);
    }
  );
}

function pollScanProgress() {
  if (Date.now() - pollStart > MAX_POLL_TIME_MS) {
    clearInterval(pollTimer);
    pollTimer = null;
    showState("error", "Scan timed out after 60 seconds. The engine may still be processing.");
    return;
  }

  chrome.runtime.sendMessage({ action: "POLL_SCAN", context: currentPageContext }, (res) => {
    if (chrome.runtime.lastError || !res || res.status === "error") {
      return;
    }

    const current = res.currentScan;
    if (current) {
      const scanStatus = (current.scan_status || "").toLowerCase();
      updateProgressStep(scanStatus);
      document.getElementById("state-loading-text").textContent =
        `Status: ${scanStatus.toUpperCase()} (${current.items_scanned || 0} items scanned)`;

      if (["complete", "partial", "error"].includes(scanStatus)) {
        clearInterval(pollTimer);
        pollTimer = null;
        setTimeout(initPopup, 600);
      }
    } else {
      // Current scan finished or not active, refresh popup view
      clearInterval(pollTimer);
      pollTimer = null;
      setTimeout(initPopup, 600);
    }
  });
}

function updateProgressStep(stepName) {
  const steps = ["fetching", "extracting", "verifying", "complete"];
  steps.forEach((s) => {
    const el = document.getElementById(`step-${s}`);
    if (el) {
      el.classList.remove("step-active", "step-done");
      if (s === stepName) {
        el.classList.add("step-active");
      }
    }
  });

  const stepOrder = { fetching: 1, extracting: 2, verifying: 3, complete: 4, partial: 4, error: 4 };
  const currentLevel = stepOrder[stepName] || 0;
  steps.forEach((s) => {
    const el = document.getElementById(`step-${s}`);
    if (el && stepOrder[s] < currentLevel) {
      el.classList.add("step-done");
    }
  });
}

function renderSingleItemResults(scan, targetItem) {
  showState("results");

  // Show warnings & errors if present
  renderAlerts(scan);

  // Check Staleness (> 1 hour = 3600s)
  checkStaleness(scan.completed_at);

  // 1. Assessment Card
  const card = document.getElementById("assessment-card");
  card.className = "assessment-card";

  const assessment = targetItem.assessment || "Insufficient evidence";
  const labelEl = document.getElementById("assessment-text");
  const iconEl = document.getElementById("assessment-icon");

  if (assessment === "Review recommended") {
    card.classList.add("assessment-review");
    iconEl.textContent = "⚠️";
    labelEl.textContent = "Review recommended";
  } else if (assessment === "No strong concern found") {
    card.classList.add("assessment-no-concern");
    iconEl.textContent = "✅";
    labelEl.textContent = "No strong concern found";
  } else {
    card.classList.add("assessment-insufficient");
    iconEl.textContent = "❓";
    labelEl.textContent = "Insufficient evidence";
  }

  document.getElementById("confidence-badge").textContent = `${targetItem.assessment_confidence || "LOW"} Confidence`;
  document.getElementById("assessment-reason").textContent = targetItem.assessment_reason || "";

  const ageStr = scan.completed_at ? formatTimeAgo(scan.completed_at) : "Recent";
  document.getElementById("result-age").textContent = `Verified: ${ageStr}`;

  // 2. Claims list (stacked in 1 column)
  const claims = targetItem.claims || [];
  document.getElementById("claims-count").textContent = claims.length;
  const listEl = document.getElementById("claims-list");
  listEl.innerHTML = "";

  if (claims.length === 0) {
    listEl.innerHTML = '<div class="empty-claims">No verifiable technical claims extracted.</div>';
    return;
  }

  claims.forEach((c) => {
    const claimEl = document.createElement("div");
    claimEl.className = "claim-card";

    let verdictIcon = "❓";
    let chipClass = "chip-unverifiable";
    if (c.verdict === "supported" || c.verdict === "reproduced") {
      verdictIcon = "✅";
      chipClass = "chip-supported";
    } else if (c.verdict === "contradicted" || c.verdict === "not_reproduced") {
      verdictIcon = "❌";
      chipClass = "chip-contradicted";
    }

    let evHtml = "";
    if (c.evidence && c.evidence.length > 0) {
      c.evidence.forEach((ev) => {
        evHtml += `
          <div class="evidence-box">
            <div>${escapeHtml(ev.description)}</div>
            ${ev.file_path ? `<div class="evidence-ref">File: ${escapeHtml(ev.file_path)}${ev.line_start ? `:L${ev.line_start}` : ""}</div>` : ""}
          </div>
        `;
      });
    }

    claimEl.innerHTML = `
      <div class="claim-header">
        <span class="verdict-label">${verdictIcon} ${c.verdict.toUpperCase()}</span>
        <span class="claim-chip ${chipClass}">${(c.confidence || "LOW").toUpperCase()}</span>
      </div>
      <div class="claim-text">${escapeHtml(c.text)}</div>
      <div class="claim-reason">${escapeHtml(c.reason)}</div>
      ${evHtml}
    `;

    listEl.appendChild(claimEl);
  });
}

function renderRepoOverviewResults(scan) {
  showState("repo-overview");

  renderAlerts(scan);
  checkStaleness(scan.completed_at);

  const listEl = document.getElementById("repo-items-list");
  listEl.innerHTML = "";

  const items = scan.items || [];
  if (items.length === 0) {
    listEl.innerHTML = '<div class="empty-claims">No items scanned for this repository yet.</div>';
    return;
  }

  items.forEach((item) => {
    const itemEl = document.createElement("div");
    itemEl.className = "repo-item-card";

    let icon = "❓";
    let badgeClass = "badge-insufficient";
    if (item.assessment === "Review recommended") {
      icon = "⚠️";
      badgeClass = "badge-review";
    } else if (item.assessment === "No strong concern found") {
      icon = "✅";
      badgeClass = "badge-no-concern";
    }

    const typeLabel = item.item_type === "pull_request" ? "PR" : "Issue";
    const claimsCount = item.claims ? item.claims.length : 0;

    itemEl.innerHTML = `
      <div class="repo-item-row">
        <span class="item-badge">#${item.number} ${typeLabel}</span>
        <span class="assessment-pill ${badgeClass}">${icon} ${item.assessment}</span>
      </div>
      <div class="repo-item-title">${escapeHtml(item.title || "Untitled")}</div>
      <div class="repo-item-reason">${escapeHtml(item.assessment_reason || "")}</div>
      <div class="repo-item-meta">
        <span>${claimsCount} claim(s)</span> •
        <span>${item.assessment_confidence || "LOW"} Confidence</span> •
        <a href="${escapeHtml(item.url || '#')}" target="_blank" class="item-link">Open on GitHub ↗</a>
      </div>
    `;

    itemEl.addEventListener("click", (e) => {
      if (e.target.tagName !== "A") {
        renderSingleItemResults(scan, item);
      }
    });

    listEl.appendChild(itemEl);
  });
}

function renderAlerts(scan) {
  const errBox = document.getElementById("scan-errors-box");
  const warnBox = document.getElementById("scan-warnings-box");

  if (errBox) {
    if (scan.errors && scan.errors.length > 0) {
      errBox.innerHTML = `<strong>Errors:</strong> ${scan.errors.map(escapeHtml).join("<br/>")}`;
      errBox.style.display = "block";
    } else {
      errBox.style.display = "none";
    }
  }

  if (warnBox) {
    if (scan.warnings && scan.warnings.length > 0) {
      warnBox.innerHTML = `<strong>Warnings:</strong> ${scan.warnings.map(escapeHtml).join("<br/>")}`;
      warnBox.style.display = "block";
    } else {
      warnBox.style.display = "none";
    }
  }
}

function checkStaleness(completedAt) {
  const banner = document.getElementById("stale-banner");
  if (!banner) return;
  if (!completedAt) {
    banner.style.display = "none";
    return;
  }

  try {
    const ageSeconds = (Date.now() - new Date(completedAt).getTime()) / 1000;
    if (ageSeconds > 3600) {
      banner.style.display = "block";
    } else {
      banner.style.display = "none";
    }
  } catch (e) {
    banner.style.display = "none";
  }
}

function formatTimeAgo(isoString) {
  try {
    const date = new Date(isoString);
    const sec = Math.floor((new Date() - date) / 1000);
    if (sec < 60) return "Just now";
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return `${Math.floor(sec / 86400)}d ago`;
  } catch (e) {
    return "Recent";
  }
}

function escapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
