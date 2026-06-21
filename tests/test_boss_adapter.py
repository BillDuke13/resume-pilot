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


def test_can_click_contact_scopes_count_to_selected_detail_box():
    adapter = BossHtmlAdapter()

    can_click, risks = adapter.can_click_contact(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
          </div>
          <div class="recommend-list">
            <div class="job-card-wrapper"><a class="op-btn">立即沟通</a></div>
          </div>
        </div>
        """
    )

    # A recommendation card's 立即沟通 outside the selected box must not count, so a
    # selected job with exactly one contact button stays clickable.
    assert can_click is True
    assert risks == []


def test_search_crawl_extracts_list_cards_despite_detail_pane():
    adapter = BossHtmlAdapter()
    html = """
    <div class="job-detail-container">
      <div class="job-detail-box">
        <div class="job-detail-info">Selected 30-50K</div>
        <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
        <div class="recommend-list">
          <a class="job-name" href="/job_detail/reco.html">Recommended 8-10K</a>
        </div>
      </div>
    </div>
    <div class="job-list">
      <a class="job-name" href="/job_detail/aaa.html">First 30-40K</a>
      <a class="job-name" href="/job_detail/bbb.html">Second 20-30K</a>
    </div>
    """
    url = "https://www.zhipin.com/web/geek/jobs"

    # The default detail-pane path returns only the auto-selected card.
    assert len(adapter.extract_job_cards(html, source_url=url)) == 1
    # Search-crawl mode skips the detail pane and returns every list card, but not
    # the recommendation links rendered inside that pane.
    crawl = adapter.extract_job_cards(html, source_url=url, include_detail_pane=False)
    ids = {job.platform_job_id for job in crawl}
    assert {"aaa", "bbb"} <= ids
    assert "reco" not in ids


def test_selected_detail_id_prefers_detail_url_over_recommendation_link():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <div class="job-detail-info">Selected Engineer 30-50K</div>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
            <div class="recommend-list">
              <a class="job-name" href="/job_detail/reco-other.html">Recommended 8-10K</a>
            </div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/job_detail/selected-real.html",
    )

    # The selected job has no data-job-id; its id must come from the detail URL, not
    # the recommendation link rendered inside the same box.
    assert jobs[0].platform_job_id == "selected-real"


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


def test_detail_page_ignores_recommendation_links():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <div class="job-detail-info">Selected Engineer 30-50K</div>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
            <div class="job-boss-info"><div class="boss-info-attr">Example Tech · HR</div></div>
          </div>
          <div class="recommend-list">
            <a class="job-name" href="/job_detail/reco1.html">Recommended One 8-10K</a>
            <a class="job-name" href="/job_detail/reco2.html">Recommended Two 9-12K</a>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/job_detail/selected.html",
    )

    assert len(jobs) == 1
    assert jobs[0].title == "Selected Engineer"
    assert jobs[0].platform_job_id == "selected"


def test_card_prefers_job_detail_url_id_over_volatile_data_lid():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <li class="job-card-wrapper" data-lid="session-token-abc">
          <a class="job-name" href="/job_detail/stable-id.html">Engineer</a>
          <span class="company-name">Example</span>
        </li>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs",
    )

    assert jobs[0].platform_job_id == "stable-id"


def test_detail_card_prefers_panel_platform_job_id():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box" data-job-id="boss-real-123">
            <div class="job-detail-header">
              <div class="job-detail-info">Platform Engineer 30-50K</div>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
            <div class="job-boss-info"><div class="boss-info-attr">Example Tech · HR</div></div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs?query=k8s",
    )

    assert jobs[0].platform_job_id == "boss-real-123"


def test_detail_card_uses_panel_job_detail_link_for_id():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <a class="job-detail-info" href="/job_detail/linked456.html">Engineer 30-50K</a>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
            <div class="job-boss-info"><div class="boss-info-attr">Example Tech · HR</div></div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs?query=k8s",
    )

    assert jobs[0].platform_job_id == "linked456"


def test_detail_card_id_stable_across_different_search_urls():
    adapter = BossHtmlAdapter()

    def detail_html() -> str:
        return """
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
        """

    from_k8s = adapter.extract_job_cards(
        detail_html(),
        source_url="https://www.zhipin.com/web/geek/jobs?query=k8s",
    )
    from_sre = adapter.extract_job_cards(
        detail_html(),
        source_url="https://www.zhipin.com/web/geek/jobs?query=sre",
    )

    # The same posting opened from two different search keywords must dedupe to one id,
    # so the duplicate-contact guard cannot be bypassed by reopening from another query.
    assert from_k8s[0].platform_job_id == from_sre[0].platform_job_id


def test_detail_page_selected_in_conversation_does_not_borrow_recommendation_button():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box">
            <div class="job-detail-header">
              <div class="job-detail-info">Selected Engineer 30-50K</div>
              <div class="job-detail-op"><a class="op-btn">继续沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
            <div class="job-boss-info"><div class="boss-info-attr">Example Tech · HR</div></div>
          </div>
          <div class="recommend-list">
            <div class="job-card-wrapper">
              <a class="job-name" href="/job_detail/reco1.html">Recommended One 8-10K</a>
              <a class="op-btn">立即沟通</a>
            </div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/job_detail/selected.html",
    )

    # The selected job is already in conversation (its box has 继续沟通, not 立即沟通),
    # so it must not be surfaced as the contactable detail card by borrowing the
    # recommendation card's 立即沟通 button — that would record a contact against the
    # wrong posting.
    ids = {job.platform_job_id for job in jobs}
    titles = {job.title for job in jobs}
    assert "selected" not in ids
    assert "Selected Engineer" not in titles


def test_detail_container_without_box_does_not_borrow_recommendation_contact():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <h1>Selected No Box Engineer 30-50K</h1>
          <div class="recommend-list">
            <div class="job-card-wrapper">
              <a class="job-name" href="/job_detail/reco9.html">Recommended Nine 8-10K</a>
              <a class="op-btn">立即沟通</a>
            </div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/job_detail/selected-no-box.html",
    )

    # Without a .job-detail-box there is no authoritative selected job, so the
    # outer container's only 立即沟通 (a recommendation's) must not be borrowed to
    # synthesize a contactable card for the detail URL's posting.
    titles = {job.title for job in jobs}
    assert "Selected No Box Engineer" not in titles


def test_detail_card_prefers_job_detail_link_over_volatile_data_lid():
    adapter = BossHtmlAdapter()

    jobs = adapter.extract_job_cards(
        """
        <div class="job-detail-container">
          <div class="job-detail-box" data-lid="session-token-xyz">
            <div class="job-detail-header">
              <a class="job-detail-info" href="/job_detail/stable999.html">Engineer 30-50K</a>
              <div class="job-detail-op"><a class="op-btn">立即沟通</a></div>
            </div>
            <div class="job-detail-body">职位描述 Python</div>
          </div>
        </div>
        """,
        source_url="https://www.zhipin.com/web/geek/jobs?query=k8s",
    )

    # A volatile data-lid session token must not win over the stable /job_detail/
    # id, or the duplicate-contact guard fails when the posting is reopened.
    assert jobs[0].platform_job_id == "stable999"
