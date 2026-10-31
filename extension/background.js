/**
 * RepoGuard Background Service Worker (Manifest V3)
 * Mediates communication with the local RepoGuard engine via authenticated loopback HTTP.
 */

const DEFAULT_ENGINE_URL = "http://127.0.0.1:18329";

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

  if (request.action === "POLL_SCAN") {
    handlePollScan(request.context)
      .then(sendResponse)
      .catch((err) => sendResponse({ status: "error", error: err.message }));
    return true;
  }

  if (request.action === "SAVE_SECRET") {
    chrome.storage.local.set({ apiSecret: request.secret }, () => {
      sendResponse({ status: "saved" });
    });
    return true;
  }

  if (request.action === "GET_SECRET") {
    chrome.storage.local.get(["apiSecret"], (data) => {
      sendResponse({ secret: data.apiSecret || "" });
    });
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
      if (statusRes.status === 401) {
        return {
          status: "auth_required",
          message: "API pairing secret required. Configure secret in settings.",
        };
      }
      return {
        status: "engine_unavailable",
        message: `Engine returned HTTP ${statusRes.status}`,
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

    // 2. Fetch recent scans for this repository
    const scansRes = await fetch(
      `${url}/api/scans?repository=${encodeURIComponent(context.repository)}&limit=10`,
      { headers }
    );
    if (!scansRes.ok) {
      return { status: "ready", context, engineStatus };
    }

    const scansData = await scansRes.json();
    const scansList = scansData.scans || [];

    // P7: If viewing an issue or PR, look specifically for a scan that covers THIS item number
    if (context.number) {
      for (const scanSummary of scansList) {
        // Fetch full scan details to inspect items
        try {
          const detailRes = await fetch(`${url}/api/scan/${scanSummary.scan_id}`, { headers });
          if (detailRes.ok) {
            const fullScan = await detailRes.json();
            const matchingItem = (fullScan.items || []).find(
              (i) => i.number === context.number
            );
            if (matchingItem) {
              return {
                status: "results_ready",
                scan: fullScan,
                targetItem: matchingItem,
                engineStatus,
              };
            }
          }
        } catch (e) {
          // Continue searching other scans
        }
      }

      // No scan covers this specific item! Never fall back to another item's result (P7).
      return {
        status: "item_not_scanned",
        context,
        engineStatus,
        message: `No scan found for #${context.number}. Click 'Verify Claims Now' to analyze.`,
      };
    }

    // P7: On a repository overview page, get the newest scan
    if (scansList.length > 0) {
      const newestScanSummary = scansList[0];
      const detailRes = await fetch(`${url}/api/scan/${newestScanSummary.scan_id}`, { headers });
      if (detailRes.ok) {
        const fullScan = await detailRes.json();
        return {
          status: "repo_results_ready",
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
      message: `Cannot reach local engine at ${url}. Start with 'repoguard server'.`,
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

async function handlePollScan(context) {
  const { url, secret } = await getEngineConfig();
  const headers = { "Content-Type": "application/json" };
  if (secret) headers["X-RepoGuard-Secret"] = secret;

  const statusRes = await fetch(`${url}/api/status`, { headers });
  if (!statusRes.ok) {
    return { status: "error", error: "Failed to poll engine status" };
  }
  const statusData = await statusRes.json();
  const currentScan = statusData.current_scan;

  return {
    status: "ok",
    currentScan: currentScan || null,
  };
}
