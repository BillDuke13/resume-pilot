from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import websockets

from resume_pilot.config import default_cdp_url


class CdpError(RuntimeError):
    """Raised when direct CDP control cannot complete safely."""


@dataclass(frozen=True)
class CdpTarget:
    url: str
    title: str
    web_socket_debugger_url: str


def list_cdp_targets(cdp_url: str | None = None) -> list[dict[str, Any]]:
    endpoint = (cdp_url or default_cdp_url()).rstrip("/") + "/json/list"
    try:
        with urllib.request.urlopen(endpoint, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except CdpError:
        raise
    except Exception as exc:
        raise CdpError(f"Could not list CDP targets: {type(exc).__name__}: {exc}") from exc


def find_page_target(url_contains: str, *, cdp_url: str | None = None) -> CdpTarget | None:
    for target in list_cdp_targets(cdp_url):
        if target.get("type") != "page":
            continue
        if url_contains not in str(target.get("url", "")):
            continue
        return CdpTarget(
            url=str(target.get("url", "")),
            title=str(target.get("title", "")),
            web_socket_debugger_url=str(target.get("webSocketDebuggerUrl", "")),
        )
    return None


def find_exact_page_target(url: str, *, cdp_url: str | None = None) -> CdpTarget | None:
    for target in list_cdp_targets(cdp_url):
        if target.get("type") != "page":
            continue
        if str(target.get("url", "")) != url:
            continue
        return CdpTarget(
            url=str(target.get("url", "")),
            title=str(target.get("title", "")),
            web_socket_debugger_url=str(target.get("webSocketDebuggerUrl", "")),
        )
    return None


def target_url_matches(expected_url: str, actual_url: str, url_contains: str) -> bool:
    if not actual_url.startswith(("http://", "https://")):
        return False
    if url_contains and url_contains not in actual_url:
        return False

    expected = urllib.parse.urlparse(expected_url)
    actual = urllib.parse.urlparse(actual_url)
    if expected.netloc and actual.netloc != expected.netloc:
        return False
    if expected.path and expected.path != "/" and actual.path != expected.path:
        return False

    expected_query = urllib.parse.parse_qs(expected.query)
    actual_query = urllib.parse.parse_qs(actual.query)
    return all(actual_query.get(key) == value for key, value in expected_query.items())


def find_matching_page_target(
    url: str,
    *,
    url_contains: str,
    cdp_url: str | None = None,
) -> CdpTarget | None:
    for target in list_cdp_targets(cdp_url):
        if target.get("type") != "page":
            continue
        actual_url = str(target.get("url", ""))
        if not target_url_matches(url, actual_url, url_contains):
            continue
        return CdpTarget(
            url=actual_url,
            title=str(target.get("title", "")),
            web_socket_debugger_url=str(target.get("webSocketDebuggerUrl", "")),
        )
    return None


def open_url_in_cdp_target(url: str, *, cdp_url: str | None = None) -> CdpTarget:
    endpoint = (cdp_url or default_cdp_url()).rstrip("/") + "/json/new?" + urllib.parse.quote(
        url,
        safe="",
    )
    request = urllib.request.Request(endpoint, method="PUT")
    with urllib.request.urlopen(request, timeout=10) as response:
        target = json.loads(response.read().decode("utf-8"))
    return CdpTarget(
        url=str(target.get("url", "")),
        title=str(target.get("title", "")),
        web_socket_debugger_url=str(target.get("webSocketDebuggerUrl", "")),
    )


async def cdp_evaluate(web_socket_url: str, expression: str) -> Any:
    try:
        async with websockets.connect(web_socket_url, max_size=30_000_000) as socket:
            next_id = 1

            async def send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal next_id
                message: dict[str, Any] = {"id": next_id, "method": method}
                if params is not None:
                    message["params"] = params
                await socket.send(json.dumps(message))
                current_id = next_id
                next_id += 1
                while True:
                    response = json.loads(await socket.recv())
                    if response.get("id") == current_id:
                        return response

            await send("Runtime.enable")
            result = await send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
            )
            if "exceptionDetails" in result:
                raise CdpError(json.dumps(result["exceptionDetails"], ensure_ascii=False)[:1000])
            return result.get("result", {}).get("result", {}).get("value")
    except CdpError:
        raise
    except Exception as exc:
        raise CdpError(f"CDP evaluate failed: {type(exc).__name__}: {exc}") from exc


async def cdp_navigate(web_socket_url: str, url: str) -> None:
    try:
        async with websockets.connect(web_socket_url, max_size=30_000_000) as socket:
            next_id = 1

            async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
                nonlocal next_id
                message = {"id": next_id, "method": method, "params": params}
                await socket.send(json.dumps(message))
                current_id = next_id
                next_id += 1
                while True:
                    response = json.loads(await socket.recv())
                    if response.get("id") == current_id:
                        return response

            await send("Page.enable", {})
            response = await send("Page.navigate", {"url": url})
            if "error" in response:
                raise CdpError(json.dumps(response["error"], ensure_ascii=False))
    except CdpError:
        raise
    except Exception as exc:
        raise CdpError(f"CDP navigate failed: {type(exc).__name__}: {exc}") from exc


async def cdp_current_url(web_socket_url: str) -> str:
    value = await cdp_evaluate(web_socket_url, "window.location.href")
    return str(value or "")


async def cdp_bring_to_front(web_socket_url: str) -> None:
    try:
        async with websockets.connect(web_socket_url, max_size=30_000_000) as socket:
            await socket.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
            while True:
                response = json.loads(await socket.recv())
                if response.get("id") == 1:
                    if "error" in response:
                        raise CdpError(json.dumps(response["error"], ensure_ascii=False))
                    return
    except CdpError:
        raise
    except Exception as exc:
        raise CdpError(f"CDP bring to front failed: {type(exc).__name__}: {exc}") from exc


async def cdp_dispatch_mouse_click(
    web_socket_url: str,
    x: float,
    y: float,
    *,
    bring_to_front: bool = True,
) -> None:
    try:
        async with websockets.connect(web_socket_url, max_size=30_000_000) as socket:
            next_id = 1

            async def send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal next_id
                message: dict[str, Any] = {"id": next_id, "method": method}
                if params is not None:
                    message["params"] = params
                await socket.send(json.dumps(message))
                current_id = next_id
                next_id += 1
                while True:
                    response = json.loads(await socket.recv())
                    if response.get("id") == current_id:
                        return response

            if bring_to_front:
                response = await send("Page.bringToFront")
                if "error" in response:
                    raise CdpError(json.dumps(response["error"], ensure_ascii=False))

            for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
                response = await send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": x,
                        "y": y,
                        "button": "left",
                        "buttons": 1 if event_type == "mousePressed" else 0,
                        "clickCount": 1,
                    },
                )
                if "error" in response:
                    raise CdpError(json.dumps(response["error"], ensure_ascii=False))
    except CdpError:
        raise
    except Exception as exc:
        raise CdpError(f"CDP mouse click failed: {type(exc).__name__}: {exc}") from exc


def open_url_in_visible_chrome(url: str, *, display: str = ":1") -> None:
    quoted_url = urllib.parse.quote(url, safe=":/?=&%#._+-")
    script = f"""
    export DISPLAY={display}
    wid=$(xdotool search --onlyvisible --name Chromium | tail -n 1)
    test -n "$wid"
    xdotool windowactivate --sync "$wid"
    xdotool key --window "$wid" Ctrl+l
    xdotool type --window "$wid" --delay 2 "{quoted_url}"
    xdotool key --window "$wid" Return
    """
    subprocess.run(["bash", "-lc", script], check=True, timeout=20)


async def ensure_page_target(
    url: str,
    *,
    url_contains: str = "zhipin.com",
    settle_seconds: float = 8.0,
    retries: int = 1,
    display: str = ":1",
    allow_visible_fallback: bool = False,
) -> CdpTarget:
    last_error: CdpError | None = None
    try:
        target = find_matching_page_target(url, url_contains=url_contains)
    except Exception as exc:
        target = None
        last_error = CdpError(f"Could not list matching CDP targets: {type(exc).__name__}: {exc}")
    if target:
        try:
            actual_url = await cdp_current_url(target.web_socket_debugger_url)
        except CdpError as exc:
            last_error = exc
        else:
            if target_url_matches(url, actual_url, url_contains):
                return CdpTarget(
                    url=actual_url,
                    title=target.title,
                    web_socket_debugger_url=target.web_socket_debugger_url,
                )

    try:
        reusable_target = find_page_target(url_contains)
    except Exception as exc:
        reusable_target = None
        last_error = CdpError(f"Could not list reusable CDP targets: {exc}")
    if reusable_target:
        try:
            await cdp_navigate(reusable_target.web_socket_debugger_url, url)
            await asyncio.sleep(settle_seconds)
            actual_url = await cdp_current_url(reusable_target.web_socket_debugger_url)
        except CdpError as exc:
            last_error = exc
        else:
            if target_url_matches(url, actual_url, url_contains):
                return CdpTarget(
                    url=actual_url,
                    title=reusable_target.title,
                    web_socket_debugger_url=reusable_target.web_socket_debugger_url,
                )

    created_target: CdpTarget | None = None
    for _attempt in range(retries + 1):
        if created_target is None:
            try:
                created_target = open_url_in_cdp_target(url)
            except Exception as exc:
                if not allow_visible_fallback:
                    raise CdpError(f"CDP could not open {url!r}: {exc}") from exc
                open_url_in_visible_chrome(url, display=display)
        else:
            try:
                await cdp_navigate(created_target.web_socket_debugger_url, url)
            except CdpError as exc:
                last_error = exc
                created_target = None
                continue

        await asyncio.sleep(settle_seconds)

        if created_target is not None:
            try:
                actual_url = await cdp_current_url(created_target.web_socket_debugger_url)
            except CdpError as exc:
                last_error = exc
                created_target = None
            else:
                if target_url_matches(url, actual_url, url_contains):
                    return CdpTarget(
                        url=actual_url,
                        title=created_target.title,
                        web_socket_debugger_url=created_target.web_socket_debugger_url,
                    )

        try:
            target = find_matching_page_target(url, url_contains=url_contains)
        except Exception as exc:
            last_error = CdpError(
                f"Could not list matching CDP targets: {type(exc).__name__}: {exc}"
            )
            continue
        if target:
            try:
                actual_url = await cdp_current_url(target.web_socket_debugger_url)
            except CdpError as exc:
                last_error = exc
                continue
            if target_url_matches(url, actual_url, url_contains):
                return CdpTarget(
                    url=actual_url,
                    title=target.title,
                    web_socket_debugger_url=target.web_socket_debugger_url,
                )
    message = f"No matching live CDP page target for {url!r} containing {url_contains!r}"
    if last_error is not None:
        message += f"; last error: {last_error}"
    raise CdpError(message)


async def page_text(target: CdpTarget) -> str:
    return str(
        await cdp_evaluate(
            target.web_socket_debugger_url,
            "document.body ? document.body.innerText : ''",
        )
        or ""
    )


async def page_html(target: CdpTarget) -> str:
    return str(
        await cdp_evaluate(target.web_socket_debugger_url, "document.documentElement.outerHTML")
        or ""
    )
