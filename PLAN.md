# merge-request-bot — PLAN

Standalone bot that watches merge requests on `code.corp.indeed.com`,
judges their risk with AI, and **posts one risk-assessment comment per
new head SHA**. The comment explains the verdict (low/medium/high),
confidence, the specific factors that pushed it above low risk, and —
where practical — concrete actionable items that would bring it down
to low risk.

The bot can additionally **auto-approve** and **auto-merge** in two
distinct situations:

  - **Trivial low-risk MRs** (only test/doc/comment changes) →
    auto-approve + auto-merge **regardless of JSOX status**. Trivial
    changes don't touch any code under JSOX scope, so they bypass the
    compliance gate.
  - **Non-trivial low-risk MRs in JSOX-exempt repos** (those whose
    `.gitlab-ci.yml` *explicitly* declares `JSOX_COMPLIANCE: false|0|no|off`)
    → auto-approve only (never auto-merge non-trivial code).

JSOX is a compliance regime — JSOX-compliant repos *require* human
approvals on non-trivial code. If the flag is missing, ambiguous, or
truthy, mrbot treats the repo as "needs compliance" by default.

Both auto-actions are gated behind separate env kill-switches
(`ALLOW_AUTO_APPROVE`, `ALLOW_AUTO_MERGE`) and inherit `DRY_RUN`.
Comments, approvals, and merges are all attributed to you, posted from
your GitLab Personal Access Token.

| risk | trivial | JSOX flag                      | comment | approve | merge |
|------|---------|--------------------------------|---------|---------|-------|
| low  | yes     | any                            | ✓       | ✓       | ✓ (if CI ok) |
| low  | no      | explicit `false`/`0`/`no`/`off`| ✓       | ✓       | – |
| low  | no      | `true`/`1`/missing/unknown     | ✓       | –       | – |
| med  | –       | any                            | ✓       | –       | – |
| high | –       | any                            | ✓       | –       | – |

---

## 1. Goals

- Continuously poll a configured list of GitLab projects for **open
  merge requests**.
- For each new MR (and each new push to an MR we've already seen),
  gather the diff and metadata, send it to an AI risk scorer, and
  post **one** comment summarising the verdict.
- The comment is structured: risk level, confidence, why-not-low list,
  and an actionable "to make this low risk, do X" list when feasible.
- Be deduplicated by head SHA — never spam the same SHA twice.
- Be safe by default: never comment on your own MRs, respect `DRY_RUN`.

## 2. Non-goals

- No patch suggestions / no code-writing.
- No DMs, Slack, escalation files, or human-review hand-off.
- No CI orchestration. CI status is a *signal*, not something we trigger.
- No webhook server in v1 — pure polling.
- No auto-approve or auto-merge of *non-trivial* changes in JSOX-
  compliant repos (those need human approvals by policy). Trivial
  changes (test/doc/comment only) bypass JSOX and are still
  auto-actioned.

## 3. Architecture

```text
┌───────────────────────────────────────────────────────────┐
│  Project Resolver (once at boot)                          │
│   For each value in PROJECT_REPO_MAP (shared with danbot):│
│     1. read `git remote get-url origin` from              │
│        LOCAL_REPO_BASE_PATH/<repo_name>/.git              │
│     2. parse origin → group/project                       │
│     3. fall back to GET /projects?search=<repo_name> and  │
│        find an exact path match                           │
│     4. confirm with GET /projects/:path → project_id      │
│   Cache the (path_with_namespace, project_id) tuples.     │
└──────────────┬────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────┐
│  Poller (every N seconds)                                 │
│   For each resolved project:                              │
│     GET /projects/:id/merge_requests                      │
│       ?state=opened&scope=all                             │
└──────────────┬────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────┐
│  Dedupe (state/seen.sqlite)                               │
│   key = (project_id, mr_iid, head_sha)                    │
│   Skip if we've already commented on this exact head SHA. │
└──────────────┬────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────┐
│  Context Gatherer (REST):                                 │
│   - MR metadata: title, description, author, target,      │
│     labels, draft flag                                    │
│   - Diff (full text, file list, additions/deletions)      │
│   - CI status (pipeline state, failed jobs)               │
└──────────────┬────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────┐
│  Risk Scorer (cursor-agent call)                          │
│   prompts/mr_risk.md + gathered context                   │
│   strict-JSON output                                      │
└──────────────┬────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────┐
│  Comment Builder + Gate                                   │
│   - format the JSON verdict into a markdown comment       │
│   - skip if author is current user, or DRY_RUN, or rate-  │
│     limited                                               │
└──────────────┬────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────┐
│  Action: POST /projects/:id/merge_requests/:iid/notes     │
└──────────────┬────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────┐
│  Auto-actions (lazy, only when verdict.risk == "low"):    │
│   1. If verdict.is_trivial → eligible (basis="trivial").  │
│      Else fetch .gitlab-ci.yml; eligible only if it       │
│      explicitly declares `JSOX_COMPLIANCE: false|0|no|off`│
│      (basis="jsox-exempt"). Truthy/missing/404 → not      │
│      eligible.                                            │
│   2. If eligible AND ALLOW_AUTO_APPROVE                   │
│        → POST .../merge_requests/:iid/approve (sha pinned)│
│   3. If verdict.is_trivial (regardless of JSOX)           │
│      AND CI in {success, skipped, none}                   │
│      AND ALLOW_AUTO_MERGE                                 │
│        → PUT .../merge_requests/:iid/merge (sha pinned)   │
└──────────────┬────────────────────────────────────────────┘
               ▼
   logs/audit.jsonl  +  state/seen.sqlite
```

## 4. Repo layout

```text
merge-request-bot/
├── README.md
├── PLAN.md                            # this file
├── .env.example
├── .env                               # gitignored — your PAT lives here
├── .gitignore
├── pyproject.toml
├── src/mrbot/
│   ├── __init__.py
│   ├── __main__.py                    # main loop entrypoint
│   ├── config.py                      # loads .env
│   ├── poller.py                      # GitLab MR polling loop
│   ├── project_resolver.py            # PROJECT_REPO_MAP → group/project
│   ├── jsox.py                        # JSOX_COMPLIANCE detector (true=needs compliance)
│   ├── gatherer.py                    # diff, CI, metadata
│   ├── risk.py                        # cursor-agent invocation + JSON parse
│   ├── gate.py                        # decide post / skip and why
│   ├── comment.py                     # render verdict into markdown
│   ├── ai/
│   │   ├── __init__.py
│   │   └── cursor_agent.py            # subprocess wrapper
│   ├── gitlab/
│   │   ├── __init__.py
│   │   └── client.py                  # REST client (httpx)
│   ├── actions/
│   │   ├── __init__.py
│   │   ├── post_comment.py            # POST .../notes
│   │   ├── approve.py                 # POST .../approve
│   │   └── merge.py                   # PUT .../merge
│   └── state/
│       ├── __init__.py
│       ├── store.py                   # SQLite
│       └── schema.sql
├── prompts/
│   └── mr_risk.md                     # the risk taxonomy + examples
├── logs/audit.jsonl
└── state/seen.sqlite
```

## 5. The deterministic gate

Auto-comment is allowed only when **all** of these hold:

```text
ENABLE_COMMENTS == true
mr.author_id != current_user_id            # don't talk to yourself
mr.draft == false                          # unless INCLUDE_DRAFT_MRS=true
ci.state is terminal                       # see below — defer if running
we have not yet commented on this head_sha
DRY_RUN == false                           # if true, log and skip
```

Anything else ⇒ log a `skipped_*` decision and move on.

**Terminal vs deferred skips.** Some skips are terminal — we record the
head SHA in `seen_mrs` and never look at it again (e.g. draft,
dry-run). Others are *deferred* — we audit the skip but deliberately
do **not** mark the SHA seen, so the next polling cycle re-evaluates
it:

| skip reason            | terminal? | why |
|------------------------|-----------|-----|
| `skipped_draft`        | terminal  | re-evaluated when SHA changes |
| `skipped_disabled`     | terminal  | flip `ENABLE_COMMENTS` and restart |
| `skipped_dry_run`      | terminal  | comment was built; flip `DRY_RUN` and restart for new SHAs |
| `skipped_ci_in_progress` | **deferred** | wait for CI to settle |
| `error` (AI/network)   | **deferred** | transient cursor-agent or GitLab blip |

CI in-progress states (defer): `created`, `waiting_for_resource`,
`preparing`, `pending`, `running`, `scheduled`. Anything else
(`success`, `failed`, `canceled`, `skipped`, `manual`, `none`) is
treated as terminal and we proceed to score + comment.

The AI verdict itself doesn't gate posting — once we proceed past the
gate we comment exactly once per head SHA. The risk level changes the
*content* of the comment, not whether we post.

## 6. Risk-judgment prompt (`prompts/mr_risk.md`)

```text
You are reviewing a GitLab merge request to decide ONE thing: how risky is
it to merge this MR right now, given what you can see?

Output strict JSON, nothing else:
{
  "risk": "low" | "medium" | "high",
  "confidence": "low" | "medium" | "high",
  "summary": "<one-sentence plain-English summary of what this MR does>",
  "reasons": [
    "<short bullet describing one specific factor in your verdict>",
    ...
  ],
  "blocking_factors": [
    {
      "factor": "<the specific thing that pushed risk above low, e.g. 'changes public API contract' or 'modifies db migration'>",
      "evidence": "<the file path or snippet from the diff that shows it>",
      "actionable_fix": "<concrete suggestion that would remove this factor, or empty string if no practical fix exists>"
    },
    ...
  ]
}

`blocking_factors` MUST be empty if and only if `risk` == "low".
For medium and high risk, list every factor that contributed.
For each factor, give an `actionable_fix` only when there's a *practical*
change the author could make in this MR (e.g. "split the migration into
its own MR", "add a feature flag default-off", "add a test covering the
new branch in foo.py:42"). If the factor is intrinsic to the change
(e.g. "this is a payments-system change, by nature high risk"), set
`actionable_fix` to "" (empty string).

A change is LOW RISK when ALL of these hold:
- Diff is small (< ~150 net lines changed) and scoped to a single concern.
- CI is green; tests exist and were updated/added when behavior changed.
- No production data migration, schema change, or destructive SQL.
- No change to authn/authz, secrets handling, crypto, or PII flow.
- No change to public APIs, contracts, message schemas, or wire formats.
- No change to billing, money math, invoice totals, tax, or rounding.
- No change to retry/timeout/circuit-breaker/queue config in hot paths.
- No infrastructure-as-code, Terraform, Helm, or deploy-pipeline change.
- No new dependency, especially not transitive across major versions.
- No feature flag flipped to "on" by default for prod.
- Reverting is trivially safe (git revert with no data implications).
- Blast radius is contained to a single service / module.

Examples of LOW-RISK changes:
- Typo fix in a comment, log message, README, or docstring.
- Renaming a private variable/method with all call sites updated.
- Bumping a patch version of a dev/test-only dependency with green CI.
- Adding a unit test that exercises an existing branch.
- Adding a log line at INFO/DEBUG in a non-hot-path code path.
- Tightening a private type signature when callers already comply.
- Removing dead code that has zero references in the repo.
- Updating fixture data used only by tests.
- Adjusting a non-prod config (dev/stage) value.
- Pure formatting / linter-driven changes with no semantic diff.

A change is MEDIUM RISK when ANY of these hold:
- Behavior change in business logic but well-tested and reversible.
- New endpoint behind a feature flag defaulting off.
- Refactor that touches multiple files but preserves observable behavior.
- Library upgrade within the same minor version with green CI.
- Performance optimization in a non-hot path.
- New cron with conservative schedule and clear shutoff switch.
- Adds a new external call (HTTP, DB, queue) with timeout and retry set.
- Touches a moderately hot path but with adequate test coverage.

A change is HIGH RISK when ANY of these hold:
- Touches authentication, authorization, session, or token handling.
- Touches payments, billing, invoicing, money totals, tax, FX, or rounding.
- Database schema change, migration, backfill, or destructive SQL.
- Public/external API contract change or message-schema change.
- Infrastructure-as-code, Terraform, Helm chart, deploy pipeline, or
  Kubernetes manifest changes.
- New or upgraded dependency that crosses a major version, or any new
  dependency without security review evidence.
- Feature flag turned ON for prod by default.
- Concurrency primitives (locks, queues, retries, idempotency keys) in a
  hot path.
- Caching layer changes that affect correctness boundaries.
- Anything PII, GDPR, SOX, or audit-controlled.
- CI is red, missing, or coverage dropped on a critical module.
- Diff is large (> ~400 lines) or spans many unrelated concerns.
- Touches a directory with RUNBOOK / INCIDENT / POSTMORTEM markers in it.

Hard rules:
- If you cannot see the diff, return risk="high", confidence="low",
  blocking_factors=[{factor:"diff unavailable", evidence:"", actionable_fix:""}].
- If CI status is unknown or red, that itself is a blocking_factor on
  any non-low verdict, with actionable_fix "fix CI before merging".
- If the MR description is empty AND the diff isn't trivial, downgrade
  confidence by one level and add a blocking_factor with
  actionable_fix "add an MR description explaining intent and scope".
- Do not invent file names, function names, or quotes that aren't in the
  provided context.

Now evaluate the MR provided in context.
```

## 7. Comment shape

Posted as a single MR note (not an inline diff comment) so it's easy to
locate and easy to skim. The renderer splits risk factors into two
groups so reviewers don't get told to "fix" something that's the point
of the MR:

- **Fixable concerns** — the AI proposed a concrete in-MR fix.
- **Inherent risks** — no fix is proposed because the risky change is
  intentional (e.g. removing a deprecated public endpoint, dropping a
  table) or inherent to the change type. Reviewer must read carefully
  but there's nothing for the author to change.

The bottom-line "Suggestion to reviewer" adapts to which groups are
present.

Example with both groups:

```markdown
**risk-bot:** **HIGH** risk · confidence: high

_Removes the deprecated /v1/users endpoint and bumps a dep across a
major version._

**Fixable concerns**

- **major-version dependency upgrade** — `package.json`: `react ^17 → ^18`
  _Suggested fix:_ pin to ^18.0.0 and add a CHANGELOG note explaining
  what callers need to migrate.

**Inherent risks** _(no in-MR fix; review carefully)_

- **removes a public HTTP API endpoint** — `src/api/v1/users.py`
  deletes the `/v1/users` route. This is the stated purpose of the MR.

**Suggestion to reviewer:** Ask for the suggested fixes above, then
review the inherent risks carefully before approving.

<!-- mrbot: head_sha=abc1234 -->
```

For **low risk** verdicts the comment is short:

```markdown
**risk-bot:** **LOW** risk · confidence: high

_<one-sentence summary>_

No risk factors found.

**Suggestion to reviewer:** Approve.

<!-- mrbot: head_sha=abc1234 -->
```

The trailing HTML comment carries the head SHA so we can detect "is
this comment mine for this SHA" without keeping state — but we still
primarily dedupe via SQLite.

## 8. Config (`.env.example`)

This is the only file you need to edit to get going. Copy to `.env` and
fill in your token.

```env
# ============ GitLab (your PAT) ============
GITLAB_BASE_URL=https://code.corp.indeed.com
# Personal Access Token, scope: api. Get one at:
#   https://code.corp.indeed.com/-/user_settings/personal_access_tokens
GITLAB_TOKEN=
# Auto-resolved at first boot via GET /user; you can leave blank.
GITLAB_USER_ID=

# ============ What to watch ============
# Repo allowlist resolution order (first match wins):
#   1. PROJECT_REPO_MAP env var (explicit narrowing override)
#   2. PROJECT_REPO_MAP line read from DANBOT_ENV_PATH (canonical source,
#      same pattern as ../slack-bot uses)
#   3. Bundled DEFAULT_PROJECT_REPO_MAP in src/mrbot/config.py
PROJECT_REPO_MAP=
LOCAL_REPO_BASE_PATH=/Users/ddegreef/indeed-danbot

# ============ Reuse from danbot (PROJECT_REPO_MAP source) ============
DANBOT_ENV_PATH=/Users/ddegreef/indeed/danbot/.env

GITLAB_POLL_INTERVAL_SECONDS=30
GITLAB_INCLUDE_DRAFT_MRS=false

# ============ Comment behavior ============
ENABLE_COMMENTS=true               # global kill-switch for posting
DRY_RUN=true                       # if true, build + log comments, do not post

# ============ Auto-actions ============
# Approve when LOW-risk + (trivial OR JSOX_COMPLIANCE explicitly false).
ALLOW_AUTO_APPROVE=false
# Merge when LOW-risk + trivial + CI green/skipped/none (any JSOX).
ALLOW_AUTO_MERGE=false

# ============ AI brain (cursor-agent CLI) ============
CURSOR_AGENT_CMD=cursor-agent
CURSOR_AGENT_ARGS_JSON=["-p","--output-format","json","--trust"]
CURSOR_AGENT_TIMEOUT_MS=180000

# ============ Storage ============
STATE_DB_PATH=./state/seen.sqlite
AUDIT_LOG_PATH=./logs/audit.jsonl
```

## 9. State schema

```sql
CREATE TABLE IF NOT EXISTS seen_mrs (
  project_id INTEGER NOT NULL,
  mr_iid     INTEGER NOT NULL,
  head_sha   TEXT    NOT NULL,
  decided_at INTEGER NOT NULL,
  decision   TEXT    NOT NULL,
  risk       TEXT,
  confidence TEXT,
  PRIMARY KEY (project_id, mr_iid, head_sha)
);
CREATE TABLE IF NOT EXISTS rate_log (
  ts     INTEGER NOT NULL,
  action TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_log_ts ON rate_log(ts);
```

`decision` (in `seen_mrs`) is one of: `commented`, `skipped_dry_run`,
`skipped_draft`, `skipped_already_seen`, `skipped_ci_in_progress`,
`skipped_disabled`, `error`, `error_forbidden`. The `error_forbidden`
flavor is used when the GitLab API returns 401/403/404 on the
comment-post — those won't fix themselves, so we mark the SHA seen and
stop pestering. Other deferred
decisions also write to `logs/audit.jsonl` (with `"deferred": true`)
but are not inserted into `seen_mrs`.

The audit log additionally carries auto-action records that are NOT
mirrored into `seen_mrs` (the head SHA is already there from the
`commented` row): `approved`, `approve_failed`, `merged`,
`merge_failed`, `merge_skipped_ci`.

## 10. Main loop (pseudo-code)

```python
while True:
    for project in config.allowlist:
        for mr in gitlab.list_open_mrs(project):
            if seen(mr.project_id, mr.iid, mr.sha):
                continue
            if mr.draft and not config.include_drafts:
                record(mr, "skipped_draft"); continue

            ctx     = gather_context(mr)
            verdict = risk_score(ctx)
            body    = render_comment(verdict, mr.sha)

            if not config.enable_comments:
                record(mr, "skipped_disabled", verdict); continue
            if config.dry_run:
                record(mr, "skipped_dry_run", verdict); continue

            gitlab.post_mr_note(mr, body)
            record(mr, "commented", verdict)

            # GitLab forbids self-approval, so self-authored MRs only get a comment.
            if verdict.risk == "low" and mr.author_id != config.user_id and \
               (config.allow_auto_approve or config.allow_auto_merge):
                eligible = verdict.is_trivial or \
                    jsox.is_project_safe_for_auto_action(client, mr.project_id, mr.sha)
                if eligible:
                    if config.allow_auto_approve:
                        gitlab.approve_mr(mr.project_id, mr.iid, sha=mr.sha)
                        audit("approved")
                    if config.allow_auto_merge and verdict.is_trivial \
                       and mr.ci_state in {"success", "skipped", "none"}:
                        gitlab.merge_mr(mr.project_id, mr.iid, sha=mr.sha)
                        audit("merged")
    sleep(config.poll_interval_seconds)
```

## 11. First-run plan

1. Create your GitLab PAT (scope: `api`) and paste it into `.env` as
   `GITLAB_TOKEN`.
2. Confirm `LOCAL_REPO_BASE_PATH` points at your danbot clone tree (so
   the resolver can read each repo's `git remote get-url origin`).
3. Leave `ALLOW_AUTO_APPROVE` and `ALLOW_AUTO_MERGE` at their default
   (`false`) initially.
4. Boot with `DRY_RUN=true` (the default). The bot will resolve every
   repo in `PROJECT_REPO_MAP` to its real GitLab `group/project` path,
   then build comments and write them to `logs/audit.jsonl` without
   posting.
5. Read a handful of dry-run comments. If the tone matches what you'd
   say by hand, flip `DRY_RUN=false`. Real comments start posting.
6. Once comments look reliable, flip `ALLOW_AUTO_APPROVE=true`.
   Approvals fire on low-risk MRs that are EITHER trivial (test/doc/
   comment only) OR in JSOX-exempt repos (those whose `.gitlab-ci.yml`
   explicitly declares `JSOX_COMPLIANCE: false|0`).
7. Once approvals look reliable, flip `ALLOW_AUTO_MERGE=true`. Merges
   fire only on low-risk + trivial MRs (regardless of JSOX) with green
   (or absent) CI.
8. To narrow the watch list temporarily, set `PROJECT_REPO_MAP` in
   `.env` to a small JSON object, e.g. `{"INVSVC":"invoice-service"}`.

## 12. Open questions (for after implementation)

1. Should the bot only consider MRs where you're a reviewer/assignee,
   or every open MR in the watched projects? Default: every open MR.
2. Want a per-project override for `INCLUDE_DRAFT_MRS`, or is the
   global config fine? Default: global.
3. Should the bot edit/replace its previous comment when a new push
   produces a different verdict, instead of adding a fresh comment per
   SHA? Default: append a new comment per SHA so history is preserved.
4. Want a `MIN_DIFF_LINES` threshold to skip trivial MRs entirely
   (e.g. don't bother commenting on README typo MRs)? Default: comment
   on everything.
5. When `LOCAL_REPO_BASE_PATH/<name>` is missing, we fall back to a
   GitLab project search. If multiple projects share the same path
   suffix this could be ambiguous; we currently take the exact-`path`
   match. Want a hard error instead, so a missing clone is never
   silently substituted?
