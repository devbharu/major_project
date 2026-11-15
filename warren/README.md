# Warren

An autonomous GitHub bot that reviews pull requests, ranks the review queue, and repairs broken CI/CD pipelines. Every fix is
reproduced and verified in an isolated E2B sandbox, and anything risky waits for human approval.

| Capability | Behaviour |
|---|---|
| Review | A LangGraph agent reads the diff, verifies each suspicion with tools (read, grep, semantic search, sandbox shell), then posts a professional review: assessment, findings table, inline comments with committable suggestions, pre-merge checks. |
| Autofix | Patch, verify in the sandbox, one commit, threaded "Fixed in `sha`" reply, independent re-check. |
| CI/CD Doctor | When a pipeline is **currently** broken: explain why, reproduce, fix, prove the fix, apply it (PR branch, or a fix PR when `main` is broken). Risky or unverified fixes pause for approval. |
| Triage board | P0 to P3 queue, risk/complexity/effort tags, saved views, reviewer suggestions. |
| Change Stack | Layers, security blast radius, architecture impact, verification, diffs. |
| Security | OSV dependency advisories, AI deep scan with a verification pass, one-click dependency-bump PRs. |
| Live dashboard | Server-sent events; no refreshing. |
| Chat | `@warren <question>`, `@warren review`, `@warren fix`. Collaborators can teach it team conventions. |

## Review output

Findings are precision-first: model confidence below 0.6 is discarded, comments are capped at 10 inline per review (the rest are
listed in the summary), and every finding carries severity, category, evidence-based reasoning, impact and, when exact, a committable
suggestion. Output is plain professional prose; no emoji.

### Pre-merge checks

Checks evaluated on every review and shown in the summary:

| Check | Passes when |
|---|---|
| Description | the PR description has real content |
| Title | the title is specific (not "fix", "update", "wip") |
| Docstring coverage | at least 80% of new public Python functions/classes are documented |
| Test changes | source changes come with test changes |
| Sandbox checks | syntax and lint pass in the E2B sandbox |

Warren also publishes a `warren/pre-merge` **commit status** (success, or failure when there are critical/major findings or a failed
check). It only blocks merging if you mark it as a required status check in branch protection; otherwise it is informational.
Toggle it per repository in Settings.

User-written text (repository instructions, steering notes) is rewritten into clean, professional guidance when saved
(`agents/refine.py`), and all prompts treat PR text, diffs and logs as untrusted data.

## CI/CD Doctor

Researched failure modes: superseded and cancelled runs from concurrency, re-run attempts, flaky tests hidden by blind retries, a broken
`main` failing every branch, and fix loops. Design:

**1. Act only on a current break.** A failed run becomes work only if all of these hold (`CiDoctor.assess`):
the run failed (cancelled runs are ignored); it is newer than the moment Warren started watching the repo; it ran on the branch's
**current head commit**; it is the latest run of its workflow; and no incident already covers that commit. The same checks run again
before analysis and before any push, because approval can take hours.

**2. One incident per break.** Several workflows failing on one commit become one incident (the others are noted on it). A newer
failing commit supersedes the older incident. A failure that fingerprints the same as one already handled (failing test ids / error
text with timestamps, hashes and paths removed) is folded into it. Folded rows are bookkeeping and never shown.

**3. Resolution closes everything.** When the workflow turns green on the current head, every open incident for it is resolved
automatically (fix held, a human fixed it, or a re-run passed, which also marks it flaky) and any running job is cancelled. Old failed
runs arriving later (redelivery, polling) are ignored by rule 1.

| Scenario | Warren's behaviour |
|---|---|
| New commit breaks CI | Investigate: reproduce, fix, validate, apply or ask. |
| Failed run for an old commit / already green / cancelled | Ignored. |
| Two workflows fail on the same commit | One incident. |
| Branch fails because `main` already fails the same way | PR branch untouched; PR gets one explanatory comment; `main` is fixed once via a PR. |
| Warren's fix fails the same way | Stops with an explanation; never stacks another commit. |
| Fix works | Next green run marks the incident resolved. |
| Transient infra failure (network, runner shutdown) | One re-run, then a real investigation. Passing on re-run is recorded as flaky. |
| Cannot be reproduced in the sandbox | One re-run (not for deployments), then approval is required. |
| Missing secret / permission / config | Root cause plus numbered human steps; no code change. |
| Workflow file is the cause (wrong `working-directory`, unresolvable action, invalid YAML) | Fix proposed as an exact diff to `.github/workflows/*`. Validated against the **patched** workflow (valid YAML, every working directory exists, the failing step replayed with the corrected settings), then approval is required. |
| Fix cannot be validated, or the token lacks the `workflow` scope | The exact diff is still shown in the dashboard and posted as a PR comment; nothing is discarded or pushed. |
| An attempt repeats the previous proposal | Stops retrying and shows the diff. |
| The failing PR comes from a fork (Warren cannot push) | The bot answers **in the PR conversation**: lines that are part of the PR diff get GitHub *suggested changes* (one click to commit), anything else is posted as a diff comment, with the root cause and verification result. No approval is needed because nothing is pushed. If the author enabled "Allow edits by maintainers", Warren offers to push to the fork after approval. |
| Review autofix on a fork PR | The bot comments that it cannot push and points to the committable suggestion on each finding. |
| Deployment (CD) failure | Never re-run automatically; every fix needs approval. |
| `main` broken by its head commit and no verified fix | Revert PR (only if that commit is still the head). |
| Branch moved while waiting for approval | Not applied. |
| Failure on Warren's own fix or revert branch | Diagnose only; never a fix of a fix. |
| PR already received 2 CI fixes | Escalate instead of looping. |

Guardrails: never pushes to the default branch, never merges, never weakens tests or linters (prompted and approval-gated), protected
paths (workflow files, lockfiles, secrets, Dockerfiles) require approval, fork PRs are diagnosed and the fix is suggested in the PR conversation instead of pushed. Repository
setting **CI mode**: diagnose only, ask first, or autonomous (default: applies safe, verified fixes; asks when risky).

## Security

| Area | Control |
|---|---|
| Secrets at rest | GitHub tokens encrypted with Fernet (`TOKEN_ENCRYPTION_KEY` or derived from `SECRET_KEY`); no default keys; production refuses weak configuration. |
| Sessions | Short-lived JWT (issuer-checked, 24 h) in the `Authorization` header only; never in URLs. Live stream uses a 60-second single-use ticket. |
| OAuth | `state` bound to the browser by an HttpOnly cookie nonce. |
| Webhooks | HMAC signature required (unsigned deliveries rejected), constant-time compare, `X-GitHub-Delivery` replay protection, minimum event subscription. |
| Authorization | Every resource checked against the signed-in owner; bot commands and learning restricted to collaborators with write access. |
| Prompt injection | PR text, diffs, logs and messages are delimited as untrusted data; hidden HTML comments and invisible Unicode stripped; sandbox isolates all execution. |
| Unsafe model output | Images, raw HTML, non-HTTPS or query-string links and stray @mentions removed before posting (blocks exfiltration and spam); model-proposed file paths validated against traversal. |
| Abuse | Rate limits on login and expensive actions, per-user stream caps, 26 MB body limit. |
| Transport/headers | CSP, `nosniff`, frame denial, no-referrer, no-store, HSTS over TLS; strict CSP on the production frontend build. |
| SQL | Parameterised values; table and column names validated. |
| Supply chain | `npm audit`: 0 findings. `pip-audit`: only `chromadb` advisories, none with a patched release; Warren uses only the embedded local client and never starts a Chroma server or enables remote-code embeddings. Re-run `pip-audit -r backend/requirements.txt` regularly. |

Static checks used: `bandit` (no medium/high findings), `ruff` (clean).

Residual risks to be aware of: the session token lives in `localStorage` (standard for SPAs; the strict CSP limits XSS impact), and
the sandbox receives a repository token only for the clone command. Use a dedicated bot account with minimum scopes where possible.

## Run locally

```bash
# backend (Python 3.11+)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env          # fill in; see comments in the file
python run.py                       # http://localhost:5000

# frontend (Node 20+)
cd frontend && npm install && npm run dev   # http://localhost:5173
```

Explore without keys: set `DEMO_SEED=true`, restart, choose **Try the demo** (read-only).
Tests: `cd backend && python -m unittest discover -s tests -v`. Evals: `python evals/run_evals.py`.

Configuration essentials: GitHub OAuth App (callback `http://localhost:5000/api/auth/github/callback`) or a personal access token with
`repo` plus `workflow` (GitHub requires `workflow` to push edits to `.github/workflows`; without it Warren posts the exact patch instead); OmniRoute (`OMNIROUTE_BASE_URL`, `OMNIROUTE_API_KEY`, `LLM_MODEL`; check with
`curl $OMNIROUTE_BASE_URL/models -H "Authorization: Bearer $OMNIROUTE_API_KEY"`); `E2B_API_KEY` for sandbox verification (without it
Warren still reviews and diagnoses, but every CI fix needs approval). Without a public URL the poller replaces webhooks.

## Production

```bash
cp .env.example .env     # set WARREN_ENV=production, SECRET_KEY, GITHUB_WEBHOOK_SECRET, FRONTEND_URL=https://..., SQLITE_PATH=/data/warren.db
docker compose up --build
```

* `backend/Dockerfile` runs gunicorn with **one worker and 32 threads** (job queue, event bus and rate limiter are in-process; scale
  vertically or move them to Redis before adding workers). Data lives in the `warren-data` volume.
* `frontend/nginx.conf` serves the build with security headers and proxies `/api` with buffering off for live streams.
* Terminate TLS in front (add HSTS) and set `TRUST_PROXY=true`.
* Health probe: `GET /healthz`. Back up the volume (SQLite, Chroma, checkpoints).
* The container files were written to standard practice but not built in the authoring environment; build once before relying on them.

## Repo map

```
backend/app/
  __init__.py       app factory, security headers, poller and CI watcher
  config.py         settings and agent guardrails      db.py        SQLite schema, indexes, safe helpers
  auth.py           sessions, stream tickets           crypto.py    token encryption
  ratelimit.py      rate limiter                        jobs.py      cancellable background jobs
  container.py      shared objects, timeline events     seed.py      demo data
  routes/           auth, repos, prs, incidents, security, webhooks, live (SSE)
  services/         github, llm (OmniRoute), sandbox (E2B), vectors (Chroma), security (OSV), ci_logs, codegraph, safety, bus, sync
  agents/           review, autofix, ci_doctor, triage, change_stack, chat, security_scan, refine, tools, prompts, schemas
backend/tests/      pipeline, CI Doctor scenarios, security, contracts     backend/evals/   CI classifier evals
frontend/src/       api, hooks/useLive (SSE), context (Auth, Review), pages, components, styles (dark and light)
```

## Performance notes

SQLite indexes on every hot query; PR sync and repository indexing run in parallel; code is chunked with LangChain's language-aware
splitter; the dashboard updates from push events instead of polling; routes are code-split and PR cards memoised.

## Verification status

Verified: 45 backend tests (review, autofix, chat, every CI scenario above including the wrong-working-directory workflow case and fork PRs, security
controls, real-client contract checks), 16/16 CI classifier evals, all frontend pages render against the live demo API, the stream ticket flow and security headers confirmed on a
running server, production build passes.
Not exercised (needs your credentials or infrastructure): real GitHub OAuth/webhooks/Actions logs, real E2B runs, your OmniRoute
models, Docker builds, and visual polish in a real browser.
