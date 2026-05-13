# merge-request-bot — PLAN

A small bot that watches GitLab merge requests, judges their risk with
an AI, and posts **one risk-assessment comment per new head SHA**. The
comment explains the verdict (low/medium/high), confidence, the
specific factors that pushed it above low risk, and — where practical —
concrete actionable items that would bring it down to low risk.

The bot can additionally **auto-approve** and **auto-merge** when:

- **Auto-approve** — `verdict.risk == "low"` AND author isn't you (the
  GitLab API forbids self-approval anyway).
- **Auto-merge** — additionally, `verdict.is_trivial` (only test/doc/
  comment changes) AND CI is green/skipped/none.

Both auto-actions are gated behind separate env kill-switches
(`ALLOW_AUTO_APPROVE`, `ALLOW_AUTO_MERGE`) and inherit `DRY_RUN`.
Comments, approvals, and merges are all attributed to you, posted from
your GitLab Personal Access Token.

| risk | trivial | comment | approve (if enabled) | merge (if enabled) |
|------|---------|---------|----------------------|---------------------|
| low  | yes     | ✓       | ✓                    | ✓ (if CI ok)        |
| low  | no      | ✓       | ✓                    | –                   |
| med  | –       | ✓       | –                    | –                   |
| high | –       | ✓       | –                    | –                   |

---

## 1. Goals

- Continuously poll a configured list of GitLab projects (`WATCH_REPOS`)
  for **open merge requests**.
- For each new MR (and each new push to an MR we've already seen),
  gather the diff and metadata, send it to an AI risk scorer, and
  post **one** comment summarising the verdict.
- The comment is structured: risk level, confidence, fixable concerns
  (with suggested fixes), inherent risks (no fix because it's the
  point of the MR), and a one-line "Suggestion to reviewer".
- Be deduplicated by head SHA — never spam the same SHA twice.
- Be safe by default: never approve your own MRs, respect `DRY_RUN`,
  never approve high-risk MRs.

## 2. Non-goals

- No patch suggestions / no code-writing.
- No DMs, Slack, escalation files, or human-review hand-off.
- No CI orchestration. CI status is a *signal*, not something we trigger.
- No webhook server in v1 — pure polling.

## 3. Architecture

```text
┌───────────────────────────────────────────────────────────┐
│  Project Resolver (once at boot)                          │
│   For each path in WATCH_REPOS:                           │
│     GET /projects/<url-encoded-path> → project_id         │
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
│  Auto-actions (only when verdict.risk == "low" and        │
│   author is not the current user):                        │
│   1. If ALLOW_AUTO_APPROVE                                │
│        → POST .../merge_requests/:iid/approve (sha pinned)│
│   2. If verdict.is_trivial                                │
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

Self-authored MRs get a comment but not an approve/merge — the GitLab
API forbids self-approval anyway. That check lives in the auto-action
step, not the gate.

## 6. Risk-judgment prompt (`prompts/mr_risk.md`)

The full prompt lives in `prompts/mr_risk.md`. Schema:

```text
{
  "risk": "low" | "medium" | "high",
  "confidence": "low" | "medium" | "high",
  "summary": "<one-sentence plain-English summary of what this MR does>",
  "is_trivial": true | false,
  "reasons": [ "<short bullet>", ... ],
  "risk_factors": [
    {
      "factor": "<the specific thing that pushed risk above low>",
      "evidence": "<file path or snippet from the diff>",
      "actionable_fix": "<concrete suggestion, or empty if no practical fix>",
      "intent_aligned": true | false
    }
  ]
}
```

`is_trivial` is restricted: ONLY tests, ONLY docs, ONLY code comments,
or any combination of the three. Anything that touches production code
(even a one-liner), config, build files, dependencies, CI yaml, infra,
schema, or runtime fixtures → `false`. The prompt explicitly tells the
model to default to `false` when uncertain.

## 7. Comment shape

Posted as a single MR note (not an inline diff comment) so it's easy
to locate and easy to skim. The renderer splits risk factors into two
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

**Suggestion to reviewer:** Suggest the fixes above to the author, and
carefully consider the inherent risks before approving.

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

Trivial low-risk verdicts also get a discreet `· trivial` badge in the
header, signalling that mrbot considers this a candidate for auto-merge.

The trailing HTML comment carries the head SHA so we can detect "is
this comment mine for this SHA" without keeping state — but we still
primarily dedupe via SQLite.

## 8. Config (`.env.example`)

This is the only file you need to edit to get going. Copy to `.env` and
fill in your token + repo list.

```env
# ============ GitLab (your PAT) ============
GITLAB_BASE_URL=https://gitlab.com
GITLAB_TOKEN=
GITLAB_USER_ID=

# ============ What to watch ============
# Comma-separated list of `group/repo` (or `group/sub/repo`) paths.
WATCH_REPOS=group/repo1,group/repo2

POLL_INTERVAL_SECONDS=60
INCLUDE_DRAFT_MRS=false

# ============ Comment behavior ============
ENABLE_COMMENTS=true               # global kill-switch for posting
DRY_RUN=true                       # if true, build + log comments, do not post

# ============ Auto-actions ============
ALLOW_AUTO_APPROVE=false           # approve LOW-risk MRs (not your own)
ALLOW_AUTO_MERGE=false             # also merge LOW-risk + trivial MRs

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
```

`decision` (in `seen_mrs`) is one of: `commented`, `skipped_dry_run`,
`skipped_draft`, `skipped_already_seen`, `skipped_ci_in_progress`,
`skipped_disabled`, `error`, `error_forbidden`. `error_forbidden` is
used when the GitLab API returns 401/403/404 on the comment-post —
those won't fix themselves, so we mark the SHA seen and stop pestering.
Other deferred decisions also write to `logs/audit.jsonl` (with
`"deferred": true`) but are not inserted into `seen_mrs`.

The audit log additionally carries auto-action records that are NOT
mirrored into `seen_mrs` (the head SHA is already there from the
`commented` row): `approved`, `approve_failed`, `merged`,
`merge_failed`, `merge_skipped_ci`.

## 10. Main loop (pseudo-code)

```python
while True:
    for project in resolved_watch_repos:
        for mr in gitlab.list_open_mrs(project.id):
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
            if verdict.risk == "low" and mr.author_id != config.user_id:
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
2. Set `WATCH_REPOS` to a small list (one or two low-traffic repos).
3. Leave `ALLOW_AUTO_APPROVE` and `ALLOW_AUTO_MERGE` at their default
   (`false`) initially.
4. Boot with `DRY_RUN=true` (the default). The bot will resolve every
   repo in `WATCH_REPOS` to a project ID, then build comments and
   write them to `logs/audit.jsonl` without posting.
5. Read a handful of dry-run comments. If the tone matches what you'd
   say by hand, flip `DRY_RUN=false`. Real comments start posting.
6. Once comments look reliable, flip `ALLOW_AUTO_APPROVE=true`.
   Approvals fire on low-risk MRs whose author isn't you.
7. Once approvals look reliable, flip `ALLOW_AUTO_MERGE=true`. Merges
   fire only on low-risk + trivial MRs with green (or absent) CI.
8. Expand `WATCH_REPOS` one repo at a time.

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
