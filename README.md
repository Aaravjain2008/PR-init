# RepoGuard

> **Evidence-Based Open Source Issue and Pull Request Triage**
> A local-first, privacy-focused decision-support tool that verifies technical claims in GitHub issues and pull requests against repository evidence.

[![Tests](https://img.shields.io/badge/tests-155%20passed-success)](tests/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 1. What RepoGuard is and Who It Is For

**RepoGuard** is a local-first decision-support tool built for open-source maintainers, core contributors, and security teams. 

Open-source projects face unprecedented volumes of incoming bug reports, vulnerability disclosures, and pull requests. Many contain incorrect assumptions, hallucinated symbols, fabricated vulnerabilities, or unreproducible bugs. 

**The goal is not detecting whether a contribution was written with AI or by a human. The goal is determining whether the technical claims in the report are actually true.**

RepoGuard adheres strictly to **evidence-based decision support**:
- It **never** automatically closes, rejects, labels, or comments on GitHub issues or PRs.
- It **never** judges or labels an individual contributor (no "spam score", no "bad actor" rating).
- It extracts checkable technical claims and cross-references them against observable facts in the repository's codebase and documentation.
- When evidence is missing or inconclusive, it clearly states **"Insufficient evidence"** rather than jumping to unsupported conclusions.

---

## 2. Architecture Overview

RepoGuard consists of **one unified engine** serving three interfaces: the CLI, a local web dashboard, and a Chrome-compatible browser extension.

```
+-------------------------------------------------------------------------+
|                              USER INTERFACES                            |
|                                                                         |
|   +--------------------+  +--------------------+  +-----------------+   |
|   |  Chrome Extension  |  |  Local Dashboard   |  |  RepoGuard CLI  |   |
|   |   (Manifest V3)    |  |  (Vanilla HTML/JS) |  |   (Python 3.9+) |   |
|   +---------+----------+  +---------+----------+  +--------+--------+   |
+-------------|-----------------------|----------------------|------------+
              | (Loopback HTTP w/     | (HTTP 127.0.0.1)     | (Direct In-Memory)
              |  Pairing Secret)      |                      |
+-------------v-----------------------v----------------------v------------+
|                         REPOGUARD LOCAL ENGINE                          |
|                                                                         |
|  +--------------------+  +--------------------+  +-------------------+  |
|  | GitHub Data Client |  | Claim Extractor    |  | Claim Verifier    |  |
|  | (REST API, Cache)  |  | (Deterministic)    |  | (Source, Docs)    |  |
|  +--------------------+  +--------------------+  +-------------------+  |
|                                                                         |
|  +--------------------+  +--------------------+  +-------------------+  |
|  | Sandboxed Runner   |  | Optional Ollama    |  | SQLite Local Store|  |
|  | (Isolated Docker)  |  | (Llama 3.2 Helper) |  | (repoguard.db)    |  |
|  +--------------------+  +--------------------+  +-------------------+  |
+-------------------------------------------------------------------------+
```

### Four Primary Components:
1. **Local Background Engine (Source of Truth):**
   Handles GitHub retrieval (with pagination and rate-limit preservation), deterministic claim extraction, static AST/source verification, sandbox reproduction orchestration, optional Ollama summaries, and local SQLite persistence.
2. **Command Line Interface (CLI):**
   Full headless control for scanning repositories, inspecting scan reports, exporting data, system diagnostics, running the API server, and executing the evaluation benchmark harness.
3. **Local Dashboard:**
   Light-theme web dashboard providing the full triage queue, claim-level breakdown, evidence inspection with code snippets, scope limits panel, and benchmark viewer.
4. **Chrome Manifest V3 Extension:**
   Seamless overlay that activates directly on any GitHub issue, PR, or repository page, displaying real-time claim verdicts, confidence badges, and evidence links without leaving GitHub.

---

## 3. Core Analysis & Claim Verification

RepoGuard analyzes issues and pull requests through a systematic verification pipeline:

### 3.1 Claim Extraction
The issue or PR body and comments are parsed using deterministic heuristics:
- **File references:** paths matching known code and documentation files (`src/parser.py`, `include/curl.h`).
- **Symbol references:** class names, function identifiers, and code blocks (`TokenStream`, `parse_tokens()`).
- **Line references:** specific locations (`line 42`, `L10-L15`).
- **Version strings:** semver references (`v1.4.0`, `8.1.0`).
- **Error messages & Stack traces:** language runtime exceptions and traceback frames.
- **PR diffs:** declared changes vs actual diff hunks.

### 3.2 Verification Against Code & Docs
Each claim is verified against repository reality:
- **File Existence:** Does the file exist in the repository tree at the target ref?
- **Symbol Search:** Does the referenced class, function, or method exist in the code?
- **Line Content Check:** Does the code at referenced lines match the claim?
- **Stack Trace Consistency:** Are the referenced files and call-paths plausible?
- **PR Diff Validation:** Does the patch actually modify the files claimed? Are there unmentioned files modified?

### 3.3 Safe Sandboxed Reproduction
Where a claim includes reproducible steps or commands, execution occurs **only inside an isolated sandbox**:
- **No Network:** `--network=none` prevents data exfiltration.
- **Read-Only Code Mount:** Repo is mounted `:ro`. Host files are never writable.
- **Resource Limits:** Enforced `--memory=256m`, `--cpus=0.5`, `--pids-limit=64`.
- **Non-Root Execution:** Runs as unprivileged `nobody` with `--security-opt=no-new-privileges`.
- **Automatic Fallback:** If Docker is not installed or running, RepoGuard **never** runs untrusted code on the host. It falls back to static verification and notes: `"not reproduced: sandbox unavailable"`.

---

## 4. Verdicts, Assessments, and Uncertainty

### Per-Claim Verdicts
Every individual technical claim receives an explicit label:
- **`Supported`**: Evidence found in code/documentation confirming the claim.
- **`Contradicted`**: Evidence found in code/documentation conflicting with the claim (e.g. file does not exist, symbol missing, length bounds exist).
- **`Reproduced`**: Sandboxed test run verified the behavior.
- **`Not Reproduced`**: Sandboxed test run completed without reproducing the issue.
- **`Unverifiable`**: Insufficient code or context to make a definitive determination.
- **`Missing Evidence`**: Issue lacks vital reproduction steps, logs, or environment info.

### Overall Item Assessments
Claims are rolled up into one of three neutral assessment labels:
1. **`No strong concern found`**:
   The core technical claims are backed by observable repository evidence. High or Medium confidence.
2. **`Review recommended`**:
   One or more key claims are directly contradicted by the source code or documentation (e.g., claiming a function crashes on empty input when input validation guards exist).
3. **`Insufficient evidence`**:
   The report lacks verifiable technical details or environment logs. RepoGuard communicates uncertainty instead of making unsupported guesses.

### Three Categories Kept Separate Everywhere
1. **Observed Facts:** Directly verified file paths, lines, and symbols.
2. **Deterministic Heuristics:** Diff matches, text similarity, pattern extraction.
3. **AI Interpretation:** Any summary produced by the optional local LLM (always tagged `[AI Interpretation]`).

---

## 5. Installation and Setup

### Requirements
- **Python**: 3.9+ (Python 3.9 through 3.12 supported)
- **SQLite**: 3.x (included with standard Python)
- **Docker** *(Optional)*: Required only for sandbox reproduction.
- **Ollama** *(Optional)*: Required only for optional AI summaries with Llama 3.2.
- **Google Chrome** *(Optional)*: For browser extension.

### Quick Start

1. **Clone and setup virtual environment:**
```bash
git clone https://github.com/aaravjain/repoguard.git
cd repoguard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. **Verify installation:**
```bash
python3 cli/main.py diagnostics
```

3. **(Optional) Configure GitHub Token:**
To increase GitHub API limits from 60 to 5,000 requests/hour:
```bash
export GITHUB_TOKEN="ghp_your_personal_access_token"
```

---

## 6. Usage Guide

### CLI Commands

```bash
# Scan a specific issue or pull request
python3 cli/main.py scan curl/curl --issue 11001
python3 cli/main.py scan pallets/flask --pr 4890

# Scan recent open issues across a repository
python3 cli/main.py scan psf/requests --state open

# Check engine and environment status
python3 cli/main.py status

# Inspect a past scan report by ID
python3 cli/main.py report <scan_id>

# Export scan results to JSON
python3 cli/main.py export <scan_id> -o report.json

# List recent scans in local database
python3 cli/main.py scans

# Run system diagnostics
python3 cli/main.py diagnostics

# Run the evaluation benchmark harness
python3 cli/main.py evaluate

# Start the local HTTP API and Dashboard server
python3 cli/main.py serve --port 18329
```

### Local Dashboard

1. Start the server:
```bash
python3 cli/main.py serve --port 18329
```
2. Open your browser to `http://127.0.0.1:18329`.
3. Enter any repository (e.g. `curl/curl`), view the triage queue, filter items, and inspect claims and evidence line-by-line.

### Chrome Extension (Manifest V3)

1. Open Google Chrome and navigate to `chrome://extensions/`.
2. Toggle on **Developer mode** in the top-right corner.
3. Click **Load unpacked** and select the `extension/` directory in this repository.
4. Start the local API server:
   ```bash
   python3 cli/main.py serve --port 18329
   ```
   *Note: On first startup, the server prints your randomly generated API pairing secret (e.g. `rg_sec_...`).*
5. Open any GitHub issue, pull request, or repository page (e.g., `https://github.com/psf/requests/issues/6000`).
6. Click the RepoGuard extension icon in your Chrome toolbar.
   - If prompted, open **Settings** in the extension popup and paste your API pairing secret.
   - The extension communicates with the local engine over loopback HTTP (`http://127.0.0.1:18329`) with zero remote network calls.
   - *(Note: A native messaging host script `scripts/install_native_host.py` is included as an experimental alternate transport; loopback HTTP with pairing secret is the verified, primary connection mechanism).*

---

## 7. Optional Local AI (Llama 3.2 via Ollama)

AI in RepoGuard is strictly **optional** and **local-only**:
- All core extraction and verification operates deterministically without any AI.
- To enable local AI summarization, install [Ollama](https://ollama.ai/) and pull Llama 3.2:
  ```bash
  ollama run llama3.2
  ```
- RepoGuard enforces strict evidence-grounding:
  - System instructions instruct the model to cite *only* supplied evidence.
  - The model is blocked from inventing URLs, files, lines, or PR numbers.
  - All AI output is labeled `[AI Interpretation]`.
  - Issue text is treated as untrusted data to resist prompt injection.

---

## 8. Evaluation & Regression Suites

RepoGuard includes two evaluation workflows: a synthetic regression suite for deterministic testing of triage rules, and a builder script for assembling a benchmark of real historical GitHub issues.

### Synthetic Regression Suite

The synthetic regression suite (`evaluation/data/synthetic_suite.json`) consists of 72 synthetic cases written by the project team across 12 repository contexts (`pallets/flask`, `psf/requests`, `curl/curl`, etc.), evenly split into development (36 cases) and test (36 cases). **These cases are synthetic and provide regression testing for rule behavior; they are not a measurement of accuracy on real GitHub issues.**

Ground-truth labels are withheld from the analysis engine to prevent data leakage.

Run the synthetic regression suite:
```bash
python3 cli/main.py evaluate --split test
```

#### Results on Synthetic Regression Suite (36 test cases):
```
============================================================================
Synthetic Regression Suite — Split: [TEST SET]
Synthetic cases; not a measure of accuracy on real GitHub issues.
============================================================================
Total Test Cases:          36
Overall Accuracy:          97.2%  (95% CI: [85.8%, 99.5%])
Abstention Rate:           36.1%  (13/36)
Accuracy on Non-Abstained: 100.0%
----------------------------------------------------------------------------
Risk & Error Metrics (Cost Analysis):
  False Confirm Rate (costliest): 0.0%  (invalid waved through as valid)
  False Flag Rate:                0.0%  (confirmed flagged as review recommended)
----------------------------------------------------------------------------
Per-Label Precision & Recall (with 95% Wilson CIs):
Label        | Precision (95% CI)           | Recall (95% CI)             
----------------------------------------------------------------------------
No Concern   | 100.0% [76%, 100%]           | 100.0% [76%, 100%]          
Review Rec   | 100.0% [74%, 100%]           | 91.7% [65%, 98%]            
Insuff Ev    | 92.3% [67%, 99%]             | 100.0% [76%, 100%]          
----------------------------------------------------------------------------
Confusion Matrix (Rows: Ground Truth Expected, Cols: Predicted):
Expected \ Predicted      | No Concern   | Review Rec   | Insuff Ev   
----------------------------------------------------------------------------
No Concern                | 12           | 0            | 0           
Review Rec                | 0            | 11           | 1           
Insuff Ev                 | 0            | 0            | 12          
----------------------------------------------------------------------------
Baseline Comparisons on Held-Out Test Set:
Model / Method         | Accuracy   | False Confirm   | False Flag   | Abstain   
----------------------------------------------------------------------------
RepoGuard              |  97.2%   |   0.0%         |   0.0%       |  36.1%
Always Abstain         |  33.3%   |   0.0%         |   0.0%       | 100.0%
Random (1/3)           |  33.3%   |  58.3%         |  16.7%       |  41.7%
Keyword Heuristic      |  38.9%   |  41.7%         |  41.7%       |   0.0%
============================================================================
```

### Real GitHub Benchmark Builder

To measure true real-world accuracy on actual GitHub issues, use `scripts/build_real_benchmark.py`:
```bash
GITHUB_TOKEN=ghp_... python3 scripts/build_real_benchmark.py --limit 10
python3 cli/main.py evaluate --dataset evaluation/data/real_benchmark.json
```
Results on data fetched with this builder are the only real-world accuracy numbers. When running with `--limit 10` (or without an extensive multi-repo scrape), the sample size is intentionally small until you run a larger harvest across more repositories and labels. The builder fetches file contents for referenced files at the filing commit, skips truncated trees by default, records exact label sources, and assigns splits deterministically. Furthermore, the verifier ensures that "not found" conclusions strictly require every candidate file to have been read—if candidate files are unreadable or missing, claims are classified as unverifiable rather than falsely contradicted.

---

## 9. Privacy, Security, and Governance

- **Local-First Processing:** All analysis happens on your local workstation. No code, tokens, or issue bodies are sent to third-party cloud servers.
- **Local API Security Model:**
  - **API Pairing Secret:** A cryptographically random secret is generated on first startup and persisted in `~/.repoguard/api_secret` (mode 0600, directory 0700). All `/api/*` endpoints require the `X-RepoGuard-Secret` header, preventing unauthorized local processes or websites from interacting with the engine.
  - **Strict CORS Origin Validation:** Only trusted Chrome extension origins (`chrome-extension://*`) and explicit local dev origins (`http://127.0.0.1:<port>`, `http://localhost:<port>`) are permitted. Suffix tricks (`http://localhost.evil.com`) and external origins are strictly rejected with 403 Forbidden.
  - **DNS Rebinding Prevention:** Strict `Host` header validation rejects requests with foreign Host headers.
  - **CSRF & Injection Protection:** State-changing POST endpoints enforce `Content-Type: application/json` (rejecting `text/plain` and `application/x-www-form-urlencoded`), and configuration updates are strictly whitelisted to known keys.
- **Least Privilege:** Public repository data requires no authentication. GitHub tokens are held only in memory for API calls—never committed, logged, or serialized into databases or export files.
- **Sandbox Isolation:** Untrusted contributor code is never executed on the host. Sandboxed reproduction disables networking (`--network=none`), mounts code read-only, restricts memory/CPU, and executes as unprivileged `nobody`.
- **Data Clearing:** Maintainers can wipe local scan history at any time:
  ```bash
  rm repoguard.db
  # Or via the dashboard Settings -> Clear Local Database
  ```
- **Non-Judgmental Principles:** RepoGuard evaluates reports, not people. It never calculates "spammer" or "trust" scores for individuals and never closes, labels, or comments on GitHub items automatically.

---

## 10. Running Tests

RepoGuard ships with a comprehensive test suite (155 unit and integration tests covering extraction, verification, database persistence, API server security, sandboxing, Ollama fallback, signals, and evaluation):

```bash
# Run all tests
.venv/bin/python3 -m pytest tests/

# Run tests with coverage report
.venv/bin/python3 -m pytest --cov=repoguard --cov=cli tests/
```

---

## 11. Known Limitations

- **Complex Dynamic Runtimes:** Static claim verification cannot detect runtime behavior that depends on complex external distributed services or hardware drivers.
- **Sandbox Environment:** Sandboxed reproduction currently targets containerized POSIX environments. Windows-specific compilation bugs require appropriate container runtimes.
- **Unauthenticated Rate Limits:** Without a `GITHUB_TOKEN`, the GitHub API limits requests to 60/hour. A personal access token is recommended for scanning large repositories.

---

## 12. License

MIT License. See [LICENSE](LICENSE) for details.

