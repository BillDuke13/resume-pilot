from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from resume_pilot.models import JobCard, PageRisk, RecruiterReply

CONTACT_LABELS = ("立即沟通",)
SEND_RESUME_LABELS = ("发送简历",)
LOGIN_MARKERS = ("扫码登录", "短信登录", "登录后查看", "请登录")
POLICY_GATE_MARKERS = ("我已阅读并同意", "阅读并同意", "登录即代表您同意")
SECURITY_MARKERS = ("验证码", "安全验证", "行为异常", "访问过于频繁")
AMBIGUOUS_RESUME_MARKERS = ("选择简历", "请选择简历", "多份简历")
SYSTEM_REPLY_MARKERS = ("系统消息", "已读", "撤回了一条消息", "该职位已下线")
NEGATIVE_REPLY_MARKERS = ("不合适", "暂不考虑", "已招满", "不匹配")
OBFUSCATED_DIGIT_OFFSET = 0xE031


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", decode_obfuscated_digits(value)).strip()


def decode_obfuscated_digits(value: str) -> str:
    translated = []
    for char in value:
        codepoint = ord(char)
        if OBFUSCATED_DIGIT_OFFSET <= codepoint <= OBFUSCATED_DIGIT_OFFSET + 9:
            translated.append(str(codepoint - OBFUSCATED_DIGIT_OFFSET))
        else:
            translated.append(char)
    return "".join(translated)


def _class_contains(tag: Tag, *needles: str) -> bool:
    classes = " ".join(str(item) for item in tag.get("class", []))
    return any(needle in classes for needle in needles)


def _first_text(root: Tag, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        element = root.select_one(selector)
        if element:
            text = normalize_text(element.get_text(" "))
            if text:
                return text
    return None


def _job_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/job_detail/([A-Za-z0-9_-]+)(?:\.html)?", url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    if parsed.path:
        return parsed.path.rstrip("/").split("/")[-1] or None
    return None


def _detail_url_job_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/job_detail/([A-Za-z0-9_-]+)(?:\.html)?", url)
    return match.group(1) if match else None


def _stable_job_id(*parts: str | None) -> str:
    digest = hashlib.sha256("|".join(part or "" for part in parts).encode()).hexdigest()
    return f"derived-{digest[:16]}"


def _split_title_salary(text: str) -> tuple[str, str | None]:
    match = re.search(r"\s+(\d{1,3}(?:-\d{1,3})?K(?:·\d{1,2}薪)?)\s*$", text, re.IGNORECASE)
    if match:
        return text[: match.start()].strip(), match.group(1)
    return text, None


def _salary_from_text(text: str) -> str | None:
    match = re.search(r"\b\d{1,3}(?:-\d{1,3})?K(?:·\d{1,2}薪)?\b", text, re.IGNORECASE)
    return match.group(0) if match else None


def _company_from_boss_attr(text: str | None) -> str | None:
    if not text:
        return None
    return normalize_text(text).split("·", maxsplit=1)[0].strip() or None


def _candidate_card(anchor: Tag) -> Tag:
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if _class_contains(parent, "job-card", "job-primary", "job-item"):
            return parent
        if parent.name in {"li", "article"}:
            return parent
    return anchor


@dataclass(frozen=True)
class BossHtmlAdapter:
    base_url: str = "https://www.zhipin.com"

    def extract_job_cards(self, html: str, *, source_url: str) -> list[JobCard]:
        soup = BeautifulSoup(html, "html.parser")
        detail_card = self._extract_selected_detail_card(soup, source_url=source_url)
        if detail_card is not None:
            return [detail_card]
        cards: dict[str, JobCard] = {}
        anchors = [
            anchor
            for anchor in soup.find_all("a", href=True)
            if "/job_detail/" in str(anchor.get("href"))
        ]
        for anchor in anchors:
            detail_url = urljoin(source_url or self.base_url, str(anchor["href"]))
            root = _candidate_card(anchor)
            raw_text = normalize_text(root.get_text(" "))
            title = (
                _first_text(root, (".job-name", ".job-title", "[data-role='job-title']", "h3"))
                or normalize_text(anchor.get_text(" "))
                or "Unknown role"
            )
            company = (
                _first_text(
                    root,
                    (".company-name", ".company-text", ".company", "[data-role='company']"),
                )
                or "Unknown company"
            )
            salary = _first_text(root, (".salary", ".red", "[data-role='salary']"))
            if salary is None:
                salary = _salary_from_text(raw_text)
            location = _first_text(root, (".job-area", ".location", "[data-role='location']"))
            platform_job_id = (
                str(root.get("data-job-id") or root.get("data-jobid") or "")
                or _detail_url_job_id(detail_url)
                or str(root.get("data-lid") or "")
                or _job_id_from_url(detail_url)
                or _stable_job_id(title, company, detail_url)
            )
            if platform_job_id not in cards:
                cards[platform_job_id] = JobCard(
                    platform_job_id=platform_job_id,
                    title=title,
                    company=company,
                    source_url=source_url,
                    detail_url=detail_url,
                    salary=salary,
                    location=location,
                    raw_text=raw_text,
                )
        return list(cards.values())

    def _extract_selected_detail_card(
        self, soup: BeautifulSoup, *, source_url: str
    ) -> JobCard | None:
        detail = soup.select_one(".job-detail-box, .job-detail-container")
        if not detail:
            return None
        raw_text = normalize_text(detail.get_text(" "))
        if not raw_text or "立即沟通" not in raw_text:
            return None

        title_text = _first_text(detail, (".job-detail-info", ".job-detail-header", "h1", "h2"))
        if not title_text:
            return None
        title, salary = _split_title_salary(title_text)
        company = (
            _company_from_boss_attr(_first_text(detail, (".boss-info-attr",)))
            or _first_text(soup, (".job-boss-info .boss-info-attr", ".boss-name", ".company-name"))
            or "Unknown company"
        )
        location = _first_text(detail, (".location", ".job-area", ".company-location"))
        platform_job_id = _detail_url_job_id(source_url) or _stable_job_id(
            title, company, source_url
        )
        return JobCard(
            platform_job_id=platform_job_id,
            title=title,
            company=company,
            source_url=source_url,
            detail_url=source_url,
            salary=salary,
            location=location,
            raw_text=raw_text,
        )

    def page_risks(self, html: str) -> list[PageRisk]:
        text = normalize_text(BeautifulSoup(html, "html.parser").get_text(" "))
        risks: list[PageRisk] = []
        for marker in LOGIN_MARKERS:
            if marker in text:
                risks.append(PageRisk(reason="login_required_or_policy_gate", evidence=marker))
        for marker in POLICY_GATE_MARKERS:
            if marker in text and "用户协议" in text and "隐私政策" in text:
                risks.append(PageRisk(reason="login_required_or_policy_gate", evidence=marker))
        for marker in SECURITY_MARKERS:
            if marker in text:
                risks.append(PageRisk(reason="security_verification", evidence=marker))
        for marker in AMBIGUOUS_RESUME_MARKERS:
            if marker in text:
                risks.append(PageRisk(reason="ambiguous_resume_selection", evidence=marker))
        return risks

    def button_labels(self, html: str, labels: tuple[str, ...]) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        found: list[str] = []
        controls = list(soup.find_all(["button", "a"]))
        controls.extend(
            element
            for element in soup.find_all(attrs={"role": "button"})
            if element not in controls
        )
        for element in controls:
            text = normalize_text(element.get_text(" "))
            if text in labels:
                found.append(text)
        return found

    def can_click_contact(self, html: str) -> tuple[bool, list[PageRisk]]:
        risks = self.page_risks(html)
        labels = self.button_labels(html, CONTACT_LABELS)
        if len(labels) != 1:
            risks.append(
                PageRisk(
                    reason="contact_button_not_unique",
                    evidence=f"found {len(labels)} contact buttons",
                )
            )
        return not risks, risks

    def can_click_send_resume(self, html: str) -> tuple[bool, list[PageRisk]]:
        risks = self.page_risks(html)
        labels = self.button_labels(html, SEND_RESUME_LABELS)
        if len(labels) != 1:
            risks.append(
                PageRisk(
                    reason="send_resume_button_not_unique",
                    evidence=f"found {len(labels)} send-resume buttons",
                )
            )
        return not risks, risks

    def extract_recruiter_replies(self, html: str) -> list[RecruiterReply]:
        soup = BeautifulSoup(html, "html.parser")
        replies: list[RecruiterReply] = []
        candidates = soup.select(
            "[data-conversation-id], .chat-message, .message, .msg, .chat-item, .item-message"
        )
        for index, element in enumerate(candidates):
            if not isinstance(element, Tag):
                continue
            text = normalize_text(element.get_text(" "))
            if not text:
                continue
            role = str(
                element.get("data-sender")
                or element.get("data-role")
                or element.get("data-from")
                or ""
            ).lower()
            classes = " ".join(str(item).lower() for item in element.get("class", []))
            is_system = any(marker in text for marker in SYSTEM_REPLY_MARKERS) or "system" in role
            sender = "system" if is_system else "unknown"
            if any(marker in role or marker in classes for marker in ("boss", "recruiter", "left")):
                sender = "recruiter"
            elif any(marker in role or marker in classes for marker in ("self", "me", "right")):
                sender = "self"
            conversation_id = str(element.get("data-conversation-id") or f"conversation-{index}")
            replies.append(
                RecruiterReply(
                    conversation_id=conversation_id,
                    text=text,
                    sender=sender,
                    is_system=is_system,
                    job_id=str(element.get("data-job-id") or "") or None,
                )
            )
        return replies

    def reply_is_candidate_for_resume(self, reply: RecruiterReply) -> bool:
        if reply.is_system or reply.sender != "recruiter":
            return False
        return not any(marker in reply.text for marker in NEGATIVE_REPLY_MARKERS)


class HumanPauseRequired(RuntimeError):
    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}
