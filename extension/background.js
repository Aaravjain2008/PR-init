/**
 * RepoGuard Background Service Worker (Manifest V3)
 * Mediates communication with the local RepoGuard engine via authenticated loopback HTTP.
 */

const DEFAULT_ENGINE_URL = "http://127.0.0.1:18329";

// Cache of latest results per repository/item
const displayCache = new Map();

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "GET_CONTEXT_STATUS") {
    handleGetStatus(request.context)
      .then(sendResponse)
      .catch((err) => sendResponse({ status: "error", error: err.message }));
    return true; // Keep channel open for async response
  }

  if (request.action === "TRIGGER_SCAN") {
    handleTriggerScan(request.context)
      .then(sendResponse)
      .catch((err) => sendResponse({ status: "error", error: err.message }));
    return true;
  }
});

async function getEngineConfig() {
  const data = await chrome.storage.local.get(["engineUrl", "apiSecret"]);
  return {
    url: data.engineUrl || DEFAULT_ENGINE_URL,
    secret: data.apiSecret || "",
  };
}

async function handleGetStatus(context) {
  const { url, secret } = await getEngineConfig();
  const headers = { "Content-Type": "application/json" };
  if (secret) headers["X-RepoGuard-Secret"] = secret;

  try {
    // 1. Check engine connectivity
    const statusRes = await fetch(`${url}/api/status`, { headers });
    if (!statusRes.ok) {
      return {
        status: "engine_unavailable",
        message: "RepoGuard engine returned error status",
      };
    }
    const engineStatus = await statusRes.json();

    if (!context || !context.repository) {
      return {
        status: "ready",
        engineStatus,
        message: "Navigate to a GitHub repository, issue, or pull request to verify claims.",
      };
    }

    // 2. Check if a recent scan exists for this repo/item
    const cacheKey = context.number
      ? `${context.repository}#${context.number}`
      : context.repository;

    const scansRes = await fetch(
      `${url}/api/scans?repository=${encodeURIComponent(context.repository)}&limit=5`,
      { headers }
    );
    if (!scansRes.ok) {
      return { status: "ready", engineStatus };
    }

    const scansData = await scansRes.json();
    const matchingScan = (scansData.scans || [])[0];

    if (matchingScan) {
      // Fetch full scan details
      const detailRes = await fetch(`${url}/api/scan/${matchingScan.scan_id}`, { headers });
      if (detailRes.ok) {
        const fullScan = await detailRes.json();
        return {
          status: "results_ready",
          scan: fullScan,
          engineStatus,
        };
      }
    }

    return {
      status: "ready",
      context,
      engineStatus,
    };
  } catch (err) {
    return {
      status: "engine_unavailable",
      message: `Cannot reach local engine at ${url}. Start with 'repoguard serve' or check local firewall.`,
    };
  }
}

async function handleTriggerScan(context) {
  const { url, secret } = await getEngineConfig();
  const headers = { "Content-Type": "application/json" };
  if (secret) headers["X-RepoGuard-Secret"] = secret;

  const payload = {
    repository: context.repository,
    item_type: context.itemType,
    number: context.number,
  };

  const res = await fetch(`${url}/api/scan`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.error || `Failed to trigger scan (HTTP ${res.status})`);
  }

  return await res.json();
}

