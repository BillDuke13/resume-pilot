from __future__ import annotations

from resume_pilot.boss import BossHtmlAdapter, decode_obfuscated_digits

JOB_HTML = """
<html>
  <body>
    <ul>
      <li class="job-card-wrapper" data-job-id="abc123">
        <a class="job-name" href="/job_detail/abc123.html">Python Automation Engineer</a>
        <span class="salary">30-45K</span>
        <span class="job-area">Beijing</span>
        <span class="company-name">Example Tech</span>
        <button>立即沟通</button>
      </li>
      <li class="job-card-wrapper">
        <a class="job-name" href="/job_detail/def456.html">Backend Engineer</a>
        <span class="salary">25-35K</span>
        <span class="job-area">Shanghai</span>
        <span class="company-name">Data Tools</span>
      </li>
      <li class="job-card-wrapper" data-job-id="abc123">
        <a class="job-name" href="/job_detail/abc123.html">Python Automation Engineer</a>
        <span class="company-name">Example Tech</span>
      </li>
    </ul>
  </body>
</html>
"""


def test_extract_job_cards_deduplicates_and_normalizes():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(JOB_HTML, source_url="https://www.zhipin.com/web/geek/job")

    assert [job.platform_job_id for job in jobs] == ["abc123", "def456"]
    assert jobs[0].title == "Python Automation Engineer"
    assert jobs[0].company == "Example Tech"
    assert jobs[0].detail_url == "https://www.zhipin.com/job_detail/abc123.html"


def test_extract_job_cards_decodes_obfuscated_salary_digits():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <li class="job-card-wrapper" data-job-id="sre-1">
          <a class="job-name" href="/job_detail/sre-1.html">SRE Engineer</a>
          <span class="company-name">Infra Co</span>
          <span>-K·薪</span>
        </li>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs",
    )

    assert decode_obfuscated_digits("-K·薪") == "35-50K·16薪"
    assert jobs[0].salary == "35-50K·16薪"
    assert "35-50K" in (jobs[0].raw_text or "")


def test_contact_button_must_be_unique_and_page_must_be_safe():
    adapter = BossHtmlAdapter()

    can_click, risks = adapter.can_click_contact(JOB_HTML)

    assert can_click is True
    assert risks == []


def test_contact_button_ignores_plain_container_text():
    adapter = BossHtmlAdapter()

    can_click, risks = adapter.can_click_contact("<div><button>立即沟通</button></div>")

    assert can_click is True
    assert risks == []


def test_login_or_captcha_page_pauses_clicking():
    adapter = BossHtmlAdapter()

    can_click, risks = adapter.can_click_send_resume(
        "<html><body>验证码 请选择简历 <button>发送简历</button></body></html>"
    )

    assert can_click is False
    assert {risk.reason for risk in risks} >= {
        "security_verification",
        "ambiguous_resume_selection",
    }


def test_privacy_policy_footer_is_not_a_login_gate():
    adapter = BossHtmlAdapter()

    risks = adapter.page_risks("<footer>用户协议 隐私政策</footer><button>立即沟通</button>")

    assert [risk.reason for risk in risks] == []


def test_recruiter_reply_candidates_ignore_system_and_self_messages():
    adapter = BossHtmlAdapter()
    replies = adapter.extract_recruiter_replies(
        """
        <div class="chat-message system">系统消息 已读</div>
        <div class="chat-message right" data-sender="self">您好</div>
        <div class="chat-message left" data-sender="boss" data-conversation-id="c1">
          可以发一份简历吗
        </div>
        <div class="chat-message left" data-sender="boss" data-conversation-id="c2">
          暂不考虑
        </div>
        """
    )

    candidates = [reply for reply in replies if adapter.reply_is_candidate_for_resume(reply)]
    assert len(candidates) == 1
    assert candidates[0].conversation_id == "c1"


def test_extract_selected_job_detail_when_list_uses_javascript_links():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <div class="job-detail-info">Platform Reliability Engineer 35-50K</div>
              <div class="job-detail-op"><a class="op-btn op-btn-chat">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python Linux SRE</div>
            <div class="job-boss-info">
              <div class="boss-info-attr">Example Tech · HRBP</div>
            </div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs",
    )

    assert len(jobs) == 1
    assert jobs[0].title == "Platform Reliability Engineer"
    assert jobs[0].salary == "35-50K"
    assert jobs[0].company == "Example Tech"


def test_detail_card_parses_annual_pay_salary_suffix():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <div class="job-detail-info">Senior Backend Engineer 30-60K·15薪</div>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
            <div class="job-boss-info"><div class="boss-info-attr">Example Tech · HR</div></div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs",
    )

    assert len(jobs) == 1
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].salary == "30-60K·15薪"


def test_detail_card_job_id_is_stable_across_volatile_page_text():
    adapter = BossHtmlAdapter()

    def detail_html(transient: str) -> str:
        return f"""
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <div class="job-detail-info">Platform Engineer 30-50K</div>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python {transient}</div>
            <div class="job-boss-info"><div class="boss-info-attr">Example Tech · HR</div></div>
          </div>
        </div>
        """

    first = adapter.extract_job_cards(
        detail_html("在线"),
        source_url="https://www.zhipin.com/web/geek/jobs",
    )
    second = adapter.extract_job_cards(
        detail_html("刚刚活跃 验证码已通过"),
        source_url="https://www.zhipin.com/web/geek/jobs",
    )

    assert first[0].platform_job_id == second[0].platform_job_id


def test_list_container_does_not_bleed_details_across_cards():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-list">
          <a class="job-name" href="/job_detail/aaa.html">First Role 30-40K</a>
          <a class="job-name" href="/job_detail/bbb.html">Second Role</a>
        </div>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs",
    )

    by_id = {job.platform_job_id: job for job in jobs}
    assert set(by_id) == {"aaa", "bbb"}
    assert by_id["aaa"].salary == "30-40K"
    assert by_id["bbb"].salary is None


def test_detail_card_uses_job_detail_url_id_for_dedupe():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <div class="job-detail-info">Platform Engineer 30-50K</div>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
            <div class="job-boss-info"><div class="boss-info-attr">Example Tech · HR</div></div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/job_detail/xyz789.html",
    )

    assert jobs[0].platform_job_id == "xyz789"
