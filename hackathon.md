# RepoGuard: Hackathon Submission

## Team

| Role | Name | Email |
|---|---|---|
| **Team name** | **Garfield** | |
| Team lead | Aarav Jain | jainaarav254@gmail.com |
| Member 2 | Rudresh Tiwari | rudreshtiwari41@gmail.com |
| Member 3 | Sahil Sewani | sahilsewani22@gmail.com |

## Problem statement

**Evidence-Based Open Source Issue and Pull Request Triage**
Category: Artificial Intelligence | Developer Tools | Open Source Quality

### The problem

Open-source maintainers receive large volumes of bug reports, security reports, and pull requests. Unfortunately, many reports contain incorrect assumptions, fabricated issues, misleading descriptions, or claims that cannot be reproduced. Reviewing such submissions consumes valuable maintainer time and slows down the development of legitimate contributions.

The challenge is not identifying whether a report was written by a human or AI. The real challenge is determining whether the technical claims made in the report are actually true.

### Objective

Build an intelligent GitHub App that evaluates incoming issues and pull requests based on their technical claims and supporting evidence. The system should:

1. Analyze incoming issues and pull requests.
2. Identify individual technical claims.
3. Verify claims against available source code and project documentation.
4. Identify missing evidence and inconsistencies.
5. Provide reproducible verification results where possible.
6. Generate an evidence-backed assessment for maintainers.
7. Clearly communicate uncertainty instead of making unsupported conclusions.

### Research starting points

- curl's bug bounty and AI contribution policies.
- Hacktoberfest 2020 spam wave.
- OSS-Fuzz.

### Evaluation scenarios

The system will be tested against previously closed issues from public repositories with labels such as `invalid`, `cannot reproduce`, and `confirmed`. These labels serve as the reference answers for evaluating accuracy.

### Key technical considerations

- How can code submitted by unknown contributors be executed safely?
- How can the system distinguish genuine bugs from incorrect claims?
- What kind of evidence would maintainers consider trustworthy?
- How should the system communicate uncertainty?
- How can false positives and false negatives be minimized?

### Expected deliverable

A working GitHub App or equivalent functional tool that evaluates technical claims and provides maintainers with structured, evidence-backed findings.

## Our solution

**RepoGuard** is a local-first triage assistant for GitHub maintainers. Instead of guessing who wrote a report or whether an AI was involved, it checks whether the report's technical claims hold up.

For each issue or pull request, RepoGuard:

1. **Extracts individual claims**, for example "function X crashes on empty input", "this PR only touches the encoder", or "version Y is vulnerable".
2. **Verifies each claim** against the repository's source code, diff, and documentation: do the referenced files, symbols, lines, and versions exist, does the code behave as claimed, and does the PR diff match its description?
3. **Tries safe reproduction** inside an isolated sandbox when a claim is testable.
4. **Reports missing evidence and inconsistencies**, such as no versions, no logs, no reproduction steps, or claims the diff contradicts.
5. **Produces an evidence-backed assessment** with a confidence level and the uncertainty that remains.

RepoGuard is decision support only. It never closes, labels, blocks, or comments on issues or pull requests, and it never labels a person.

### Verdicts per claim

| Verdict | Meaning |
|---|---|
| Supported | Evidence found that confirms the claim |
| Contradicted | Evidence found that conflicts with the claim |
| Reproduced / Not reproduced | Result of a sandboxed attempt, with the run log |
| Unverifiable | Not enough information, or not testable here |
| Missing evidence | The report lacks details needed to verify it |

Overall assessment labels: **No strong concern found**, **Review recommended**, **Insufficient evidence**.

## Tech stack

| Layer | Technology |
|---|---|
| Local engine and CLI | Python |
| Storage | SQLite (scan history, results, settings) |
| Dashboard and extension UI | HTML, CSS, vanilla JavaScript |
| Browser extension | Chrome Manifest V3, Native Messaging to the local engine |
| Data source | GitHub REST API (public data first, least-privilege token only if needed) |
| Safe execution | Isolated sandbox (container, no network, resource limits, non-root) |
| Local AI (optional) | Llama 3.2 served by Ollama |

All interfaces (CLI, dashboard, extension) use **one engine and one result format**. There is no second analysis system.

## Models used

- **Llama 3.2 through Ollama**, running locally. Endpoint and model are configurable.
- Used for: helping phrase extracted claims and writing short summaries grounded in supplied evidence.
- Not used for: deciding verdicts on its own. Every finding comes from deterministic checks first, and AI output is always labeled as interpretation.
- No repository data is sent to a cloud service. If Ollama is absent, RepoGuard still works with deterministic results.
- Issue text, PR text, repository files, and model output are treated as untrusted data, and instructions inside them are never followed.

## Workflow

1. Maintainer opens a GitHub issue or pull request and activates the RepoGuard extension.
2. The extension's background service worker connects to the local engine through Native Messaging, using a small set of validated messages.
3. The engine identifies the repository and item, then fetches issue or PR data, changed files, patches, and relevant docs through the GitHub API, handling pagination and rate limits.
4. Claims are extracted from the text, code blocks, stack traces, version numbers, and reproduction steps.
5. Each claim is verified statically against the code and documentation.
6. Testable claims are run in the sandbox. If no sandbox is available, RepoGuard reports "not reproduced: sandbox unavailable" and never runs untrusted code on the host.
7. Evidence, verdicts, assessment, confidence, and uncertainty are saved by the engine.
8. The extension, CLI, and dashboard show the same results, with links back to the source issues and pull requests.
9. The maintainer reviews the evidence and decides what to do on GitHub. RepoGuard takes no action itself.

## How we address the key technical questions

- **Running unknown code safely:** only inside a disposable sandbox with no network, no host mounts except a read-only repo copy, no credentials, CPU, memory, and time limits, and automatic teardown. If the sandbox is unavailable, we fall back to static checks.
- **Genuine bugs vs incorrect claims:** we verify specific claims against code, docs, and sandbox results rather than judging writing style or authorship.
- **Trustworthy evidence:** file and line references, diff hunks, documentation references, and repeatable sandbox logs, each with a source link the maintainer can open.
- **Communicating uncertainty:** per-claim verdicts, an "Insufficient evidence" outcome, confidence levels, and an explicit list of what is missing or was not scanned.
- **Minimizing false positives and negatives:** we prefer abstaining over a confident wrong answer, we never treat a single signal as proof, and we measure both error types with an evaluation harness against labeled closed issues.

## Why this is useful

- Saves maintainer time by showing which claims check out and which do not, with evidence, before a human reads a long report.
- Gives maintainers something they can verify themselves instead of an unexplained score.
- Handles untrusted code safely.
- Keeps data on the maintainer's machine.
- Does not penalize new contributors or AI-assisted work. Only the technical claims matter.

## Who this is for

- Open-source maintainers who receive many bug reports, security reports, and pull requests.
- Security triage teams reviewing vulnerability claims.
- Projects dealing with spam waves or low-quality submissions, such as the ones that prompted curl's policies and Hacktoberfest 2020.

## Evaluation

We plan to run RepoGuard on previously closed issues from public repositories labeled `invalid`, `cannot reproduce`, and `confirmed`, and report accuracy, a confusion matrix, false positives, false negatives, and abstentions ("Insufficient evidence").

**Results: not yet available.** We have not run the evaluation, so no accuracy figures are claimed here. They will be added once measured.

## Project status

- **Designed:** architecture, shared result format, claim verdict model, safety rules, and the dashboard UI (see the dashboard prototype).
- **Prototype:** `repoguard_dashboard_prototype.html` shows the intended dashboard with sample data only.
- **In progress or planned:** local engine, claim extraction and verification, sandboxed reproduction, CLI, Native Messaging extension, evaluation harness, and optional Ollama integration.

Update this section to match what is actually working at the time of submission.

## Limitations

- Results cover only the data RepoGuard could fetch and scan, and each scan reports its scope and any API limits.
- Many claims cannot be verified statically or reproduced, and these are reported as unverifiable rather than guessed.
- Sandbox reproduction depends on the project being buildable in an isolated environment.
- Local AI summaries are interpretation, not fact, and depend on the local model's quality.
- An assessment is decision support and does not prove anyone's intent or character.

## Future work

- A GitHub App mode that posts evidence summaries only when a maintainer asks.
- Broader language and build-system support for sandbox reproduction.
- Better fuzzing-style reproduction ideas inspired by OSS-Fuzz.
- Larger, labeled evaluation sets and calibration of confidence levels.
