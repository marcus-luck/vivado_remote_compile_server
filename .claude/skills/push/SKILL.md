---
name: push
description: Commit, push and open a pull request for this repository. Use when the user says "push it", "commit this", "ship it", "open a PR", or asks for any git branch, commit, push or pull request operation.
---

# Push

Audience: senior engineers. Write short. Use Simplified Technical English: one
instruction or fact per sentence, active voice, present tense, no filler.

## Rules

- Use the git identity of the user. Do not change the git configuration.
- Do not push to `main`. Each feature has its own branch and its own pull request.
- Do not force-push. Do not skip hooks.
- Add no trailers and no attribution lines to commits or pull requests.
- Do not stage secrets or local state: `.env`, `deploy/caddy.env`,
  `client_secret_*.json`, `data/`, `.venv/`, `.claude/settings.local.json`,
  `CLAUDE.local.md`. Stage `.env.template` changes only.
- Before the first commit, run `uv run ruff format .`, `uv run ruff check .`
  and `uv run pytest`. If a check fails, stop and report.
- If the change alters the API, configuration or deployment, update
  `README.md` and `CLAUDE.md` in the same pull request.

## Names

Verbs: `ADD`, `CHG`, `DOC`, `DEL`, `UPD`, `FIX`.

| Item | Format | Example |
|---|---|---|
| Branch | `<verb>-<what-the-branch-does>`, lower case, hyphens | `add-github-repo-builds`, `fix-token-expiry` |
| Commit | `<VERB>: <what the commit does>`, one line, no body | `ADD: GitHub repository builds` |
| PR title | Same format as a commit | `CHG: Keep artifacts for 30 days` |

## Procedure

1. Read `git status --short` and `git branch --show-current`.
2. If the branch is `main`, create the feature branch: `git switch -c <branch>`.
   If a feature branch is active, continue on it.
3. Run the checks (see Rules).
4. Stage files by name. Make one commit for each logical change.
5. Push: `git push -u origin <branch>`.
6. If the branch has no pull request, open one:
   `gh pr create --base main --title "<title>" --body "<body>"`.
   If one exists, the push updates it. Do not open a second one.
7. Give the pull request URL to the user.

## Pull request body

Use only these sections. Use a maximum of three bullets for each section.

```
## Change
- <what changes>

## Reason
- <why>

## Test
- <pytest and ruff results, manual check on the server, or "not tested">
```
