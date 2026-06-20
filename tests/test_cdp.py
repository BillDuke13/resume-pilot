from __future__ import annotations

import asyncio
import json
import subprocess
from io import BytesIO

from resume_pilot import cdp


def test_find_page_target_matches_url(monkeypatch):
    monkeypatch.setattr(
        cdp,
        "list_cdp_targets",
        lambda _cdp_url=None: [
            {
                "type": "page",
                "url": "chrome://newtab/",
                "title": "New Tab",
                "webSocketDebuggerUrl": "ws://newtab",
            },
            {
                "type": "page",
                "url": "https://www.zhipin.com/web/geek/jobs",
                "title": "BOSS",
                "webSocketDebuggerUrl": "ws://boss",
            },
        ],
    )

    target = cdp.find_page_target("zhipin.com")

    assert target is not None
    assert target.url == "https://www.zhipin.com/web/geek/jobs"
    assert target.web_socket_debugger_url == "ws://boss"


def test_find_exact_page_target_requires_exact_url(monkeypatch):
    monkeypatch.setattr(
        cdp,
        "list_cdp_targets",
        lambda _cdp_url=None: [
            {
                "type": "page",
                "url": "https://www.zhipin.com/job_detail/one.html",
                "title": "One",
                "webSocketDebuggerUrl": "ws://one",
            },
            {
                "type": "page",
                "url": "https://www.zhipin.com/job_detail/two.html",
                "title": "Two",
                "webSocketDebuggerUrl": "ws://two",
            },
        ],
    )

    target = cdp.find_exact_page_target("https://www.zhipin.com/job_detail/two.html")

    assert target is not None
    assert target.web_socket_debugger_url == "ws://two"


def test_target_url_matches_expected_host_path_and_query():
    expected = "https://www.zhipin.com/web/geek/jobs?query=k8s&city=123456789"
    actual = "https://www.zhipin.com/web/geek/jobs?city=123456789&query=k8s&page=2"

    assert cdp.target_url_matches(expected, actual, "zhipin.com")


def test_target_url_rejects_file_urls():
    assert not cdp.target_url_matches(
        "https://www.zhipin.com/job_detail/one.html",
        "file:///www.zhipin.com/job_detail/one.html",
        "zhipin.com",
    )


def test_find_matching_page_target_accepts_equivalent_url(monkeypatch):
    monkeypatch.setattr(
        cdp,
        "list_cdp_targets",
        lambda _cdp_url=None: [
            {
                "type": "page",
                "url": "https://www.zhipin.com/web/geek/jobs?city=123456789&query=k8s&page=2",
                "title": "BOSS",
                "webSocketDebuggerUrl": "ws://boss",
            },
        ],
    )

    target = cdp.find_matching_page_target(
        "https://www.zhipin.com/web/geek/jobs?query=k8s&city=123456789",
        url_contains="zhipin.com",
    )

    assert target is not None
    assert target.web_socket_debugger_url == "ws://boss"


def test_open_url_in_cdp_target_uses_json_new(monkeypatch):
    opened = []

    class FakeResponse:
        def __enter__(self):
            return BytesIO(
                b'{"url":"https://example.com/path","title":"Example",'
                b'"webSocketDebuggerUrl":"ws://target"}'
            )

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        opened.append((request.full_url, request.get_method(), timeout))
        return FakeResponse()

    monkeypatch.setattr(cdp.urllib.request, "urlopen", fake_urlopen)

    target = cdp.open_url_in_cdp_target("https://example.com/path?a=1")

    assert opened == [
        ("http://127.0.0.1:9222/json/new?https%3A%2F%2Fexample.com%2Fpath%3Fa%3D1", "PUT", 10)
    ]
    assert target.web_socket_debugger_url == "ws://target"


def test_list_cdp_targets_wraps_urlopen_timeout(monkeypatch):
    def fail_urlopen(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(cdp.urllib.request, "urlopen", fail_urlopen)

    try:
        cdp.list_cdp_targets()
    except cdp.CdpError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected CdpError")

    assert "Could not list CDP targets" in message
    assert "TimeoutError" in message
    assert "timed out" in message


def test_ensure_page_target_does_not_fallback_to_visible_chrome_by_default(monkeypatch):
    visible_calls = []

    monkeypatch.setattr(cdp, "find_matching_page_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cdp, "find_page_target", lambda *_args, **_kwargs: None)

    def fail_open(*_args, **_kwargs):
        raise OSError("cdp unavailable")

    monkeypatch.setattr(cdp, "open_url_in_cdp_target", fail_open)
    monkeypatch.setattr(
        cdp,
        "open_url_in_visible_chrome",
        lambda *_args, **_kwargs: visible_calls.append(True),
    )

    try:
        asyncio.run(cdp.ensure_page_target("https://www.zhipin.com/web/geek/jobs?query=k8s"))
    except cdp.CdpError:
        pass
    else:
        raise AssertionError("expected CdpError")

    assert visible_calls == []


def test_ensure_page_target_recovers_when_initial_target_listing_times_out(monkeypatch):
    calls = 0
    created_target = cdp.CdpTarget(
        url="about:blank",
        title="Created",
        web_socket_debugger_url="ws://created",
    )

    def flaky_find_matching(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("timed out")
        return None

    monkeypatch.setattr(cdp, "find_matching_page_target", flaky_find_matching)
    monkeypatch.setattr(cdp, "find_page_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cdp, "open_url_in_cdp_target", lambda *_args, **_kwargs: created_target)

    async def fake_current_url(_web_socket_url):
        return "https://www.zhipin.com/web/geek/jobs?query=k8s&page=2"

    monkeypatch.setattr(cdp, "cdp_current_url", fake_current_url)

    target = asyncio.run(
        cdp.ensure_page_target(
            "https://www.zhipin.com/web/geek/jobs?query=k8s",
            settle_seconds=0,
            retries=0,
        )
    )

    assert target.web_socket_debugger_url == "ws://created"
    assert calls == 1


def test_ensure_page_target_skips_matching_target_with_broken_websocket(monkeypatch):
    stale_target = cdp.CdpTarget(
        url="https://www.zhipin.com/web/geek/jobs?query=k8s",
        title="Stale",
        web_socket_debugger_url="ws://stale",
    )
    live_target = cdp.CdpTarget(
        url="about:blank",
        title="Live",
        web_socket_debugger_url="ws://live",
    )

    monkeypatch.setattr(cdp, "find_matching_page_target", lambda *_args, **_kwargs: stale_target)
    monkeypatch.setattr(cdp, "find_page_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cdp, "open_url_in_cdp_target", lambda *_args, **_kwargs: live_target)

    async def fake_current_url(web_socket_url):
        if web_socket_url == "ws://stale":
            raise cdp.CdpError("stale target")
        return "https://www.zhipin.com/web/geek/jobs?query=k8s&page=2"

    monkeypatch.setattr(cdp, "cdp_current_url", fake_current_url)

    target = asyncio.run(
        cdp.ensure_page_target(
            "https://www.zhipin.com/web/geek/jobs?query=k8s",
            settle_seconds=0,
            retries=0,
        )
    )

    assert target.web_socket_debugger_url == "ws://live"
    assert target.url == "https://www.zhipin.com/web/geek/jobs?query=k8s&page=2"


def test_ensure_page_target_reuses_existing_live_target(monkeypatch):
    reusable = cdp.CdpTarget(
        url="https://www.zhipin.com/web/geek/jobs?query=SRE",
        title="BOSS",
        web_socket_debugger_url="ws://boss",
    )
    navigated = []

    monkeypatch.setattr(cdp, "find_matching_page_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cdp, "find_page_target", lambda *_args, **_kwargs: reusable)
    monkeypatch.setattr(
        cdp,
        "open_url_in_cdp_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should reuse target")),
    )

    async def fake_navigate(web_socket_url, url):
        navigated.append((web_socket_url, url))

    async def fake_current_url(_web_socket_url):
        return "https://www.zhipin.com/web/geek/jobs?query=Kubernetes"

    monkeypatch.setattr(cdp, "cdp_navigate", fake_navigate)
    monkeypatch.setattr(cdp, "cdp_current_url", fake_current_url)

    target = asyncio.run(
        cdp.ensure_page_target(
            "https://www.zhipin.com/web/geek/jobs?query=Kubernetes",
            settle_seconds=0,
        )
    )

    assert navigated == [
        ("ws://boss", "https://www.zhipin.com/web/geek/jobs?query=Kubernetes")
    ]
    assert target.web_socket_debugger_url == "ws://boss"


def test_open_url_uses_visible_chrome_address_bar(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cdp.subprocess, "run", fake_run)

    cdp.open_url_in_visible_chrome("https://www.zhipin.com/web/geek/jobs?query=platform")

    assert calls
    command, kwargs = calls[0]
    assert command[:2] == ["bash", "-lc"]
    assert "xdotool key --window" in command[2]
    expected_url = (
        "https://www.zhipin.com/web/geek/jobs?query=platform"
    )
    assert expected_url in command[2]
    assert kwargs["timeout"] == 20


class _FakeCdpSocket:
    def __init__(self, responses):
        self._responses = responses

    async def send(self, _message):
        return None

    async def recv(self):
        return json.dumps(self._responses.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def test_cdp_evaluate_raises_on_nested_runtime_exception(monkeypatch):
    responses = [
        {"id": 1, "result": {}},
        {
            "id": 2,
            "result": {
                "exceptionDetails": {"text": "Uncaught", "exception": {"description": "boom"}}
            },
        },
    ]
    monkeypatch.setattr(cdp.websockets, "connect", lambda *_a, **_k: _FakeCdpSocket(responses))

    try:
        asyncio.run(cdp.cdp_evaluate("ws://target", "throw new Error('boom')"))
    except cdp.CdpError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("expected CdpError on a Runtime.evaluate exception")


def test_cdp_evaluate_returns_value_without_exception(monkeypatch):
    responses = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"result": {"type": "string", "value": "hello"}}},
    ]
    monkeypatch.setattr(cdp.websockets, "connect", lambda *_a, **_k: _FakeCdpSocket(responses))

    result = asyncio.run(cdp.cdp_evaluate("ws://target", "'hello'"))

    assert result == "hello"


def test_cdp_evaluate_raises_on_top_level_cdp_error(monkeypatch):
    responses = [{"id": 1, "error": {"code": -32000, "message": "Target closed"}}]
    monkeypatch.setattr(cdp.websockets, "connect", lambda *_a, **_k: _FakeCdpSocket(responses))

    try:
        asyncio.run(cdp.cdp_evaluate("ws://target", "document.title"))
    except cdp.CdpError as exc:
        assert "Target closed" in str(exc)
    else:
        raise AssertionError("expected CdpError on a top-level CDP error")
