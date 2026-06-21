from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from resume_pilot import __version__
from resume_pilot.boss import BossHtmlAdapter, HumanPauseRequired
from resume_pilot.browser import (
    LOOPBACK_CDP_HOSTS,
    BrowserManager,
    fetch_cdp_version,
    find_browser_binary,
)
from resume_pilot.config import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_DAILY_CAP,
    AppPaths,
    default_cdp_url,
)
from resume_pilot.llm import (
    ClaudeCodeClient,
    FixedJsonDecisionClient,
    InvalidLlmResponseError,
    LlmError,
)
from resume_pilot.models import LlmJobDecisionValue
from resume_pilot.runner import ResumePilotRunner
from resume_pilot.state import StateStore

LIVE_READY_MARKERS = (
    "/job_detail/",
    "job-detail",
    "job-card",
    "chat-message",
    "立即沟通",
    "发送简历",
    "扫码登录",
    "短信登录",
    "验证码",
    "安全验证",
)
LIVE_READY_ATTEMPTS = 20
LIVE_READY_POLL_MS = 1_000
POST_CONTACT_SUCCESS_MARKERS = (
    "发送简历",
    "继续沟通",
    "沟通中",
)
ALLOWED_BOSS_HOSTS = {
    "zhipin.com",
    "www.zhipin.com",
    "bosszhipin.com",
    "www.bosszhipin.com",
}
ALLOWED_BOSS_PATH_PREFIXES = (
    "/web/geek/",
    "/job_detail/",
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resume-pilot")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="Override the SQLite state DB path.",
    )
    subparsers = parser.add_subparsers(required=True)

    doctor = subparsers.add_parser("doctor", help="Check local runtime prerequisites.")
    doctor.add_argument("--probe-llm", action="store_true", help="Run a Claude Code JSON probe.")
    doctor.set_defaults(func=cmd_doctor)

    browser = subparsers.add_parser("browser", help="Manage the visible CDP browser.")
    browser_subparsers = browser.add_subparsers(required=True)
    for command in ("start", "status", "stop"):
        item = browser_subparsers.add_parser(command, help=f"{command.capitalize()} browser.")
        item.set_defaults(func=cmd_browser, browser_command=command)

    profile = subparsers.add_parser("profile", help="Extract or inspect the resume profile.")
    profile_subparsers = profile.add_subparsers(required=True)
    extract = profile_subparsers.add_parser("extract", help="Extract current visible page text.")
    extract_mode = extract.add_mutually_exclusive_group()
    extract_mode.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        default=True,
        help="Do not write the profile cache. This is the default.",
    )
    extract_mode.add_argument(
        "--write-cache",
        action="store_false",
        dest="dry_run",
        help="Write the extracted profile text to the private cache.",
    )
    extract.add_argument("--url", default=None, help="Optional resume page URL to open first.")
    extract.set_defaults(func=cmd_profile_extract)

    run = subparsers.add_parser("run", help="Evaluate jobs and optionally apply approved actions.")
    run_mode = run.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        default=True,
        help="Evaluate only; never click or consume budget.",
    )
    run_mode.add_argument(
        "--execute",
        action="store_false",
        dest="dry_run",
        help="Request external actions after all safety gates pass.",
    )
    run.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--source-url", required=True, help="BOSS search or recommendation URL.")
    run.add_argument("--html-file", type=Path, default=None, help="Evaluate a saved HTML fixture.")
    run.add_argument(
        "--decision-fixture",
        choices=[item.value for item in LlmJobDecisionValue],
        default=None,
        help="Use a deterministic local decision for smoke tests instead of calling the LLM.",
    )
    run.add_argument(
        "--profile-summary-file",
        type=Path,
        default=None,
        help="Path to a private resume profile summary file; defaults to the profile cache. "
        "Avoid passing the summary text directly so it never reaches process arguments.",
    )
    run.add_argument(
        "--confirm-live-contact",
        action="store_true",
        help="Required with live --execute to acknowledge one real immediate-contact click.",
    )
    run.set_defaults(func=cmd_run)

    inbox = subparsers.add_parser("inbox", help="Watch replies and decide whether to send resumes.")
    inbox_subparsers = inbox.add_subparsers(required=True)
    watch = inbox_subparsers.add_parser(
        "watch",
        help="Inspect the inbox page for reply candidates.",
    )
    watch_mode = watch.add_mutually_exclusive_group()
    watch_mode.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        default=True,
        help="Do not click send resume. This is the default.",
    )
    watch_mode.add_argument(
        "--execute",
        action="store_false",
        dest="dry_run",
        help="Request send-resume actions after all safety gates pass.",
    )
    watch.add_argument(
        "--html-file",
        type=Path,
        default=None,
        help="Inspect a saved inbox HTML file.",
    )
    watch.add_argument("--url", default=None, help="Optional inbox URL to open before inspection.")
    watch.set_defaults(func=cmd_inbox_watch)

    pauses = subparsers.add_parser("pauses", help="Inspect or resolve human-takeover pauses.")
    pauses_subparsers = pauses.add_subparsers(required=True)
    pauses_subparsers.add_parser("list", help="List unresolved pauses.").set_defaults(
        func=cmd_pauses, pauses_command="list"
    )
    pauses_subparsers.add_parser(
        "resolve", help="Mark all unresolved pauses resolved so runs can resume."
    ).set_defaults(func=cmd_pauses, pauses_command="resolve")
    return parser


def _paths(args: argparse.Namespace) -> AppPaths:
    paths = AppPaths.defaults()
    if args.state_db:
        paths = AppPaths(
            state_db=args.state_db,
            state_dir=paths.state_dir,
            data_dir=paths.data_dir,
            chrome_profile=paths.chrome_profile,
            profile_cache=paths.profile_cache,
            browser_pid=paths.browser_pid,
            browser_log=paths.browser_log,
        )
    paths.ensure_private()
    return paths


def _load_profile_summary(paths: AppPaths, summary_file: Path | None) -> str | None:
    if summary_file is not None:
        # An explicit override must exist and be readable; do not silently fall back
        # to "no profile" and let the LLM decide without the requested policy.
        raw = summary_file.read_text(encoding="utf-8")
    else:
        cache = paths.profile_cache
        if not cache.exists():
            return None
        try:
            raw = cache.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or None
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"].strip() or None
    return raw.strip() or None


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = _paths(args)
    failed = False
    checks: dict[str, Any] = {
        "version": __version__,
        "python": platform.python_version(),
        "state_db": str(paths.state_db),
        "state_db_parent_private": oct(paths.state_db.parent.stat().st_mode & 0o777),
        "uv": _command_version("uv", ["uv", "--version"]),
        "claude": _command_version("claude", ["claude", "--version"]),
        "vncserver": _command_version("vncserver", ["vncserver", "-version"])
        or _command_version("Xtigervnc", ["Xtigervnc", "-version"]),
        "browser_binary": find_browser_binary(),
        "display": os.environ.get("DISPLAY"),
        "cdp_url": default_cdp_url(),
        "cdp": fetch_cdp_version(default_cdp_url()) or None,
    }
    if args.probe_llm:
        client = ClaudeCodeClient(
            model=os.environ.get("RESUME_PILOT_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
        )
        try:
            checks["llm_probe"] = client.probe_json_capability()
        except (LlmError, InvalidLlmResponseError, subprocess.TimeoutExpired) as exc:
            checks["llm_probe_error"] = str(exc)
            failed = True
    _print_json(checks)
    return 1 if failed else 0


def _command_version(executable: str, command: list[str]) -> str | None:
    if not shutil.which(executable):
        return None
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0] if output else f"exit {result.returncode}"


def cmd_browser(args: argparse.Namespace) -> int:
    manager = BrowserManager(_paths(args))
    command = args.browser_command
    status = getattr(manager, command)()
    _print_json(status.__dict__)
    if command == "stop":
        return 0 if not status.running else 1
    return 0 if status.running else 1


def cmd_pauses(args: argparse.Namespace) -> int:
    store = StateStore(_paths(args).state_db)
    if args.pauses_command == "resolve":
        _print_json({"resolved": store.resolve_pauses()})
        return 0
    _print_json(
        {
            "active_pauses": [
                {"id": row["id"], "reason": row["reason"], "created_at": row["created_at"]}
                for row in store.active_pauses()
            ]
        }
    )
    return 0


def cmd_profile_extract(args: argparse.Namespace) -> int:
    paths = _paths(args)
    text = _read_live_page_text(args.url)

    if args.dry_run:
        _print_json({"dry_run": True, "characters": len(text), "cache": str(paths.profile_cache)})
        return 0

    paths.profile_cache.write_text(
        json.dumps({"text": text}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths.profile_cache.chmod(0o600)
    _print_json({"dry_run": False, "characters": len(text), "cache": str(paths.profile_cache)})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    state = StateStore(paths.state_db)
    try:
        profile_summary = _load_profile_summary(paths, args.profile_summary_file)
    except OSError as exc:
        print(f"Cannot read --profile-summary-file: {exc}", file=sys.stderr)
        return 2
    if args.decision_fixture and not args.dry_run:
        print(
            "Live --execute cannot use --decision-fixture; it requires a real LLM decision.",
            file=sys.stderr,
        )
        return 2
    if args.decision_fixture:
        client = FixedJsonDecisionClient(LlmJobDecisionValue(args.decision_fixture))
    else:
        client = ClaudeCodeClient(
            model=os.environ.get("RESUME_PILOT_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
    )
    runner = ResumePilotRunner(state=state, llm_client=client, adapter=BossHtmlAdapter())

    if args.html_file is None:
        try:
            if args.dry_run:
                html = _read_live_page_html(args.source_url)
                summary = runner.evaluate_static_html(
                    html,
                    source_url=args.source_url,
                    dry_run=True,
                    daily_cap=args.daily_cap,
                    limit=args.limit,
                    profile_summary=profile_summary,
                )
            else:
                if not args.confirm_live_contact:
                    print(
                        "Live --execute requires --confirm-live-contact.",
                        file=sys.stderr,
                    )
                    return 2
                summary = _execute_live_run(
                    runner,
                    paths=paths,
                    source_url=args.source_url,
                    daily_cap=args.daily_cap,
                    limit=args.limit,
                    profile_summary=profile_summary,
                )
        except HumanPauseRequired as exc:
            _print_json({"paused": True, "reason": exc.reason, "details": str(exc.details)})
            return 3
        _print_json(summary.__dict__)
        return 0

    try:
        summary = runner.evaluate_html_file(
            args.html_file,
            source_url=args.source_url,
            dry_run=args.dry_run,
            daily_cap=args.daily_cap,
            limit=args.limit,
            profile_summary=profile_summary,
        )
    except HumanPauseRequired as exc:
        _print_json({"paused": True, "reason": exc.reason, "details": str(exc.details)})
        return 3
    _print_json(summary.__dict__)
    return 0


def cmd_inbox_watch(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print(
            "Inbox live send-resume actions are not enabled until a dry-run inbox check passes.",
            file=sys.stderr,
        )
        return 2
    adapter = BossHtmlAdapter()
    html = (
        args.html_file.read_text(encoding="utf-8")
        if args.html_file
        else _read_live_page_html(args.url)
    )
    replies = adapter.extract_recruiter_replies(html)
    candidates = [reply for reply in replies if adapter.reply_is_candidate_for_resume(reply)]
    _print_json(
        {
            "dry_run": True,
            "status": "inspected",
            "replies": len(replies),
            "candidate_replies": len(candidates),
            "candidates": [
                {
                    "conversation_id": reply.conversation_id,
                    "sender": reply.sender,
                    "text": reply.text,
                    "job_id": reply.job_id,
                }
                for reply in candidates
            ],
        }
    )
    return 0


def _first_page(browser):
    if browser.contexts and browser.contexts[0].pages:
        return browser.contexts[0].pages[0]
    if browser.contexts:
        return browser.contexts[0].new_page()
    return browser.new_page()


def _read_live_page_html(url: str | None) -> str:
    playwright = None
    browser = None
    try:
        from resume_pilot.browser import connect_existing_browser

        playwright, browser = connect_existing_browser()
        page = _first_page(browser)
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        return _wait_for_live_page_html(page)
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()


def _execute_live_run(
    runner: ResumePilotRunner,
    *,
    paths: AppPaths,
    source_url: str,
    daily_cap: int,
    limit: int | None,
    profile_summary: str | None,
):
    _validate_boss_source_url(source_url)
    manager = BrowserManager(paths)
    if manager.cdp_host not in LOOPBACK_CDP_HOSTS:
        raise HumanPauseRequired(
            "cdp_host_not_loopback",
            {"cdp_host": manager.cdp_host, "cdp_url": manager.cdp_url},
        )
    status = manager.status()
    if not status.running:
        raise HumanPauseRequired(
            "managed_browser_not_running",
            {"detail": status.detail, "cdp_url": status.cdp_url},
        )
    playwright = None
    browser = None
    try:
        from resume_pilot.browser import connect_existing_browser

        playwright, browser = connect_existing_browser()
        page = _first_page(browser)
        page.goto(source_url, wait_until="domcontentloaded", timeout=60_000)
        html = _wait_for_live_page_html(page)

        def contact_executor(job):
            return _click_unique_live_contact(page, runner.adapter, job.platform_job_id)

        return runner.evaluate_static_html(
            html,
            source_url=source_url,
            dry_run=False,
            daily_cap=daily_cap,
            limit=limit,
            profile_summary=profile_summary,
            contact_executor=contact_executor,
        )
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()


def _click_unique_live_contact(
    page,
    adapter: BossHtmlAdapter,
    platform_job_id: str,
) -> dict[str, Any]:
    html = _wait_for_live_page_html(page)
    jobs = adapter.extract_job_cards(html, source_url=page.url)
    if len(jobs) != 1 or jobs[0].platform_job_id != platform_job_id:
        raise HumanPauseRequired(
            "contact_job_changed_before_click",
            {
                "expected_job_id": platform_job_id,
                "current_job_ids": [job.platform_job_id for job in jobs],
            },
        )

    can_click, risks = adapter.can_click_contact(html)
    if not can_click:
        raise HumanPauseRequired(
            "contact_button_not_safe_at_click_time",
            {"risks": [risk.__dict__ for risk in risks], "job_id": platform_job_id},
        )

    pattern = re.compile(r"^\s*立即沟通\s*$")
    visible_buttons = []
    # Count contact controls only within the selected job's box (mirrors the
    # adapter) so a recommendation card's 立即沟通 elsewhere on the page cannot make
    # a valid posting look non-unique. Match the same shapes button_labels()
    # accepts (anchors, buttons, role="button"); the single comma selector
    # de-duplicates via querySelectorAll so an element is never counted twice.
    try:
        locator = page.locator(
            ".job-detail-box a, .job-detail-box button, .job-detail-box [role=button]",
            has_text=pattern,
        )
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible(timeout=1_000):
                visible_buttons.append(item)
    except Exception as exc:
        # Locating and visibility checks run before any mouse event, so a detached
        # page or lost session here means no click occurred — a pre-click abort
        # that releases the reservation instead of burning the cap/dedupe slot.
        raise HumanPauseRequired(
            "contact_locator_unavailable",
            {"job_id": platform_job_id, "error": str(exc)},
        ) from exc

    if len(visible_buttons) != 1:
        raise HumanPauseRequired(
            "visible_contact_button_not_unique",
            {"visible_count": len(visible_buttons), "job_id": platform_job_id},
        )

    before_url = page.url
    before_text = _live_visible_text(page)
    # Run actionability checks first without dispatching (trial=True). A failure
    # here means the control never became clickable, so no mouse event was sent —
    # a pre-click abort the runner releases rather than a burned cap/dedupe slot.
    try:
        visible_buttons[0].click(timeout=10_000, trial=True)
    except Exception as exc:
        raise HumanPauseRequired(
            "contact_button_unclickable",
            {"job_id": platform_job_id, "error": str(exc)},
        ) from exc
    # The control is actionable; the real click may dispatch the mouse event and
    # then wait for a navigation it starts. If it raises now, the event may already
    # have been sent, so let it propagate to the runner's confirm path instead of
    # releasing and risking a duplicate message to the same recruiter on retry.
    visible_buttons[0].click(timeout=10_000)
    page.wait_for_timeout(1_500)
    post_click_html = _wait_for_live_page_html(page)
    post_click_risks = adapter.page_risks(post_click_html)
    # Confirm success only from a marker that newly appears in the visible body
    # after the click. Markers already present beforehand (site nav, a sidebar,
    # another conversation) are not evidence that this contact opened a
    # conversation, so they must not skip the manual-verification pause.
    post_click_text = _live_visible_text(page)
    # before_text is empty only when the pre-click read failed; without that
    # baseline a marker cannot be proven newly appeared, so leave the contact
    # unverified and let the manual-verification pause fire.
    post_click_verified = bool(before_text) and not post_click_risks and any(
        marker in post_click_text and marker not in before_text
        for marker in POST_CONTACT_SUCCESS_MARKERS
    )
    return {
        "clicked_label": "立即沟通",
        "job_id": platform_job_id,
        "before_url": before_url,
        "after_url": page.url,
        "post_click_verified": post_click_verified,
        "post_click_risks": [risk.__dict__ for risk in post_click_risks],
        "needs_manual_verification": not post_click_verified,
    }


def _read_live_page_text(url: str | None) -> str:
    playwright = None
    browser = None
    try:
        from resume_pilot.browser import connect_existing_browser

        playwright, browser = connect_existing_browser()
        page = _first_page(browser)
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return page.locator("body").inner_text(timeout=10_000)
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()


def _live_visible_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return ""


def _wait_for_live_page_html(page) -> str:
    last_html = ""
    for _ in range(LIVE_READY_ATTEMPTS):
        try:
            html = page.content()
        except Exception:
            page.wait_for_timeout(LIVE_READY_POLL_MS)
            continue
        if _live_page_has_ready_signal(html):
            return html
        if len(html) > len(last_html):
            last_html = html
        page.wait_for_timeout(LIVE_READY_POLL_MS)
    return last_html or page.content()


def _live_page_has_ready_signal(html: str) -> bool:
    return any(marker in html for marker in LIVE_READY_MARKERS)


def _validate_boss_source_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if parsed.scheme != "https":
        raise HumanPauseRequired("unsupported_source_url", {"source_url": source_url})
    if parsed.netloc not in ALLOWED_BOSS_HOSTS:
        raise HumanPauseRequired("unsupported_source_url", {"source_url": source_url})
    if not any(parsed.path.startswith(prefix) for prefix in ALLOWED_BOSS_PATH_PREFIXES):
        raise HumanPauseRequired("unsupported_source_url", {"source_url": source_url})


if __name__ == "__main__":
    raise SystemExit(main())
