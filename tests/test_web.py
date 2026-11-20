"""网页流程测试：上传、预览、校订、发布、下载、过期页与失败原子性。"""
from __future__ import annotations

import re

import pytest

from app import core
from app.app import create_app
VTT = """WEBVTT

1
00:00:01.000 --> 00:00:05.000
第一句原话。

2
00:00:05.000 --> 00:00:10.000
第二句原话，比较长，
分成两行。

3
00:00:10.000 --> 00:00:15.000
第三句原话。
"""

CSV = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:12.000
C,00:00:12.000,00:00:15.000
"""


@pytest.fixture()
def app(tmp_path):
    application = create_app(str(tmp_path / "test.db"))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, vtt=VTT, csv=CSV):
    response = client.post(
        "/upload",
        data={
            "vtt": (open_bytes(vtt), "interview.vtt"),
            "csv": (open_bytes(csv), "edit.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302, response.data.decode()
    match = re.search(r"/draft/(\d+)", response.headers["Location"])
    assert match, response.headers["Location"]
    return int(match.group(1))


def open_bytes(text: str):
    import io

    return io.BytesIO(text.encode("utf-8"))


def draft_form(client, draft_id):
    """读取草稿页，解析出隐藏的版本号与每个剪口出现的输入项。"""
    page = client.get(f"/draft/{draft_id}")
    html = page.data.decode()
    revision = re.search(r'name="revision" value="(\d+)"', html).group(1)
    return page, revision, html


def occurrence_fields(html):
    return sorted({int(m) for m in re.findall(r'name="status:(\d+)"', html)})


def fill(client, draft_id, decider, publish=False):
    page, revision, html = draft_form(client, draft_id)
    data = {"revision": revision, "page": "1", "scope": "all"}
    for occurrence_id in occurrence_fields(html):
        status, text = decider(occurrence_id)
        data[f"status:{occurrence_id}"] = status
        data[f"text:{occurrence_id}"] = text
    url = f"/draft/{draft_id}/" + ("publish" if publish else "save")
    return client.post(url, data=data, follow_redirects=False)


class TestUploadAndPreview:
    def test_upload_creates_draft_and_previews_both_timelines(self, client):
        draft_id = upload(client)
        page, revision, html = draft_form(client, draft_id)
        assert page.status_code == 200
        # 每一次出现都同时给出原片与节选时码
        assert "原片" in html and "节选" in html
        assert "00:00:01.000" in html  # 原片起点
        assert "00:00:00.000" in html  # 节选起点
        # 片段 B 的节选偏移 = A 的 4s
        assert "00:00:04.000" in html
        # 剪口截断的条目需要录入
        assert occurrence_fields(html)

    def test_bad_upload_reports_error(self, client):
        response = client.post(
            "/upload",
            data={
                "vtt": (open_bytes("not a vtt"), "x.vtt"),
                "csv": (open_bytes(CSV), "x.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "上传失败" in response.data.decode()

    def test_duplicate_fragment_number_rejected(self, client):
        bad_csv = "片段号,原片起毫秒,原片止毫秒\nA,0,1000\nA,1000,2000\n"
        response = client.post(
            "/upload",
            data={
                "vtt": (open_bytes(VTT), "x.vtt"),
                "csv": (open_bytes(bad_csv), "x.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert "片段号重复" in response.data.decode()


class TestReviewAndPublish:
    def test_cannot_publish_until_every_cut_handled(self, client):
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        assert fields
        # 只处理第一处就发布会失败
        data = {"revision": revision, f"status:{fields[0]}": "delete"}
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422
        assert "未处理" in response.data.decode()
        # 失败不产生新版
        assert client.get("/editions").data.decode().count("下载 WebVTT") == 0

    def test_full_publish_and_download(self, client):
        draft_id = upload(client)
        response = fill(
            client,
            draft_id,
            lambda index: ("keep", f"保留文字{index}"),
            publish=True,
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "/editions/1" in location

        download = client.get("/editions/1/download")
        assert download.status_code == 200
        assert download.headers["Content-Type"].startswith("text/vtt")
        body = download.data.decode("utf-8")
        assert body.startswith("WEBVTT")
        assert body.endswith("\n")

        parsed = core.parse_vtt(body)
        texts = [cue.text for cue in parsed]
        assert "第一句原话。" in texts  # 整条保留沿用正文
        # 剪口被截断的每一次出现都按各自录入的文字输出（按 occurrence 行号录入）
        assert "保留文字2" in texts
        assert "保留文字4" in texts
        assert "保留文字5" in texts
        assert len([t for t in texts if t.startswith("保留文字")]) == 3

    def test_deleted_cut_occurrence_absent_from_output(self, client):
        draft_id = upload(client)
        fill(client, draft_id, lambda index: ("delete", ""), publish=True)
        body = client.get("/editions/1/download").data.decode()
        parsed = core.parse_vtt(body)
        # 被删掉的剪口不出现，但整条保留的仍在
        assert "第一句原话。" in [cue.text for cue in parsed]
        assert body.count("-->") == len(parsed)

    def test_publish_twice_from_same_page_is_rejected(self, client):
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        data = {"revision": revision}
        for index in occurrence_fields(html):
            data[f"status:{index}"] = "delete"
        first = client.post(f"/draft/{draft_id}/publish", data=data)
        assert first.status_code == 302
        second = client.post(f"/draft/{draft_id}/publish", data=data)
        assert second.status_code == 409
        assert "过期" in second.data.decode()


class TestStaleDraft:
    def test_old_page_rejected_after_new_upload(self, client):
        old_id = upload(client)
        _, old_revision, old_html = draft_form(client, old_id)
        fields = occurrence_fields(old_html)

        new_id = upload(client, csv=CSV.replace("00:00:12.000", "00:00:11.000"))
        assert new_id != old_id

        data = {"revision": old_revision}
        for index in fields:
            data[f"status:{index}"] = "delete"
        response = client.post(f"/draft/{old_id}/save", data=data)
        assert response.status_code == 409
        assert "刷新" in response.data.decode()

    def test_old_page_rejected_on_publish(self, client):
        old_id = upload(client)
        _, old_revision, old_html = draft_form(client, old_id)
        fields = occurrence_fields(old_html)
        upload(client)  # 更换剪辑表

        data = {"revision": old_revision}
        for index in fields:
            data[f"status:{index}"] = "delete"
        response = client.post(f"/draft/{old_id}/publish", data=data)
        assert response.status_code == 409
        assert "过期" in response.data.decode()
        assert client.get("/editions").data.decode().count("下载 WebVTT") == 0


class TestRecomputeAndInheritance:
    def test_new_csv_recomputes_excerpt_times(self, client):
        draft_id = upload(client)
        _, _, old_html = draft_form(client, draft_id)
        # 原表：A 4s + B 9s => C 的节选从 13s 开始
        assert "00:00:13.000" in old_html
        fill(client, draft_id, lambda index: ("keep", f"校订{index}"), publish=False)
        # 把 B 剪短 1 秒：C 的节选时码整体前移 1 秒
        new_csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:11.000
C,00:00:12.000,00:00:15.000
"""
        new_id = upload(client, csv=new_csv)
        _, _, html = draft_form(client, new_id)
        # A 4s + B 8s = 12s，C 的节选为 12s–15s
        assert "00:00:12.000" in html
        assert "00:00:15.000" in html
        assert "00:00:13.000" not in html

    def test_inheritance_requires_fragment_text_and_range(self, client):
        review_csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:12.000
C,00:00:12.000,00:00:15.000
"""
        old_id = upload(client, csv=review_csv)
        _, revision, html = draft_form(client, old_id)
        fields = occurrence_fields(html)
        # A/C 各有一处剪口，B 的起止两端各截断一条字幕
        assert len(fields) == 3
        data = {"revision": revision}
        for index in fields:
            data[f"status:{index}"] = "keep"
            data[f"text:{index}"] = f"旧校订{index}"
        assert client.post(f"/draft/{old_id}/save", data=data).status_code == 302

        # 同一份剪辑表：三处剪口全部沿用
        same_id = upload(client, csv=review_csv)
        same_html = draft_form(client, same_id)[2]
        assert same_html.count("沿用上一版校订") == 3
        assert "旧校订" in same_html

        # 只改变 B 的止毫秒：B 里结束端被截的那条原片范围变了，不再沿用
        changed = review_csv.replace(
            "00:00:03.000,00:00:12.000", "00:00:03.000,00:00:11.000"
        )
        changed_id = upload(client, csv=changed)
        changed_html = draft_form(client, changed_id)[2]
        assert changed_html.count("沿用上一版校订") == 2

    def test_changed_original_text_loses_inheritance(self, client):
        old_csv = "片段号,原片起毫秒,原片止毫秒\nA,00:00:03.000,00:00:08.000\n"
        old_id = upload(client, csv=old_csv)
        revision = draft_form(client, old_id)[1]
        fields = occurrence_fields(draft_form(client, old_id)[2])
        assert len(fields) == 2  # cue1 起端截断、cue2 末端截断
        data = {"revision": revision}
        for index in fields:
            data[f"status:{index}"] = "keep"
            data[f"text:{index}"] = f"旧校订{index}"
        client.post(f"/draft/{old_id}/save", data=data)

        # 改掉其中一条原字幕的文字：只有这一条失去沿用
        new_vtt = VTT.replace("第二句原话，比较长，", "第二句原话，改过，")
        new_id = upload(client, csv=old_csv, vtt=new_vtt)
        new_html = draft_form(client, new_id)[2]
        assert new_html.count("沿用上一版校订") == 1

    def test_same_span_taken_twice_needs_two_entries(self, client):
        csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:03.000,00:00:12.000
A2,00:00:03.000,00:00:12.000
"""
        draft_id = upload(client, csv=csv)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        # 两段相同的原片范围各出现一次，需分别处理
        assert len(fields) >= 2
        data = {"revision": revision, f"status:{fields[0]}": "delete"}
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422


class TestPersistence:
    def test_edition_survives_restart(self, tmp_path):
        db = str(tmp_path / "restart.db")
        first = create_app(db)
        first.config.update(TESTING=True)
        client = first.test_client()
        draft_id = upload(client)
        fill(client, draft_id, lambda index: ("delete", ""), publish=True)
        expected = client.get("/editions/1/download").data

        second = create_app(db)  # 模拟重启
        second.config.update(TESTING=True)
        again = second.test_client().get("/editions/1/download")
        assert again.status_code == 200
        assert again.data == expected

    def test_failed_publish_leaves_no_version(self, tmp_path):
        db = str(tmp_path / "atomic.db")
        application = create_app(db)
        application.config.update(TESTING=True)
        client = application.test_client()
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        data = {"revision": revision}
        for index in fields[:-1]:
            data[f"status:{index}"] = "delete"
        # 最后一处留空，发布应被拒绝
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422

        conn = core.connect(db)
        try:
            assert core.get_edition(conn, 1) is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 分页校订
# ---------------------------------------------------------------------------


def _big_fixture(fragment_total: int = 45, cue_per_fragment: int = 1):
    """构造 fragment_total 个片段、每页 20 个必然分页的稿；每片段都带剪口。"""
    cues = []
    cue_idx = 0
    csv_lines = ["片段号,原片起毫秒,原片止毫秒"]
    # 片段依次取原片 10s 窗口，相邻窗口重叠 1s，使每条字幕都被剪口截断。
    for i in range(fragment_total):
        start = 2_000 + i * 9_000
        end = start + 10_000
        csv_lines.append(f"F{i:03d},{start},{end}")
        for j in range(cue_per_fragment):
            cues.append(
                f"{cue_idx}\n"
                f"{core.format_timestamp(max(0, start - 500))} --> "
                f"{core.format_timestamp(start + 1_500)}\n"
                f"字幕{i}-{j}\n"
            )
            cue_idx += 1
    vtt = "WEBVTT\n\n" + "\n".join(cues)
    return vtt, "\n".join(csv_lines) + "\n"


def _page_html(client, draft_id, page=1, scope="all"):
    resp = client.get(f"/draft/{draft_id}?page={page}&scope={scope}")
    assert resp.status_code == 200
    return resp.data.decode()


def _shown_ordinals(html):
    return [int(m) for m in re.findall(r"剪辑顺序第 (\d+) 个", html)]


def _save_page(client, draft_id, html, *, status="delete", page="1", scope="all"):
    revision = re.search(r'name="revision" value="(\d+)"', html).group(1)
    data = {"revision": revision, "page": page, "scope": scope}
    for occurrence_id in occurrence_fields(html):
        data[f"status:{occurrence_id}"] = status
        if status == "keep":
            data[f"text:{occurrence_id}"] = f"文字{occurrence_id}"
    return client.post(f"/draft/{draft_id}/save", data=data, follow_redirects=False)


class TestPagination:
    def test_page_contains_at_mast_20_fragments_in_clip_order(self, client):
        vtt, csv_text = _big_fixture(45)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        page1 = _page_html(client, draft_id, 1)
        ordinals = _shown_ordinals(page1)
        assert len(ordinals) == 20
        assert ordinals == list(range(1, 21))  # 按剪辑顺序
        page3 = _page_html(client, draft_id, 3)
        assert _shown_ordinals(page3) == list(range(41, 46))  # 末页 5 个
        assert "第 3 / 3 页" in page3

    def test_each_page_is_a_small_form(self, client):
        vtt, csv_text = _big_fixture(45)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        html = _page_html(client, draft_id, 1)
        # 一页最多 20 个片段，字段只属于本页出现，不含其他页字段
        assert len(_shown_ordinals(html)) == 20
        all_ids = set(occurrence_fields(html))
        page2_ids = set(occurrence_fields(_page_html(client, draft_id, 2)))
        assert all_ids and all_ids.isdisjoint(page2_ids)

    def test_pending_scope_only_lists_fragments_with_pending_cuts(self, client):
        vtt, csv_text = _big_fixture(45)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        # 初始：全部片段都有待校订剪口
        pending_html = _page_html(client, draft_id, 1, "pending")
        assert len(_shown_ordinals(pending_html)) == 20
        # 把第一页（剪辑顺序前 20 个片段）全部标删
        assert _save_page(client, draft_id, _page_html(client, draft_id, 1)).status_code == 302
        # 待校订范围缩短为剩下 25 个片段，且从第 21 个开始，不漏不重
        pending_after = _page_html(client, draft_id, 1, "pending")
        assert _shown_ordinals(pending_after) == list(range(21, 41))
        page2 = _page_html(client, draft_id, 2, "pending")
        assert _shown_ordinals(page2) == list(range(41, 46))

    def test_save_only_touches_fragments_shown_on_that_page(self, client):
        vtt, csv_text = _big_fixture(45)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        conn = core.connect(client.application.config["DATABASE"])
        try:
            before = core.count_pending(conn, draft_id)
            assert before == 89
            html = _page_html(client, draft_id, 2)  # 只显示第 21-40 个片段
            page_fields = len(occurrence_fields(html))
            assert _save_page(client, draft_id, html, page="2").status_code == 302
            after = core.count_pending(conn, draft_id)
            assert after == before - page_fields  # 恰好本页出现被处理
            # 仍有待校订的片段数 = 45 - 20
            assert core.count_pending_fragments(conn, draft_id) == 25
        finally:
            conn.close()

    def test_walk_every_page_until_no_pending_then_publish_succeeds(self, client):
        vtt, csv_text = _big_fixture(45)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        # 按待校订范围逐页处理，直到列表为空
        page = 1
        while True:
            html = _page_html(client, draft_id, page, "pending")
            if not occurrence_fields(html):
                break
            resp = _save_page(client, draft_id, html, page=str(page), scope="pending")
            assert resp.status_code == 302
            page += 1
        assert page == 4  # 20 + 20 + 5 三页处理完，第四页为空
        last = _page_html(client, draft_id, 1, "pending")
        assert "没有待校订的剪口" in last
        # 全稿待校订数为 0 才允许发布
        revision = re.search(r'name="revision" value="(\d+)"', last).group(1)
        published = client.post(
            f"/draft/{draft_id}/publish",
            data={"revision": revision, "page": "1", "scope": "pending"},
        )
        assert published.status_code == 302
        assert "/editions/1" in published.headers["Location"]

    def test_redirect_after_last_pending_page_recovers_to_valid_page(self, client):
        vtt, csv_text = _big_fixture(45)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        for p in (1, 2):
            html = _page_html(client, draft_id, p, "pending")
            rev = re.search(r'name="revision" value="(\d+)"', html).group(1)
            data = {"revision": rev, "page": str(p), "scope": "pending"}
            for oid in occurrence_fields(html):
                data[f"status:{oid}"] = "delete"
            client.post(f"/draft/{draft_id}/save", data=data)
        # 最后一页（第 3 页）保存后重定向回 page=3，但已无待校订片段
        html = _page_html(client, draft_id, 3, "pending")
        rev = re.search(r'name="revision" value="(\d+)"', html).group(1)
        data = {"revision": rev, "page": "3", "scope": "pending"}
        for oid in occurrence_fields(html):
            data[f"status:{oid}"] = "delete"
        resp = client.post(f"/draft/{draft_id}/save", data=data)
        assert resp.status_code == 302
        body = client.get(resp.headers["Location"]).data.decode()
        assert "没有待校订的剪口" in body
        assert "第 1 / 1 页" in body  # 页码回收到有效页，不显示 3 / 1

    def test_saved_decisions_can_be_reviewed_and_modified(self, client):
        vtt, csv_text = _big_fixture(25)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        html = _page_html(client, draft_id, 1)
        first_id = occurrence_fields(html)[0]
        revision = re.search(r'name="revision" value="(\d+)"', html).group(1)
        client.post(
            f"/draft/{draft_id}/save",
            data={
                "revision": revision,
                "page": "1",
                "scope": "all",
                f"status:{first_id}": "keep",
                f"text:{first_id}": "初次录入",
            },
        )
        reopened = _page_html(client, draft_id, 1)
        assert "初次录入" in reopened  # 回看已存校订
        # 改成删除后再存
        revision2 = re.search(r'name="revision" value="(\d+)"', reopened).group(1)
        client.post(
            f"/draft/{draft_id}/save",
            data={
                "revision": revision2,
                "page": "1",
                "scope": "all",
                f"status:{first_id}": "delete",
            },
        )
        conn = core.connect(client.application.config["DATABASE"])
        try:
            decisions = core.decision_rows(conn, draft_id)
            kept = [d for d in decisions.values() if d["status"] == "keep"]
            assert not kept or all(d["signature"] for d in decisions.values())
            assert any(d["status"] == "delete" for d in decisions.values())
        finally:
            conn.close()

    def test_page_shows_total_pending_count_of_whole_draft(self, client):
        vtt, csv_text = _big_fixture(45)
        draft_id = upload(client, vtt=vtt, csv=csv_text)
        html = _page_html(client, draft_id, 2)
        assert "全稿待校订" in html
        assert re.search(r"全稿待校订\s*<strong[^>]*>89</strong>", html)
        # 处理完第一页片段后，全稿计数随之下降，第二页看到的是全稿最新值
        _save_page(client, draft_id, _page_html(client, draft_id, 1))
        conn = core.connect(client.application.config["DATABASE"])
        try:
            remaining = core.count_pending(conn, draft_id)
        finally:
            conn.close()
        html2 = _page_html(client, draft_id, 2)
        assert re.search(
            rf"全稿待校订\s*<strong[^>]*>{remaining}</strong>", html2
        )
        assert remaining < 89


class TestPersistenceBackfill:
    def test_legacy_sqlite_volume_is_backfilled_once_and_then_not_recomputed(
        self, tmp_path, monkeypatch
    ):
        db = str(tmp_path / "legacy.db")
        # 1) 用当前代码建一个有校订的草稿
        first = create_app(db)
        first.config.update(TESTING=True)
        client = first.test_client()
        draft_id = upload(client)
        fill(client, draft_id, lambda index: ("delete", ""), publish=False)
        conn = core.connect(db)
        try:
            assert core.is_persisted(conn, draft_id)
        finally:
            conn.close()

        # 2) 模拟旧卷：删掉持久化表内容，只保留 draft/decision/edition。
        conn = core.connect(db)
        try:
            conn.execute("DELETE FROM occurrence")
            conn.execute("DELETE FROM fragment")
            assert not core.is_persisted(conn, draft_id)
        finally:
            conn.close()

        # 3) 首次载入触发一次性回填，旧草稿与校订接着用
        second = create_app(db)
        second.config.update(TESTING=True)
        client2 = second.test_client()
        page = client2.get(f"/draft/{draft_id}")
        assert page.status_code == 200
        conn = core.connect(db)
        try:
            assert core.is_persisted(conn, draft_id)
            stats = core.draft_statistics(conn, draft_id)
            assert stats["fragment_count"] == 3
            assert stats["pending_count"] == 0  # 回填前保存的删除校订仍有效
        finally:
            conn.close()

        # 4) 回填后再打开/保存都不重算：监视 compute_occurrences 不应再被调用
        calls = {"n": 0}
        original = core.compute_occurrences

        def _spy(cuts, cues):
            calls["n"] += 1
            return original(cuts, cues)

        monkeypatch.setattr(core, "compute_occurrences", _spy)
        assert client2.get(f"/draft/{draft_id}?page=1&scope=pending").status_code == 200
        html = client2.get(f"/draft/{draft_id}").data.decode()
        revision = re.search(r'name="revision" value="(\d+)"', html).group(1)
        resp = client2.post(
            f"/draft/{draft_id}/save",
            data={"revision": revision, "page": "1", "scope": "all"},
        )
        assert resp.status_code in (302, 409)
        assert calls["n"] == 0

    def test_legacy_draft_backfill_then_full_publish(self, tmp_path):
        db = str(tmp_path / "legacy2.db")
        application = create_app(db)
        application.config.update(TESTING=True)
        client = application.test_client()
        draft_id = upload(client)
        conn = core.connect(db)
        try:
            conn.execute("DELETE FROM occurrence")
            conn.execute("DELETE FROM fragment")
        finally:
            conn.close()
        # 回填 + 逐页处理 + 发布
        fill(client, draft_id, lambda index: ("keep", f"回填校订{index}"), publish=True)
        body = client.get("/editions/1/download").data.decode()
        assert body.startswith("WEBVTT")
        # WebVTT 按剪辑顺序输出：节选起点单调不减
        parsed = core.parse_vtt(body)
        starts = [cue.start_ms for cue in parsed]
        assert starts == sorted(starts)


class TestOversizeAndStale:
    def test_form_over_limit_is_rejected_with_friendly_message(self, client):
        draft_id = upload(client)
        huge = "字" * (33 * 1024 * 1024)
        revision = draft_form(client, draft_id)[1]
        resp = client.post(
            f"/draft/{draft_id}/save",
            data={"revision": revision, "page": "1", "scope": "all",
                  "status:1": "keep", "text:1": huge},
        )
        assert resp.status_code == 302
        followed = client.get(resp.headers["Location"])
        assert "32 MiB" in followed.data.decode()
        # 没有任何校订写入
        conn = core.connect(client.application.config["DATABASE"])
        try:
            assert core.decision_rows(conn, draft_id) == {}
        finally:
            conn.close()

    def test_stale_submit_after_csv_change_prompts_refresh_with_paging(self, client):
        old_id = upload(client)
        old_html = draft_form(client, old_id)[2]
        fields = occurrence_fields(old_html)
        new_id = upload(client, csv=CSV.replace("00:00:12.000", "00:00:11.000"))
        assert new_id != old_id
        data = {"revision": draft_form(client, old_id)[1], "page": "1", "scope": "all"}
        for occurrence_id in fields:
            data[f"status:{occurrence_id}"] = "delete"
        resp = client.post(f"/draft/{old_id}/save", data=data)
        assert resp.status_code == 409
        body = resp.data.decode()
        assert "刷新" in body
        assert "页面已过期" in body
