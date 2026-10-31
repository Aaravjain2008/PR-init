# RepoGuard

> **Evidence-Based Open Source Issue and Pull Request Triage**
> A local-first, privacy-focused decision-support tool that verifies technical claims in GitHub issues and pull requests against repository evidence.

[![Tests](https://img.shields.io/badge/tests-144%20passed-success)](tests/)
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
              | (Loopback HTTP /      | (HTTP 127.0.0.1)     | (Direct In-Memory)
              |  Native Messaging)    |                      |
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

1. Open Google Chrome and go to `chrome://extensions/`.
2. Toggle on **Developer mode** in the top-right corner.
3. Click **Load unpacked** and select the `/Users/aaravjain/Desktop/hacktober/extension` folder.
4. Open any GitHub issue or PR (e.g., `https://github.com/curl/curl/issues/11001`).
5. Click the RepoGuard extension icon in your toolbar to see live claim verification.

#### Native Messaging Host Registration (Optional)
To register the native messaging host for extension-to-engine communication:
```bash
python3 scripts/install_native_host.py
```

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

## 8. Evaluation Benchmark Harness

RepoGuard includes a dedicated evaluation harness that benchmarks the system against real-world closed issues with ground-truth reference labels (`invalid`, `cannot reproduce`, and `confirmed`).

Run the evaluation suite:
```bash
python3 cli/main.py evaluate
```

### Benchmark Results on Reference Dataset:
```
========================================================================
RepoGuard Evaluation Benchmark Results
========================================================================
Total Test Cases:       6
Correct Assessments:    6 (100%)
Abstentions (Uncertain):2
False Positives:        0 (Invalid classified as valid)
False Negatives:        0 (Valid classified as contradicted)
------------------------------------------------------------------------
Policy Observation:
  RepoGuard favors explicit uncertainty ('Insufficient evidence')
  over making unsupported conclusions.
------------------------------------------------------------------------
Confusion Matrix (Rows: Expected, Cols: Predicted):
Expected \ Predicted      | No Concern   | Review Rec   | Insuff Ev   
------------------------------------------------------------------------
No Concern                | 2            | 0            | 0           
Review Rec                | 0            | 2            | 0           
Insuff Ev                 | 0            | 0            | 2           
========================================================================
```

---

## 9. Privacy, Security, and Governance

- **Local-First Processing:** All analysis happens on your local workstation. No code or issue bodies are sent to third-party cloud servers.
- **Least Privilege:** Public data requires no authentication. Tokens are stored in memory or local environment variables only—never committed, logged, or serialized.
- **Sandbox Isolation:** Untrusted contributor code is never executed on the host. Sandboxed reproduction disables networking and mounts code read-only.
- **Data Clearing:** Maintainers can wipe local scan history at any time:
  ```bash
  rm repoguard.db
  # Or via the dashboard Settings -> Clear Local Database
  ```
- **Non-Judgmental Principles:** Contributor activity patterns are secondary signals and never label or score individuals.

---

## 10. Running Tests

RepoGuard ships with a comprehensive test suite (144 unit and integration tests covering extraction, verification, database persistence, API, sandboxing, Ollama fallback, signals, and evaluation):

```bash
# Run all tests
.venv/bin/pytest tests/

# Run tests with coverage report
.venv/bin/pytest --cov=repoguard --cov=cli tests/
```

---

## 11. Known Limitations

- **Complex Dynamic Runtimes:** Static claim verification cannot detect runtime behavior that depends on complex external distributed services or hardware drivers.
- **Sandbox Environment:** Sandboxed reproduction currently targets containerized POSIX environments. Windows-specific compilation bugs require appropriate container runtimes.
- **Unauthenticated Rate Limits:** Without a `GITHUB_TOKEN`, the GitHub API limits requests to 60/hour. A personal access token is recommended for scanning large repositories.

---

## 12. License

MIT License. See [LICENSE](LICENSE) for details.

