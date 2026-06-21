# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A guarded, LLM-first pilot that drives a **human-owned, visible Chromium** (over CDP on loopback, viewed via VNC) to evaluate BOSS Zhipin (zhipin.com) job postings and, only after many safety gates, click the single "立即沟通" contact button on one job. It also watches the recruiter inbox and decides whether to send a resume. Candidate matching is delegated to an LLM, not hard-coded. **The guardrails are the point of this project — preserve them.**

## Toolchain & commands

Package manager is **uv** (not pip/poetry); everything runs through `uv run`. `requires-python >=3.13`; build backend is `uv_build`.

- Setup: `uv sync` (or `uv sync --all-groups`); `uv run playwright install chromium` (only on the host that runs the browser)
- Run the CLI: `uv run resume-pilot <cmd>` (equivalently `python -m resume_pilot`). Subcommands: `doctor`, `browser {start,status,stop}`, `profile extract`, `run`, `inbox watch`, `pauses {list,resolve}`.
- Test: `uv run pytest` (config already sets `-q`, `testpaths=tests`). Use `--decision-fixture {apply,skip,needs_review}` for deterministic local decisions (rejected if combined with `--execute`).
- Lint: `uv run ruff check .` (autofix: `--fix`)

## Code style

- ruff, **line length 100** (not the 88 default); rule sets `E,F,I,UP,B,SIM`; `target-version = py313`.
- Every module starts with `from __future__ import annotations`.

## Safety guardrails — do not weaken these

This automates a live third-party platform. The design is deliberately defensive; treat the following as invariants, not incidental code:

- **Dry-run is the default.** `run`, `inbox watch`, and `profile extract` take no real action without `--execute`; `run --execute` *also* requires `--confirm-live-contact`. Passing `--html-file` can never click. **Never trigger a live `--execute` / live contact unless the human explicitly asks for it in that session.**
- **The live click path is intentionally narrow** (`cli.py` + `runner.py`): exactly one job on the page, an allow-listed `https` BOSS host with path `/web/geek/` or `/job_detail/`, no page risks, an LLM `apply` decision with confidence ≥ `MIN_APPLY_CONFIDENCE` (0.75) and zero risk flags, daily cap free, and exactly one visible 立即沟通 control inside the selected `.job-detail-box`. Don't loosen any of these.
- **Pauses block everything.** Any ambiguity (login/captcha/security wall, multiple resumes, non-unique button, nav drift) raises `HumanPauseRequired` and writes a `pauses` row; an unresolved pause blocks all future live runs (both `cli.py` and `autonomous_apply.py`). Clear it only via `resume-pilot pauses resolve` after a human handles it in VNC — never auto-clear to "unblock".
- **Click-failure semantics are deliberate** (`cdp.py`, `runner.py`, `autonomous_apply.py`): a *pre-click* abort (no mouse event dispatched) calls `release_contact` and may retry; a failure *after* the mouse event was dispatched calls `confirm_contact` and pauses, and must **never** release — that prevents double-messaging a recruiter. Don't "simplify" this.
- **Never `browser.close()` a connected CDP browser** — detach with `playwright.stop()` only. Closing force-quits the human's logged-in VNC session.
- **Success requires a newly-appearing post-click marker** (发送简历/继续沟通/沟通中) that was absent before the click; pre-existing nav/sidebar markers don't count.
- **Budget reservation** in `state.py` (`reserve_contact` → `confirm_contact`/`release_contact`; one `BEGIN IMMEDIATE` transaction plus a partial-unique index; 600s TTL) is the dedupe/daily-cap mechanism. Don't bypass it.

## LLM integration

The LLM is the `claude` **CLI invoked as a subprocess** (`llm.py` → `claude -p --bare --model <m> --tools "" --disallowedTools "mcp__*" --no-session-persistence --output-format json`), **not** the Anthropic API/SDK. The default model alias `kimi-k2.7-code` (override via `RESUME_PILOT_CLAUDE_MODEL`) is **not** an Anthropic model — don't assume Anthropic-API behavior. Employer/job text is untrusted: it is wrapped in `<untrusted_job_posting>` and the prompt treats it as data, never instructions. Matching policy is passed to the LLM as context, never compiled into a source-level allow/reject list.

## Secrets & repository hygiene (public repo — enforce strictly)

Never commit candidate data, resume content, salary targets, role allow/reject lists, hostnames, cookies, logs, or any secret. Private runtime files (`state.sqlite`, `autonomous-policy.json`, `profile*.json`, `chrome-profile/`, `screenshots/`, `audit/`, …) are gitignored — keep them out. Before any commit/PR, run the hygiene gate:

```
rg -n "your-host|autonomous-policy|profile-analysis|state.sqlite|chrome-profile|root@" .
uv run pytest
uv run ruff check .
```

## Layout & environment notes

- `src/resume_pilot/`: `cli.py` (subcommands + live orchestration/guards), `runner.py` (evaluate→decide→gate→contact loop + prompt builder), `boss.py` (BOSS HTML parsing, risk markers, button uniqueness, obfuscated-digit decode), `state.py` (SQLite persistence), `browser.py` (loopback CDP Chromium management), `cdp.py` (raw CDP WebSocket client), `llm.py`, `config.py`, `models.py`.
- `ops/remote/autonomous_apply.py` is a **standalone** runner (not a console script; lives outside the package on purpose), driven by a private policy JSON. `ops/remote/*.sh` provision Debian + VNC + noVNC; `ops/fixtures/*.html` feed dry-run smoke tests.
- No `.env`/dotenv — config is read from `os.environ` directly; all `RESUME_PILOT_*` vars are optional overrides (paths default under `~/.local/...`; CDP binds `127.0.0.1:9222`). The daily-cap day boundary is `Asia/Shanghai`.
