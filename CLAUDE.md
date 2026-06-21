# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A guarded, LLM-first pilot that drives a **human-owned, visible Chromium** (over CDP on loopback, viewed via VNC) to evaluate BOSS Zhipin (zhipin.com) job postings and, only after many safety gates, click the single "立即沟通" contact button on one job. It also inspects the recruiter inbox (dry-run only) to surface replies that are candidates for a resume send; the send-resume decision and action are not yet implemented. Candidate matching is delegated to an LLM, not hard-coded. **The guardrails are the point of this project — preserve them.**

## Toolchain & commands

Package manager is **uv** (not pip/poetry); everything runs through `uv run`. `requires-python >=3.13`; build backend is `uv_build`.

- Setup: `uv sync` (or `uv sync --all-groups`); `uv run playwright install chromium` (only on the host that runs the browser)
- Run the CLI: `uv run resume-pilot <cmd>` (equivalently `python -m resume_pilot`). Subcommands: `doctor`, `browser {start,status,stop}`, `profile extract`, `run`, `inbox watch`, `pauses {list,resolve}`.
- Test: `uv run pytest` (config already sets `-q`, `testpaths=tests`).
- Lint: `uv run ruff check .` (autofix: `--fix`)
- Deterministic smoke (no LLM call): `uv run resume-pilot run --dry-run --decision-fixture {apply,skip,needs_review} ...` — this is a `run` flag, not a pytest option, and is rejected with `--execute`.

## Code style

- ruff, **line length 100** (not the 88 default); rule sets `E,F,I,UP,B,SIM`; `target-version = py313`.
- Every module starts with `from __future__ import annotations`.

## Safety guardrails — do not weaken these

This automates a live third-party platform. The design is deliberately defensive; treat the following as invariants, not incidental code:

- **Dry-run is the default, and flags differ per subcommand.** Dry-run means *no real action is taken*, not *no browser interaction* — it still navigates/reads the live page when a URL is given; only `--html-file` avoids the live browser entirely. `run` needs `--execute` *and* `--confirm-live-contact` to click (`--html-file` can never click). `inbox watch` is dry-run only and rejects `--execute` (live send-resume is not implemented). `profile extract` persists the profile cache with `--write-cache` (there is no `--execute`). **Never trigger a live `run --execute` / live contact unless the human explicitly asks for it in that session.**
- **The live click path is intentionally narrow** (`cli.py` + `runner.py`): exactly one job on the page, an allow-listed `https` BOSS host with path `/web/geek/` or `/job_detail/`, no page risks, an LLM `apply` decision with confidence ≥ `MIN_APPLY_CONFIDENCE` (0.75) and zero risk flags, daily cap free, and exactly one visible 立即沟通 control inside the selected `.job-detail-box`. Don't loosen any of these.
- **Persistent pauses block future runs.** In-evaluation ambiguities (login/captcha/security wall, multiple resumes, non-unique button, nav drift) call `state.pause()` (`runner.py`; the inbox page-risk path in `cli.py`; `autonomous_apply.py`) to write a `pauses` row, and an unresolved row blocks all future live runs. Clear it only via `resume-pilot pauses resolve` after a human handles it in VNC — never auto-clear to "unblock". Note: some pre-flight guards in `cmd_run` (`unsupported_source_url`, `cdp_host_not_loopback`, `managed_browser_not_running`) raise `HumanPauseRequired` and abort that run with exit 3 *without* persisting a row, so they do not block later runs.
- **Click-failure semantics are deliberate** (`cdp.py`, `runner.py`, `autonomous_apply.py`): a *pre-click* abort (no mouse event dispatched) calls `release_contact` and may retry; a failure *after* the mouse event was dispatched calls `confirm_contact` and pauses, and must **never** release — that prevents double-messaging a recruiter. Don't "simplify" this.
- **Never `browser.close()` a connected CDP browser** — detach with `playwright.stop()` only. Closing force-quits the human's logged-in VNC session.
- **Success requires a newly-appearing post-click marker** (发送简历/继续沟通/沟通中) that was absent before the click; pre-existing nav/sidebar markers don't count.
- **Budget reservation** in `state.py` (`reserve_contact` → `confirm_contact`/`release_contact`; one `BEGIN IMMEDIATE` transaction plus a partial-unique index; 600s TTL) is the dedupe/daily-cap mechanism. Don't bypass it.

## LLM integration

The LLM is the `claude` **CLI invoked as a subprocess** (`llm.py` → `claude -p --bare --model <m> --tools "" --disallowedTools "mcp__*" --no-session-persistence --output-format json`), **not** the Anthropic API/SDK. The default model alias `kimi-k2.7-code` (override via `RESUME_PILOT_CLAUDE_MODEL`) is **not** an Anthropic model — don't assume Anthropic-API behavior. Employer/job text is untrusted: it is wrapped in `<untrusted_job_posting>` and the prompt treats it as data, never instructions. Matching is mostly delegated to the LLM rather than compiled into source-level allow/reject logic — with one deliberate exception: a **salary floor gate** (`runner._salary_gate_decision`, `autonomous_apply.salary_skip_reason`). When `minimum_monthly_salary_k` is set, jobs whose parsed salary is below it (or unparseable) are deterministically skipped *before* the LLM is called; don't remove or bypass it.

## Secrets & repository hygiene (public repo — enforce strictly)

Never commit candidate data, resume content, salary targets, role allow/reject lists, hostnames, cookies, logs, or any secret. Private runtime files (`state.sqlite`, `autonomous-policy.json`, `profile.json`, `profile-analysis.json`, `chrome-profile/`, `screenshots/`, `audit/`, …) are gitignored — keep them out. Before any commit/PR, run the hygiene gate:

```
rg -n "your-host|autonomous-policy|profile-analysis|state.sqlite|chrome-profile|root@" .
uv run pytest
uv run ruff check .
```

## Layout & environment notes

- `src/resume_pilot/`: `cli.py` (subcommands + live orchestration/guards), `runner.py` (evaluate→decide→gate→contact loop + prompt builder), `boss.py` (BOSS HTML parsing, risk markers, button uniqueness, obfuscated-digit decode), `state.py` (SQLite persistence), `browser.py` (loopback CDP Chromium management), `cdp.py` (raw CDP WebSocket client), `llm.py`, `config.py`, `models.py`.
- `ops/remote/autonomous_apply.py` is a **standalone** runner (not a console script; lives outside the package on purpose), driven by a private policy JSON. `ops/remote/*.sh` provision Debian + VNC + noVNC; `ops/fixtures/*.html` feed dry-run smoke tests.
- No `.env`/dotenv — config is read from `os.environ` directly; all `RESUME_PILOT_*` vars are optional overrides (paths default under `~/.local/...`; CDP binds `127.0.0.1:9222`). The daily-cap day boundary is `Asia/Shanghai`.
