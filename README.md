# merge-request-bot

A small bot that watches GitLab merge requests, asks an AI to score
each MR's risk, and posts a single markdown comment per head SHA
explaining the verdict. Optionally also auto-approves low-risk MRs and
auto-merges trivial low-risk ones (test/doc/comment-only changes) when
CI is green.

Comments, approvals, and merges are all attributed to you, posted from
your GitLab Personal Access Token.

---

## Quick start

```bash
# 1. install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. configure
cp .env.example .env
# edit .env: paste your GitLab PAT (scope: api) into GITLAB_TOKEN
# and list the repos to watch in WATCH_REPOS, e.g.
#   WATCH_REPOS=mygroup/repo1,mygroup/sub/repo2

# 3. boot in dry-run (default; logs comments without posting)
python -m mrbot

# 4. when the dry-run output looks right, flip DRY_RUN=false in .env
#    and restart. To turn on auto-approve / auto-merge, flip
#    ALLOW_AUTO_APPROVE / ALLOW_AUTO_MERGE in .env.
```

Tail what the bot is doing:

```bash
tail -f logs/audit.jsonl | jq .
```

## What it does

For each open MR in `WATCH_REPOS`:

1. Skips if the head SHA has already been evaluated, or it's a draft,
   or CI is still in progress (deferred — re-checked next poll cycle).
2. Pulls the diff, MR metadata, and pipeline status from GitLab.
3. Sends them through your configured AI (`cursor-agent` by default)
   with the prompt in `prompts/mr_risk.md`. The AI returns strict JSON.
4. Renders a markdown comment listing the verdict (low / medium / high
   risk), confidence, fixable concerns with suggested fixes, inherent
   risks (no automated fix), and a one-line "Suggestion to reviewer".
5. Posts the comment as a regular MR note.
6. Optionally auto-approves (low risk, not your own MR) and/or
   auto-merges (low risk + strictly trivial — only test/doc/comment
   changes — with green CI), subject to `ALLOW_AUTO_APPROVE` /
   `ALLOW_AUTO_MERGE` env switches.

The bot never approves its own MRs (GitLab forbids self-approval anyway).

See `PLAN.md` for the full design.

---

## Adapt for GitHub

This codebase is wired for GitLab. To use it against GitHub instead,
clone the repo, then **paste the entire prompt below into your AI
assistant** (Cursor, Claude Code, Copilot Chat, etc.) running in the
repo's directory.

> ### Prompt to feed your AI
>
> ~~~
> You are helping me adapt this merge-request-bot codebase from GitLab
> to GitHub. The bot currently uses the GitLab REST API to watch MRs;
> I want it to use the GitHub REST API to watch PRs.
>
> STEP 1 — Ask me these setup questions, then wait for my answers
> before editing anything:
>
>   a. What's the **base URL** of your GitHub instance?
>      Examples: `https://github.com` (with API at `https://api.github.com`),
>      `https://github.mycompany.com/api/v3` (GitHub Enterprise).
>   b. List the **repositories to watch**, one per line, as
>      `owner/repo`.
>   c. AI backend: default is `cursor-agent` CLI in
>      `--print --output-format json` mode. If you'd rather use
>      `claude`, OpenAI API, or something else, tell me and stub the
>      wrapper accordingly.
>
> STEP 2 — Once I've answered, make these edits:
>
>   * Rename `src/mrbot/gitlab/` → `src/mrbot/github/` and rewrite
>     `client.py` against the GitHub REST API. Endpoint mapping:
>       - list open PRs:   `GET /repos/{o}/{r}/pulls?state=open`
>       - PR detail:       `GET /repos/{o}/{r}/pulls/{n}`
>       - PR diff/files:   `GET /repos/{o}/{r}/pulls/{n}/files`
>       - post comment:    `POST /repos/{o}/{r}/issues/{n}/comments`
>       - approve PR:      `POST /repos/{o}/{r}/pulls/{n}/reviews`
>                          with body `{"event": "APPROVE"}`
>       - merge PR:        `PUT /repos/{o}/{r}/pulls/{n}/merge`
>                          with body `{"sha": "<head_sha>"}`
>       - CI status:       `GET /repos/{o}/{r}/commits/{sha}/check-runs`
>                          (or `/status` for legacy commit-status API).
>                          Map to {success, failed, running, none}
>                          using the same in-progress detection
>                          currently in `src/mrbot/gatherer.py`.
>     Use `Authorization: Bearer <token>` and
>     `Accept: application/vnd.github+json` headers.
>   * Rename `GITLAB_TOKEN` → `GITHUB_TOKEN`, `GITLAB_BASE_URL` →
>     `GITHUB_BASE_URL` everywhere (config, .env.example, README, PLAN).
>     Update the default base URL to my answer from 1a.
>   * Replace `WATCH_REPOS` semantics: same env var name, but the
>     entries are now `owner/repo`.
>   * In `prompts/mr_risk.md`, replace "GitLab merge request" with
>     "GitHub pull request" and "MR" with "PR" wherever it appears in
>     prose. The JSON schema, taxonomy, examples, and trivial-detection
>     rules can stay as-is.
>   * In `src/mrbot/comment.py`, the head-SHA marker comment can stay;
>     it's provider-agnostic.
>   * In `src/mrbot/poller.py`, `_resolve_watch_repos` already calls
>     `client.get_project(path)` — rename the method to whatever maps
>     onto GitHub's "get repo" endpoint and adjust the field
>     extraction (`id` and `full_name` instead of `id` and
>     `path_with_namespace`).
>
> STEP 3 — Regenerate `.env.example` from scratch matching the new
> shape (with `GITHUB_TOKEN`, `GITHUB_BASE_URL`, `WATCH_REPOS=owner/repo`
> example, and the same comment/auto-action/AI/storage blocks).
>
> STEP 4 — Update `README.md`:
>   * Replace the "Quick start" GitLab references with GitHub.
>   * Drop this "Adapt for GitHub" section (you're done adapting) or
>     replace it with a short "Adapted from the upstream GitLab
>     version" note.
>   * Update the "What it does" wording (MR → PR, GitLab → GitHub).
>
> STEP 5 — Update `PLAN.md` to swap GitLab terminology for GitHub
> terminology and update the architecture diagram URLs/endpoints to
> match GitHub's REST API.
>
> STEP 6 — Run `pip install -e .` and verify imports:
>     python -c "from mrbot.config import load_config; print('ok')"
>
> Then tell me exactly what to put in `.env` and how to launch it.
> ~~~

---

## Files

| Path | What |
|---|---|
| `PLAN.md` | full design |
| `.env.example` | config template — your token goes in `.env` |
| `prompts/mr_risk.md` | the AI prompt that drives the risk verdict |
| `src/mrbot/` | source |
| `state/seen.sqlite` | per-head-SHA dedupe |
| `logs/audit.jsonl` | one structured line per decision the bot makes |
