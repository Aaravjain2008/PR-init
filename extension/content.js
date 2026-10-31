/**
 * RepoGuard Content Script
 * Extracts GitHub repository and issue/PR context from the current tab.
 */

function extractGitHubContext() {
  const path = window.location.pathname.replace(/^\/|\/$/g, "");
  const parts = path.split("/");

  if (parts.length < 2) return null;

  const owner = parts[0];
  const repo = parts[1];

  // Ignore GitHub system paths
  const systemPaths = ["settings", "notifications", "explore", "marketplace", "pulls", "issues"];
  if (systemPaths.includes(owner)) return null;

  let itemType = null;
  let number = null;

  if (parts.length >= 4) {
    if (parts[2] === "issues" && /^\d+$/.test(parts[3])) {
      itemType = "issue";
      number = parseInt(parts[3], 10);
    } else if (parts[2] === "pull" && /^\d+$/.test(parts[3])) {
      itemType = "pull_request";
      number = parseInt(parts[3], 10);
    }
  }

  return {
    repository: `${owner}/${repo}`,
    owner,
    repo,
    itemType,
    number,
    url: window.location.href,
  };
}

// Respond to context queries from popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "EXTRACT_PAGE_CONTEXT") {
    const context = extractGitHubContext();
    sendResponse({ context });
  }
});

