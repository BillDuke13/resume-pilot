# resume-pilot

`resume-pilot` is a guarded browser automation pilot for BOSS Zhipin. It is designed for
a human-owned account, a visible VNC browser session, and an LLM-first approval loop.

The project intentionally avoids credential capture, captcha automation, private API
reverse engineering, proxy rotation, fingerprint spoofing, multi-account behavior, or
attempts to bypass platform limits.

## Open Source Boundary

This repository contains reusable automation infrastructure only. It must not contain:

- Personal resume content, candidate preferences, salary targets, or job-search strategy.
- Browser profiles, cookies, screenshots, DOM dumps, logs, SQLite state, or audit trails.
- Real hostnames, IP addresses, usernames, SSH tunnels, VNC passwords, API keys, or model
  credentials.
- Hard-coded role decisions that only make sense for one candidate.
- Agent memory, handoff notes, or local runbooks with private operational history.

Candidate-specific data belongs in a private policy file outside the repository. The code
passes that policy and the extracted resume profile to the LLM; it should not reimplement
candidate matching with open-source hard-coded keywords.

## Safety Model

- The user logs in manually through VNC. Account passwords, SMS codes, and scan-login
  steps are never automated or stored.
- Chromium runs with a dedicated profile outside the repository.
- CDP listens on `127.0.0.1` only.
- SQLite state is stored outside the repository, by default at
  `~/.local/share/resume-pilot/state.sqlite`.
- Real "immediate contact" actions are single-threaded, deduplicated, audited, and capped.
- Any login gate, captcha, ambiguous resume selector, missing button, or non-unique button
  should pause the workflow for manual VNC takeover.

## Local Setup

```bash
uv sync
uv run resume-pilot doctor
```

Install Playwright browser dependencies only on the machine that will run the visible
browser:

```bash
uv run playwright install chromium
```

## Remote Browser Shape

Provision the remote machine with a non-root runtime user, XFCE, TigerVNC, Chromium or
Chrome, fonts required by the target website, `uv`, and Claude Code. Bind VNC and CDP to
loopback only.

Local tunnel:

```bash
ssh -L 5901:127.0.0.1:5901 -L 6080:127.0.0.1:6080 user@your-host.example
```

Expected browser state:

```text
DISPLAY=:1
CDP: http://127.0.0.1:9222
Profile: ~/.local/state/resume-pilot/chrome-profile
```

Start or inspect the browser:

```bash
uv run resume-pilot browser start
uv run resume-pilot browser status
```

The repeatable remote scripts live in `ops/remote/`:

```bash
ops/remote/bootstrap_debian.sh
ops/remote/start-vnc.sh
ops/remote/start-browser.sh
ops/remote/start-novnc.sh
ops/remote/smoke.sh
```

## Private Policy

The autonomous remote runner reads a private JSON policy from
`RESUME_PILOT_AUTONOMOUS_POLICY` or from
`~/.local/share/resume-pilot/autonomous-policy.json`.

Start from the template in `examples/autonomous-policy.example.json` and keep the real
file outside the repository:

```bash
install -m 600 examples/autonomous-policy.example.json \
  ~/.local/share/resume-pilot/autonomous-policy.json
```

The policy can define search keywords, target-site query parameters, an optional salary
floor, and candidate-side instructions that the LLM must treat as authoritative. Role
terms in the policy are passed to the LLM as private context; the open-source runner does
not use them as a source-code allow-list or reject-list. The project does not encode one
candidate's role fit in source code; it relies on the LLM decision schema and private
policy/profile material.

## CLI

```bash
uv run resume-pilot doctor --probe-llm
uv run resume-pilot profile extract --dry-run
uv run resume-pilot run --dry-run --source-url "https://www.zhipin.com/web/geek/job" --html-file sample.html --limit 5
uv run resume-pilot run --execute --confirm-live-contact --daily-cap 1 --source-url "https://www.zhipin.com/web/geek/jobs" --limit 1
uv run resume-pilot inbox watch --dry-run
```

`profile extract`, `run`, and `inbox watch` default to dry-run mode. Saved HTML execution
is never allowed to click. Live CDP `run --execute` is intentionally narrow: the page must
yield exactly one job, `--confirm-live-contact` must be present, the source URL must be an
expected BOSS/Zhipin job URL, the LLM decision must be `apply` with sufficient confidence
and no risk flags, the daily cap and duplicate guards must pass, and there must be
exactly one visible platform contact control at click time. Any ambiguity pauses the
workflow for manual VNC takeover.

By default the Claude Code provider uses `kimi-k2.7-code`. Override it with
`RESUME_PILOT_CLAUDE_MODEL` if your Claude Code setup uses a different model alias.

## LLM Contract

Job approval must return JSON:

```json
{
  "decision": "apply",
  "confidence": 0.9,
  "reason": "Relevant automation role",
  "resume_match_signals": ["Python", "Playwright"],
  "risk_flags": []
}
```

Reply handling must return JSON:

```json
{
  "send_resume": true,
  "reply_type": "recruiter_interested",
  "reason": "Recruiter asked for a resume",
  "needs_human": false
}
```

## Repository Hygiene

Before publishing or opening a PR, run:

```bash
rg -n "your-host|autonomous-policy|profile-analysis|state.sqlite|chrome-profile|root@" .
uv run pytest
uv run ruff check .
```

The first command should only show placeholders, documentation, tests, or `.gitignore`
entries. Real local state must stay outside the repository.

## Tests

```bash
uv run pytest
uv run ruff check .
```
