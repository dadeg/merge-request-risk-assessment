# merge-request-bot

A small bot that watches your code-review platform (GitLab or GitHub),
asks an AI to score each merge/pull request's risk, posts a single
markdown comment per head SHA explaining the verdict, and optionally
auto-approves / auto-merges trivial low-risk changes.

This repo is currently configured to read GitLab. Most users will need to customize it
before running it. There's a one-shot AI prompt below that does that
for you.

---

## 1. Customize this repo for your environment (read first)

Clone this repo, then **paste the entire prompt below into your AI
assistant** (Cursor, Claude Code, Copilot Chat, etc.) running in this
directory. The AI will ask you a few questions, then make all the
edits needed to turn this into a generic bot for your provider.

> ### Prompt to feed your AI
>
> ~~~
> You are helping me adapt this merge-request-bot codebase for my own
> environment. It currently has Indeed-specific assumptions (an Indeed
> compliance flag called JSOX_COMPLIANCE, a sibling-repo `danbot` that
> supplies the project list, and hard-coded Indeed GitLab URLs). I
> want a clean, generic bot.
>
> STEP 1 — Ask me these setup questions, then wait for my answers
> before editing anything:
>
>   a. Are you using **GitLab** or **GitHub**? (Pick one.)
>   b. What's the **base URL** of your provider?
>      Examples: `https://gitlab.com`, `https://github.com`,
>      `https://gitlab.mycompany.com`, `https://github.mycompany.com/api/v3`.
>   c. List the **repositories to watch**, one per line. Use
>      `group/repo` for GitLab or `owner/repo` for GitHub.
>   d. Auto-actions: do you want
>        (i)   comment-only (safest), OR
>        (ii)  comment + auto-approve low-risk MRs, OR
>        (iii) comment + auto-approve + auto-merge trivial low-risk MRs?
>   e. AI backend: default is `cursor-agent` CLI in `--print --output-format json`
>      mode. If you'd rather use `claude`, OpenAI API, or something
>      else, tell me and stub the wrapper accordingly.
>
> STEP 2 — Once I've answered, make these edits:
>
> Remove all Indeed-specific code and config:
>   * In `src/mrbot/config.py`:
>     - delete `DANBOT_ENV_PATH`, `danbot_env_path` field,
>       `_read_project_repo_map_from_danbot_env`, the
>       `DEFAULT_PROJECT_REPO_MAP` dict, and the `PROJECT_REPO_MAP`
>       env loading.
>     - delete `LOCAL_REPO_BASE_PATH` and `local_repo_base_path`.
>     - replace all of the above with a single `WATCH_REPOS` env var
>       (comma-separated list of `group/repo` or `owner/repo`).
>   * Delete `src/mrbot/project_resolver.py` (it walks local danbot
>     clones to recover GitLab groups — not needed when the user
>     supplies fully-qualified paths). In `poller.py`, replace the
>     resolver call with a direct `client.get_project(path)` per entry.
>   * Delete `src/mrbot/jsox.py` and every reference to
>     `JSOX_COMPLIANCE`, `is_project_safe_for_auto_action`, and
>     `auto_action_basis` in `src/mrbot/poller.py`.
>   * Replace the JSOX gate in `_maybe_auto_act` with the policy I
>     picked in 1d:
>       (i)   delete `_maybe_auto_act` entirely.
>       (ii)  approve when `verdict.risk == "low"` and
>             `cfg.allow_auto_approve` and author != current user.
>       (iii) same as (ii), plus merge when `verdict.is_trivial` and
>             CI is in {success, skipped, none} and `cfg.allow_auto_merge`.
>   * Strip the `JIRA_*` block from `.env.example` (Indeed-only, the
>     bot doesn't actually use it).
>   * Update default `GITLAB_BASE_URL` in `config.py` and `.env.example`
>     to my answer from 1b.
>
> If I picked GitHub in step 1a, additionally:
>   * Rename `src/mrbot/gitlab/` to `src/mrbot/github/` and rewrite
>     `client.py` against the GitHub REST API. Endpoint mapping:
>       - list open PRs:   `GET /repos/{o}/{r}/pulls?state=open`
>       - PR detail:       `GET /repos/{o}/{r}/pulls/{n}`
>       - PR diff/files:   `GET /repos/{o}/{r}/pulls/{n}/files`
>       - post comment:    `POST /repos/{o}/{r}/issues/{n}/comments`
>       - approve PR:      `POST /repos/{o}/{r}/pulls/{n}/reviews`
>                          with body `{"event":"APPROVE"}`
>       - merge PR:        `PUT /repos/{o}/{r}/pulls/{n}/merge`
>                          with body `{"sha": "<head_sha>"}`
>       - CI status:       `GET /repos/{o}/{r}/commits/{sha}/check-runs`
>                          (or `/status` for legacy commit-status API).
>                          Map to {success, failed, running, none}
>                          using the same in-progress detection
>                          currently in `src/mrbot/gatherer.py`.
>       - file at ref:     `GET /repos/{o}/{r}/contents/{path}?ref={sha}`
>     Use `Authorization: Bearer <token>` and
>     `Accept: application/vnd.github+json` headers.
>   * Rename `GITLAB_TOKEN` → `GITHUB_TOKEN`, `GITLAB_BASE_URL` →
>     `GITHUB_BASE_URL` everywhere (config, .env.example, README, PLAN).
>   * In `prompts/mr_risk.md`, replace "GitLab merge request" with
>     "GitHub pull request" and "MR" with "PR".
>   * In `src/mrbot/comment.py`, change the `risk-bot:` header
>     wording if needed and keep the head_sha marker.
>
> STEP 3 — Regenerate `.env.example` from scratch matching the new
> shape. It must contain:
>   * `<PROVIDER>_TOKEN=` (with a comment linking to where to create
>     one for the chosen provider)
>   * `<PROVIDER>_BASE_URL=` (with my answer from 1b as the default)
>   * `WATCH_REPOS=` (with my answer from 1c as the default)
>   * `POLL_INTERVAL_SECONDS=60`
>   * `INCLUDE_DRAFT_MRS=false`
>   * `ENABLE_COMMENTS=true`
>   * `DRY_RUN=true`        ← keep DRY_RUN on by default for safety
>   * `ALLOW_AUTO_APPROVE=false`  (omit if I picked 1d-i)
>   * `ALLOW_AUTO_MERGE=false`    (omit if I picked 1d-i or 1d-ii)
>   * `CURSOR_AGENT_CMD=cursor-agent`
>   * `CURSOR_AGENT_ARGS_JSON=["-p","--output-format","json","--trust"]`
>   * `CURSOR_AGENT_TIMEOUT_MS=180000`
>   * `STATE_DB_PATH=./state/seen.sqlite`
>   * `AUDIT_LOG_PATH=./logs/audit.jsonl`
>
> STEP 4 — Update `README.md`:
>   * Replace section 1 (this customization prompt section) with a
>     short note saying "this codebase has been customized for
>     <provider> at <base_url>".
>   * Update the Quick start to use the new `<PROVIDER>_TOKEN` env var.
>   * Update the "What it does" section to drop Indeed/JSOX language.
>
> STEP 5 — Update `PLAN.md` to reflect the new (non-Indeed) shape:
>   * Drop the JSOX section.
>   * Replace `PROJECT_REPO_MAP` / `DANBOT_ENV_PATH` /
>     `LOCAL_REPO_BASE_PATH` discussion with the simpler `WATCH_REPOS`
>     model.
>   * Adjust the §3 architecture diagram to remove the local-clone
>     resolver step.
>
> STEP 6 — Run `pip install -e .` and verify imports:
>     python -c "from mrbot.config import load_config; print('ok')"
>
> Then tell me exactly what to put in `.env` and how to launch it.
> ~~~

Once the AI is done, you should be able to:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env       # then edit it: paste your token + repo list
python -m mrbot            # dry-run by default; tail logs/audit.jsonl
```

When the dry-run output looks right, set `DRY_RUN=false` and restart.

---

## 2. Quick start (if you've already customized)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# edit .env: paste your provider token, list the repos to watch

python -m mrbot            # dry-run; logs comments without posting
tail -f logs/audit.jsonl | jq .

# when the dry-run output looks right:
# - flip DRY_RUN=false in .env
# - restart `python -m mrbot`
```

## 3. What it does

For each open MR/PR in the watched repos:

1. Skips if the head SHA has already been evaluated, or if the MR is a
   draft, or if CI is still in progress (deferred — re-checked next
   poll cycle).
2. Pulls the diff, MR metadata, and pipeline status.
3. Sends them through your configured AI (`cursor-agent` by default)
   with the prompt in `prompts/mr_risk.md`. The AI returns strict JSON.
4. Renders a markdown comment listing the verdict (low / medium / high
   risk), confidence, fixable concerns with suggested fixes, inherent
   risks (no automated fix), and a one-line "Suggestion to reviewer".
5. Posts the comment as a regular MR/PR note.
6. Optionally auto-approves (low risk) and/or auto-merges (low risk +
   strictly trivial — only test/doc/comment changes — with green CI),
   subject to `ALLOW_AUTO_APPROVE` / `ALLOW_AUTO_MERGE` env switches.

The bot never approves its own MRs (your provider would reject that
anyway).

## 4. Files

| Path | What |
|---|---|
| `PLAN.md` | full design |
| `.env.example` | config template — your token goes in `.env` |
| `prompts/mr_risk.md` | the AI prompt that drives the risk verdict |
| `src/mrbot/` | source |
| `state/seen.sqlite` | per-head-SHA dedupe |
| `logs/audit.jsonl` | one structured line per decision the bot makes |
