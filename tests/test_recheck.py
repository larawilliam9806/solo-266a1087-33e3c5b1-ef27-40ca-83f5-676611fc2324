# -*- coding: utf-8 -*-
"""盲样复验：开单、盲号、回报入库/订正、失效与待复验名单。"""
import csv
import io

import pytest

from app import create_app
from app.csvio import CsvError, parse_report_csv


@pytest.fixture()
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


CSV_HEADER = "样号,起厘米,止厘米,品位ppm,复验标记\n"


def _make_hole(app, client, code="ZK001"):
    client.post("/holes", data={"code": code, "name": "一号孔"}, follow_redirects=True)
    with app.app_context():
        from app.db import get_db
        return get_db().execute("SELECT id FROM hole WHERE code = ?", (code,)).fetchone()["id"]


def _upload_assay(client, hole_id, content):
    data = {"assay_csv": (io.BytesIO(content.encode("utf-8")), "assay.csv")}
    return client.post(f"/holes/{hole_id}/upload", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


def _open_order(client, hole_id, samples, max_diff):
    return client.post(
        f"/holes/{hole_id}/rechecks",
        data={"sample_no": samples, "max_diff_ppm": str(max_diff)},
        follow_redirects=True,
    )


def _upload_report(client, order_id, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["盲号", "品位ppm"])
    for code, grade in rows:
        w.writerow([code, grade])
    data = {"report_csv": (io.BytesIO(buf.getvalue().encode("utf-8")), "report.csv")}
    return client.post(f"/rechecks/{order_id}/report", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


def _order_state(app, order_id, report_id=None):
    from app.recheck import get_order
    with app.app_context():
        return get_order(order_id, report_id)


def _codes(state):
    return [(row["item"]["sample_no"], [c["code"] for c in row["codes"]])
            for row in state["items"]]


class TestOpenOrder:
    def test_open_order_creates_two_unique_blind_codes(self, app, client):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 200"},
                    follow_redirects=True)
        _upload_assay(client, hole_id,
                      CSV_HEADER + "S1,0,100,300,否\nS2,100,200,400,是\n")
        resp = _open_order(client, hole_id, ["S1", "S2"], 10)
        body = resp.get_data(as_text=True)
        assert "已开出" in body and "盲号" in body

        with app.app_context():
            from app.db import get_db
            order = get_db().execute("SELECT * FROM recheck_order").fetchone()
            codes = get_db().execute("SELECT code FROM blind_code").fetchall()
        assert order["max_diff_ppm"] == 10
        assert len(codes) == 4  # 2 样号 × 2 盲号
        assert len({c["code"] for c in codes}) == 4
        state = _order_state(app, order["id"])
        for _, pair in _codes(state):
            assert len(pair) == 2 and pair[0] != pair[1]

    def test_blind_codes_unique_across_orders(self, app, client):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 100"},
                    follow_redirects=True)
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")
        _open_order(client, hole_id, ["S1"], 5)
        _open_order(client, hole_id, ["S1"], 5)
        with app.app_context():
            from app.db import get_db
            codes = [r["code"] for r in get_db().execute("SELECT code FROM blind_code")]
        assert len(codes) == 4 and len(set(codes)) == 4

    def test_bad_max_diff_rejected(self, app, client):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 100"},
                    follow_redirects=True)
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")
        resp = _open_order(client, hole_id, ["S1"], -1)
        assert "非负整数" in resp.get_data(as_text=True)
        resp = _open_order(client, hole_id, ["S1"], "1.5")
        assert "非负整数" in resp.get_data(as_text=True)
        with app.app_context():
            from app.db import get_db
            assert get_db().execute("SELECT COUNT(*) c FROM recheck_order").fetchone()["c"] == 0

    def test_unknown_or_empty_samples_rejected(self, app, client):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 100"},
                    follow_redirects=True)
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")
        resp = _open_order(client, hole_id, ["NOPE"], 5)
        assert "不在当前生效化验" in resp.get_data(as_text=True)
        resp = _open_order(client, hole_id, [], 5)
        assert "至少勾选" in resp.get_data(as_text=True)


class TestReport:
    def _setup(self, app, client, max_diff=10):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 100"},
                    follow_redirects=True)
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\nS2,0,100,200,否\n")
        _open_order(client, hole_id, ["S1", "S2"], max_diff)
        with app.app_context():
            from app.db import get_db
            order_id = get_db().execute("SELECT id FROM recheck_order").fetchone()["id"]
        state = _order_state(app, order_id)
        return hole_id, order_id, {row["item"]["sample_no"]: [c["code"] for c in row["codes"]]
                                   for row in state["items"]}

    def test_report_computes_diff_and_over_limit(self, app, client):
        _, order_id, codes = self._setup(app, client, max_diff=10)
        resp = _upload_report(client, order_id, [
            (codes["S1"][0], 100), (codes["S1"][1], 105),   # 差 5，限内
            (codes["S2"][0], 100), (codes["S2"][1], 200),   # 差 100，超差
        ])
        body = resp.get_data(as_text=True)
        assert "回报已入库" in body and "1 个样号" in body
        state = _order_state(app, order_id)
        by_sample = {row["item"]["sample_no"]: row for row in state["items"]}
        assert by_sample["S1"]["diff_ppm"] == 5 and not by_sample["S1"]["over_limit"]
        assert by_sample["S2"]["diff_ppm"] == 100 and by_sample["S2"]["over_limit"]
        # 页面标出超差样号与差值
        assert "S2" in body and "100" in body

    def test_result_exactly_at_limit_not_over(self, app, client):
        _, order_id, codes = self._setup(app, client, max_diff=10)
        _upload_report(client, order_id, [
            (codes["S1"][0], 100), (codes["S1"][1], 110),
            (codes["S2"][0], 0), (codes["S2"][1], 10),
        ])
        state = _order_state(app, order_id)
        assert all(not row["over_limit"] for row in state["items"])

    def test_unknown_code_rejected_and_no_report(self, app, client):
        _, order_id, codes = self._setup(app, client)
        resp = _upload_report(client, order_id, [
            (codes["S1"][0], 1), (codes["S1"][1], 2),
            (codes["S2"][0], 3), (codes["S2"][1], 4),
            ("BNOTMINE", 5),
        ])
        assert "不属于本单" in resp.get_data(as_text=True)
        assert _order_state(app, order_id)["selected_report"] is None

    def test_missing_code_rejected(self, app, client):
        _, order_id, codes = self._setup(app, client)
        resp = _upload_report(client, order_id, [
            (codes["S1"][0], 1), (codes["S1"][1], 2),
            (codes["S2"][0], 3),
        ])
        assert "缺少本单盲号" in resp.get_data(as_text=True)
        assert _order_state(app, order_id)["selected_report"] is None

    def test_duplicate_row_in_csv_rejected(self):
        text = "盲号,品位ppm\nBAAA,1\nBAAA,2\n"
        with pytest.raises(CsvError, match="恰好一行"):
            parse_report_csv(text)

    def test_negative_or_non_integer_grade_rejected(self):
        with pytest.raises(CsvError, match="非负整数"):
            parse_report_csv("盲号,品位ppm\nBAAA,-1\n")
        with pytest.raises(CsvError, match="非负整数"):
            parse_report_csv("盲号,品位ppm\nBAAA,1.5\n")

    def test_correction_supersedes_and_history_queryable(self, app, client):
        _, order_id, codes = self._setup(app, client, max_diff=10)
        _upload_report(client, order_id, [
            (codes["S1"][0], 100), (codes["S1"][1], 200),   # 超差
            (codes["S2"][0], 100), (codes["S2"][1], 105),   # 限内
        ])
        first = _order_state(app, order_id)["selected_report"]["id"]
        resp = _upload_report(client, order_id, [
            (codes["S1"][0], 100), (codes["S1"][1], 104),   # 已订正为限内
            (codes["S2"][0], 100), (codes["S2"][1], 105),
        ])
        assert "回报已入库" in resp.get_data(as_text=True)
        with app.app_context():
            from app.db import get_db
            reports = get_db().execute(
                "SELECT id, seq, status FROM recheck_report ORDER BY seq").fetchall()
        assert [(r["seq"], r["status"]) for r in reports] == [
            (1, "superseded"), (2, "effective")]
        # 当前回报：S1 不再超差
        current = _order_state(app, order_id)
        assert {r["item"]["sample_no"]: r["over_limit"] for r in current["items"]} == {
            "S1": False, "S2": False}
        # 历史回报仍可查询且保留原判断
        hist = _order_state(app, order_id, first)
        assert {r["item"]["sample_no"]: r["over_limit"] for r in hist["items"]} == {
            "S1": True, "S2": False}

    def test_failed_correction_keeps_previous_report(self, app, client):
        _, order_id, codes = self._setup(app, client, max_diff=10)
        _upload_report(client, order_id, [
            (codes["S1"][0], 100), (codes["S1"][1], 100),
            (codes["S2"][0], 100), (codes["S2"][1], 500),
        ])
        before = _order_state(app, order_id)
        assert before["selected_report"]["seq"] == 1
        resp = _upload_report(client, order_id, [(codes["S1"][0], 1)])  # 缺号
        assert "沿用上次回报" in resp.get_data(as_text=True)
        after = _order_state(app, order_id)
        assert after["selected_report"]["id"] == before["selected_report"]["id"]
        assert {r["item"]["sample_no"]: r["over_limit"] for r in after["items"]} == {
            "S1": False, "S2": True}


class TestInvalidation:
    def _setup(self, app, client):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 100"},
                    follow_redirects=True)
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\nS2,0,100,200,否\n")
        _open_order(client, hole_id, ["S1", "S2"], 10)
        with app.app_context():
            from app.db import get_db
            order_id = get_db().execute("SELECT id FROM recheck_order").fetchone()["id"]
        codes = {row["item"]["sample_no"]: [c["code"] for c in row["codes"]]
                 for row in _order_state(app, order_id)["items"]}
        _upload_report(client, order_id, [
            (codes["S1"][0], 100), (codes["S1"][1], 200),   # S1 超差
            (codes["S2"][0], 100), (codes["S2"][1], 300),   # S2 超差
        ])
        return hole_id, order_id

    def test_changed_record_invalidates_permanently(self, app, client):
        hole_id, order_id = self._setup(app, client)
        # S1 记录变更：判断失效；S2 不变。
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,301,否\nS2,0,100,200,否\n")
        state = _order_state(app, order_id)
        by = {r["item"]["sample_no"]: r["item"] for r in state["items"]}
        assert by["S1"]["invalidated"] == 1
        assert "变更" in by["S1"]["invalid_reason"]
        assert by["S2"]["invalidated"] == 0

        with app.app_context():
            from app.recheck import pending_rechecks
            pending = pending_rechecks(hole_id)
        assert [p["sample_no"] for p in pending] == ["S2"]

        # 改回原值也不恢复。
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\nS2,0,100,200,否\n")
        state = _order_state(app, order_id)
        by = {r["item"]["sample_no"]: r["item"] for r in state["items"]}
        assert by["S1"]["invalidated"] == 1
        with app.app_context():
            from app.recheck import pending_rechecks
            assert [p["sample_no"] for p in pending_rechecks(hole_id)] == ["S2"]

        # 仍可查询该单上的回报。
        assert len(state["selected_report"]) > 0
        assert {r["item"]["sample_no"]: (r["diff_ppm"], r["over_limit"])
                for r in state["items"]}["S1"] == (100, True)

    def test_disappeared_sample_invalidates(self, app, client):
        hole_id, order_id = self._setup(app, client)
        _upload_assay(client, hole_id, CSV_HEADER + "S2,0,100,200,否\nS3,0,100,50,否\n")
        state = _order_state(app, order_id)
        by = {r["item"]["sample_no"]: r["item"] for r in state["items"]}
        assert by["S1"]["invalidated"] == 1 and "消失" in by["S1"]["invalid_reason"]
        assert by["S2"]["invalidated"] == 0

    def test_recompute_same_records_does_not_invalidate(self, app, client):
        hole_id, order_id = self._setup(app, client)
        client.post(f"/holes/{hole_id}/recompute", follow_redirects=True)
        state = _order_state(app, order_id)
        assert all(not r["item"]["invalidated"] for r in state["items"])


class TestPendingList:
    def test_pending_aggregates_triggers_and_updates(self, app, client):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 100"},
                    follow_redirects=True)
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\nS2,0,100,200,否\n")
        _open_order(client, hole_id, ["S1"], 10)   # 单 1
        _open_order(client, hole_id, ["S1"], 20)   # 单 2
        with app.app_context():
            from app.db import get_db
            orders = get_db().execute(
                "SELECT id, seq FROM recheck_order ORDER BY seq").fetchall()
        ids = {r["seq"]: r["id"] for r in orders}

        # 回报前：无待复验。
        body = client.get(f"/holes/{hole_id}").get_data(as_text=True)
        assert "当前无待复验样号" in body

        for seq in (1, 2):
            codes = [c["code"] for c in _order_state(app, ids[seq])["items"][0]["codes"]]
            _upload_report(client, ids[seq],
                           [(codes[0], 100), (codes[1], 100 + 15 * seq)])

        # 单1 差15 > 10 超差；单2 差30 > 20 超差 => S1 两名触发单都列出。
        body = client.get(f"/holes/{hole_id}").get_data(as_text=True)
        assert "当前待复验样号" in body
        assert "#1" in body and "#2" in body

        with app.app_context():
            from app.recheck import pending_rechecks
            pending = pending_rechecks(hole_id)
        assert len(pending) == 1 and pending[0]["sample_no"] == "S1"
        assert [t["order_seq"] for t in pending[0]["triggers"]] == [1, 2]

        # 单2 整份订正为限内 => 仅单1 仍触发。
        codes2 = [c["code"] for c in _order_state(app, ids[2])["items"][0]["codes"]]
        _upload_report(client, ids[2], [(codes2[0], 100), (codes2[1], 105)])
        with app.app_context():
            from app.recheck import pending_rechecks
            pending = pending_rechecks(hole_id)
        assert [t["order_seq"] for t in pending[0]["triggers"]] == [1]

        # 单1 订正为限内 => 名单清空。
        codes1 = [c["code"] for c in _order_state(app, ids[1])["items"][0]["codes"]]
        _upload_report(client, ids[1], [(codes1[0], 100), (codes1[1], 100)])
        with app.app_context():
            from app.recheck import pending_rechecks
            assert pending_rechecks(hole_id) == []

    def test_pending_uses_effective_report_not_superseded(self, app, client):
        hole_id = _make_hole(app, client)
        client.post(f"/holes/{hole_id}/segments", data={"segments_text": "0 100"},
                    follow_redirects=True)
        _upload_assay(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")
        _open_order(client, hole_id, ["S1"], 10)
        with app.app_context():
            from app.db import get_db
            order_id = get_db().execute("SELECT id FROM recheck_order").fetchone()["id"]
        codes = [c["code"] for c in _order_state(app, order_id)["items"][0]["codes"]]
        _upload_report(client, order_id, [(codes[0], 100), (codes[1], 200)])  # 超差
        _upload_report(client, order_id, [(codes[0], 100), (codes[1], 100)])  # 订正
        with app.app_context():
            from app.recheck import pending_rechecks
            assert pending_rechecks(hole_id) == []
