/**
 * RepoGuard Popup UI Controller
 */

let currentPageContext = null;

document.addEventListener("DOMContentLoaded", () => {
  initPopup();

  document.getElementById("btn-retry-conn")?.addEventListener("click", () => {
    initPopup();
  });

  document.getElementById("btn-start-scan")?.addEventListener("click", () => {
    startScan();
  });

  document.getElementById("btn-open-dashboard")?.addEventListener("click", () => {
    chrome.tabs.create({ url: "http://127.0.0.1:18329" });
  });
});

async function initPopup() {
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
    // Content script might not be injected yet; parse from tab.url
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
      } else if (response.status === "results_ready" && response.scan) {
        setEngineStatus(true, "Connected");
        renderScanResults(response.scan);
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
  const states = ["loading", "error", "ready", "results"];
  states.forEach((s) => {
    const el = document.getElementById(`state-${s}`);
    if (el) el.style.display = s === stateName ? "flex" : "none";
  });

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

  showState("loading", "Verifying technical claims against repository source...");

  chrome.runtime.sendMessage(
    { action: "TRIGGER_SCAN", context: currentPageContext },
    (res) => {
      if (chrome.runtime.lastError || res?.error) {
        showState("error", res?.error || "Scan request failed.");
        return;
      }
      // Poll for completion after 2.5 seconds
      setTimeout(initPopup, 2500);
    }
  );
}

function renderScanResults(scan) {
  const container = document.getElementById("state-results");
  showState("results");

  // Find relevant item in scan
  let targetItem = null;
  if (currentPageContext?.number && scan.items) {
    targetItem = scan.items.find((i) => i.number === currentPageContext.number);
  }
  if (!targetItem && scan.items && scan.items.length > 0) {
    targetItem = scan.items[0];
  }

  if (!targetItem) {
    showState("ready", null, "Scan finished with no items matching this page.");
    return;
  }

  // 1. Assessment Card
  const card = document.getElementById("assessment-card");
  card.className = "assessment-card";
  if (targetItem.assessment === "Review recommended") {
    card.classList.add("assessment-review");
  } else if (targetItem.assessment === "No strong concern found") {
    card.classList.add("assessment-no-concern");
  } else {
    card.classList.add("assessment-insufficient");
  }

  document.getElementById("assessment-label").textContent = targetItem.assessment;
  document.getElementById("confidence-badge").textContent = `${targetItem.assessment_confidence} Confidence`;
  document.getElementById("assessment-reason").textContent = targetItem.assessment_reason;

  const ageStr = scan.completed_at ? formatTimeAgo(scan.completed_at) : "Recent";
  document.getElementById("result-age").textContent = `Verified: ${ageStr}`;

  // 2. Claims list
  const claims = targetItem.claims || [];
  document.getElementById("claims-count").textContent = claims.length;
  const listEl = document.getElementById("claims-list");
  listEl.innerHTML = "";

  if (claims.length === 0) {
    listEl.innerHTML = '<div style="font-size:12px; color:var(--text-muted); text-align:center; padding:12px;">No verifiable technical claims extracted.</div>';
    return;
  }

  claims.forEach((c) => {
    const claimEl = document.createElement("div");
    claimEl.className = "claim-card";

    const chipClass =
      c.verdict === "supported" || c.verdict === "reproduced"
        ? "chip-supported"
        : c.verdict === "contradicted"
        ? "chip-contradicted"
        : "chip-unverifiable";

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
        <span style="font-weight:700; font-size:11px; text-transform:uppercase;">${c.verdict}</span>
        <span class="claim-chip ${chipClass}">${c.confidence.toUpperCase()}</span>
      </div>
      <div class="claim-text">${escapeHtml(c.text)}</div>
      <div class="claim-reason">${escapeHtml(c.reason)}</div>
      ${evHtml}
    `;

    listEl.appendChild(claimEl);
  });
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

