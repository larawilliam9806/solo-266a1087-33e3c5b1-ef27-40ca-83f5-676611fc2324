# -*- coding: utf-8 -*-
"""盲样复验：开单、盲号、回报入库、超差核查与待复验名单。

核心口径：
- 开单时对选中样号固化「正式化验快照」，并生成两个全库唯一盲号。
- 正式化验换交后，若某样号消失或记录（起止/品位/复验）变化，该单对该
  样号的判断永久失效（invalidated=1），改回原值也不恢复。
- 回报整份上传：只收本单盲号且每号恰好一行；每次成功上传生成一版不可变
  回报快照（含逐样核查结果），上一版保留；失败不产生版本，旧回报继续有效。
- 待复验名单由「当前生效回报 + 未失效样号 + 超差」直接查询得到，换交或
  回报入库后天然同步；不逐版扫描化验表。
"""
from __future__ import annotations

import secrets
import sqlite3
from typing import Dict, List, Optional, Sequence

from .db import get_db


class RecheckError(ValueError):
    pass


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混字符 0/O/1/I


def _new_blind_code(conn: sqlite3.Connection) -> str:
    """生成全库唯一盲号（UNIQUE(code) 兜底，冲突重试）。"""
    for _ in range(50):
        code = "B" + "".join(secrets.choice(_ALPHABET) for _ in range(12))
        exists = conn.execute(
            "SELECT 1 FROM blind_code WHERE code = ?", (code,)
        ).fetchone()
        if exists is None:
            return code
    raise RecheckError("盲号生成失败，请重试")


def _validate_max_diff(max_diff_ppm) -> int:
    if isinstance(max_diff_ppm, bool) or not isinstance(max_diff_ppm, int):
        raise RecheckError("允许的最大差值必须是非负整数 ppm")
    if max_diff_ppm < 0:
        raise RecheckError("允许的最大差值必须是非负整数 ppm")
    return max_diff_ppm


def _validate_grade(grade) -> int:
    if isinstance(grade, bool) or not isinstance(grade, int):
        raise RecheckError("品位必须是非负整数 ppm")
    if grade < 0:
        raise RecheckError("品位必须是非负整数 ppm")
    return grade


# ---------------------------------------------------------------- 开单

def create_order(hole_id: int, sample_nos: Sequence[str], max_diff_ppm: int) -> int:
    """为当前生效化验中的若干样号开一张复验单，返回单 id。"""
    conn = get_db()
    hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
    if hole is None:
        raise RecheckError("钻孔不存在")
    max_diff = _validate_max_diff(max_diff_ppm)

    cleaned: List[str] = []
    seen = set()
    for raw in sample_nos:
        sample = (raw or "").strip()
        if not sample:
            continue
        if sample in seen:
            raise RecheckError(f"样号 {sample} 被重复勾选")
        seen.add(sample)
        cleaned.append(sample)
    if not cleaned:
        raise RecheckError("请至少勾选一个当前样号")

    if hole["effective_version_id"] is None:
        raise RecheckError("尚无生效化验，无法开单")
    rows = conn.execute(
        "SELECT * FROM assay_row WHERE version_id = ?", (hole["effective_version_id"],)
    ).fetchall()
    current = {r["sample_no"]: r for r in rows}
    missing = [s for s in cleaned if s not in current]
    if missing:
        raise RecheckError(
            "以下样号不在当前生效化验中：" + "、".join(missing)
        )

    with conn:  # 单事务：单头、明细、盲号一并落库
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM recheck_order WHERE hole_id = ?",
            (hole_id,),
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO recheck_order(hole_id, seq, max_diff_ppm) VALUES (?, ?, ?)",
            (hole_id, seq, max_diff),
        )
        order_id = cur.lastrowid
        for i, sample in enumerate(cleaned, start=1):
            a = current[sample]
            item_cur = conn.execute(
                """
                INSERT INTO recheck_item(
                    order_id, seq, sample_no, start_cm, end_cm, grade_ppm, recheck)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, i, sample, a["start_cm"], a["end_cm"],
                 a["grade_ppm"], a["recheck"]),
            )
            item_id = item_cur.lastrowid
            for slot in (1, 2):
                conn.execute(
                    "INSERT INTO blind_code(order_item_id, slot, code) VALUES (?, ?, ?)",
                    (item_id, slot, _new_blind_code(conn)),
                )
    return order_id


# ---------------------------------------------------------------- 失效

def invalidate_for_new_version(conn: sqlite3.Connection, hole_id: int) -> int:
    """在新化验版本写入后、提交前调用。

    对比每个未失效复验明细的开单快照与**新生效版本**：样号消失或记录
    变化即永久失效。因此「变化后改回原值」也不会恢复。
    返回本次新失效的行数。
    """
    hole = conn.execute(
        "SELECT effective_version_id FROM hole WHERE id = ?", (hole_id,)
    ).fetchone()
    if hole is None or hole["effective_version_id"] is None:
        return 0
    rows = conn.execute(
        "SELECT sample_no, start_cm, end_cm, grade_ppm, recheck "
        "FROM assay_row WHERE version_id = ?",
        (hole["effective_version_id"],),
    ).fetchall()
    current = {
        r["sample_no"]: (r["start_cm"], r["end_cm"], r["grade_ppm"], r["recheck"])
        for r in rows
    }
    items = conn.execute(
        """
        SELECT i.id, i.sample_no, i.start_cm, i.end_cm, i.grade_ppm, i.recheck
        FROM recheck_item i
        JOIN recheck_order o ON o.id = i.order_id
        WHERE o.hole_id = ? AND i.invalidated = 0
        """,
        (hole_id,),
    ).fetchall()
    changed = 0
    for item in items:
        now = current.get(item["sample_no"])
        if now is None:
            reason = "样号已从正式化验中消失"
        elif now != (item["start_cm"], item["end_cm"],
                     item["grade_ppm"], item["recheck"]):
            reason = "样号化验记录已变更（起止/品位/复验）"
        else:
            continue
        conn.execute(
            "UPDATE recheck_item SET invalidated = 1, "
            "invalidated_at = datetime('now'), invalid_reason = ? "
            "WHERE id = ?",
            (reason, item["id"]),
        )
        changed += 1
    return changed


# ---------------------------------------------------------------- 查询

def list_orders(hole_id: int) -> List[dict]:
    conn = get_db()
    orders = conn.execute(
        "SELECT * FROM recheck_order WHERE hole_id = ? ORDER BY seq DESC", (hole_id,)
    ).fetchall()
    out = []
    for o in orders:
        item_count = conn.execute(
            "SELECT COUNT(*) c FROM recheck_item WHERE order_id = ?", (o["id"],)
        ).fetchone()["c"]
        invalid_count = conn.execute(
            "SELECT COUNT(*) c FROM recheck_item WHERE order_id = ? AND invalidated = 1",
            (o["id"],),
        ).fetchone()["c"]
        report = conn.execute(
            "SELECT * FROM recheck_report WHERE order_id = ? AND status = 'effective'",
            (o["id"],),
        ).fetchone()
        over_count = 0
        if report is not None:
            over_count = conn.execute(
                """
                SELECT COUNT(*) c FROM recheck_result r
                JOIN recheck_item i ON i.id = r.order_item_id
                WHERE r.report_id = ? AND r.over_limit = 1 AND i.invalidated = 0
                """,
                (report["id"],),
            ).fetchone()["c"]
        out.append({
            "order": o,
            "item_count": item_count,
            "invalidated_count": invalid_count,
            "report": report,
            "over_count": over_count,
        })
    return out


def get_order(order_id: int, report_id: Optional[int] = None) -> Optional[dict]:
    """单详情；report_id 缺省展示当前生效回报。"""
    conn = get_db()
    order = conn.execute(
        "SELECT * FROM recheck_order WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        return None
    hole = conn.execute(
        "SELECT * FROM hole WHERE id = ?", (order["hole_id"],)
    ).fetchone()
    item_rows = conn.execute(
        "SELECT * FROM recheck_item WHERE order_id = ? ORDER BY seq", (order_id,)
    ).fetchall()
    code_rows = conn.execute(
        """
        SELECT b.* FROM blind_code b
        JOIN recheck_item i ON i.id = b.order_item_id
        WHERE i.order_id = ? ORDER BY b.order_item_id, b.slot
        """,
        (order_id,),
    ).fetchall()
    codes_by_item: Dict[int, List[sqlite3.Row]] = {}
    for c in code_rows:
        codes_by_item.setdefault(c["order_item_id"], []).append(c)

    reports = conn.execute(
        "SELECT * FROM recheck_report WHERE order_id = ? ORDER BY seq DESC", (order_id,)
    ).fetchall()
    selected = None
    if report_id is not None:
        selected = next((r for r in reports if r["id"] == report_id), None)
        if selected is None:
            raise RecheckError("该回报不属于本单")
    else:
        selected = next((r for r in reports if r["status"] == "effective"), None)

    readings: Dict[int, int] = {}
    results: Dict[int, sqlite3.Row] = {}
    if selected is not None:
        readings = {
            r["blind_code_id"]: r["grade_ppm"]
            for r in conn.execute(
                "SELECT * FROM recheck_reading WHERE report_id = ?", (selected["id"],)
            ).fetchall()
        }
        results = {
            r["order_item_id"]: r
            for r in conn.execute(
                "SELECT * FROM recheck_result WHERE report_id = ?", (selected["id"],)
            ).fetchall()
        }

    items = []
    for it in item_rows:
        codes = codes_by_item.get(it["id"], [])
        rows = []
        for c in codes:
            rows.append({"code": c["code"], "grade": readings.get(c["id"])})
        res = results.get(it["id"])
        items.append({
            "item": it,
            "codes": rows,
            "diff_ppm": res["diff_ppm"] if res else None,
            "over_limit": bool(res["over_limit"]) if res else False,
        })
    return {
        "order": order,
        "hole": hole,
        "items": items,
        "reports": reports,
        "selected_report": selected,
        "selected_report_id": selected["id"] if selected else None,
    }


def pending_rechecks(hole_id: int) -> List[dict]:
    """当前待复验样号及其全部触发单号。

    只查当前生效回报、未失效明细和超差结果，不逐版读取化验表。
    """
    rows = get_db().execute(
        """
        SELECT i.sample_no, o.id AS order_id, o.seq AS order_seq, r.diff_ppm
        FROM recheck_result r
        JOIN recheck_report rp ON rp.id = r.report_id AND rp.status = 'effective'
        JOIN recheck_item i ON i.id = r.order_item_id AND i.invalidated = 0
        JOIN recheck_order o ON o.id = i.order_id
        WHERE o.hole_id = ? AND r.over_limit = 1
        ORDER BY i.sample_no, o.seq
        """,
        (hole_id,),
    ).fetchall()
    grouped: Dict[str, dict] = {}
    for row in rows:
        entry = grouped.setdefault(
            row["sample_no"], {"sample_no": row["sample_no"], "triggers": []}
        )
        entry["triggers"].append({
            "order_id": row["order_id"],
            "order_seq": row["order_seq"],
            "diff_ppm": row["diff_ppm"],
        })
    return list(grouped.values())


# ---------------------------------------------------------------- 回报入库

def upload_report(order_id: int, readings: Dict[str, int]) -> dict:
    """整份回报入库。

    readings: 盲号 -> 品位（解析阶段已去重）。要求恰好覆盖本单全部盲号。
    成功生成新版本并原子替换生效回报；任何失败整体回滚，旧回报继续有效。
    """
    conn = get_db()
    order = conn.execute(
        "SELECT * FROM recheck_order WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        raise RecheckError("复验单不存在")

    code_rows = conn.execute(
        """
        SELECT b.id, b.code, i.id AS item_id
        FROM blind_code b
        JOIN recheck_item i ON i.id = b.order_item_id
        WHERE i.order_id = ?
        ORDER BY b.id
        """,
        (order_id,),
    ).fetchall()
    by_code = {r["code"]: r for r in code_rows}
    unknown = [c for c in readings if c not in by_code]
    if unknown:
        raise RecheckError(
            "以下盲号不属于本单：" + "、".join(sorted(unknown))
        )
    missing = [r["code"] for r in code_rows if r["code"] not in readings]
    if missing:
        raise RecheckError(
            "回报缺少本单盲号（每号必须恰好一行）：" + "、".join(missing)
        )
    clean: Dict[str, int] = {}
    for code, grade in readings.items():
        clean[code] = _validate_grade(grade)

    codes_by_item: Dict[int, List[int]] = {}
    for r in code_rows:
        codes_by_item.setdefault(r["item_id"], []).append(r["id"])
    for item_id, ids in codes_by_item.items():
        if len(ids) != 2:  # pragma: no cover - 开单保证两枚
            raise RecheckError(f"复验明细 {item_id} 盲号数量异常")

    with conn:  # 单事务：整份替换，失败回滚
        # 先降级旧生效回报，满足「每单最多一版 effective」的唯一索引。
        conn.execute(
            "UPDATE recheck_report SET status = 'superseded' "
            "WHERE order_id = ? AND status = 'effective'",
            (order_id,),
        )
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM recheck_report WHERE order_id = ?",
            (order_id,),
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO recheck_report(order_id, seq, status) VALUES (?, ?, 'effective')",
            (order_id, seq),
        )
        report_id = cur.lastrowid

        grade_by_code_id: Dict[int, int] = {}
        for code, grade in clean.items():
            code_id = by_code[code]["id"]
            grade_by_code_id[code_id] = grade
            conn.execute(
                "INSERT INTO recheck_reading(report_id, blind_code_id, grade_ppm) "
                "VALUES (?, ?, ?)",
                (report_id, code_id, grade),
            )

        over = 0
        for item_id, ids in codes_by_item.items():
            a, b = grade_by_code_id[ids[0]], grade_by_code_id[ids[1]]
            diff = abs(a - b)
            is_over = 1 if diff > order["max_diff_ppm"] else 0
            over += is_over
            conn.execute(
                "INSERT INTO recheck_result(report_id, order_item_id, diff_ppm, over_limit) "
                "VALUES (?, ?, ?, ?)",
                (report_id, item_id, diff, is_over),
            )

        invalidated = conn.execute(
            "SELECT COUNT(*) c FROM recheck_item WHERE order_id = ? AND invalidated = 1",
            (order_id,),
        ).fetchone()["c"]

    return {
        "report_id": report_id,
        "report_seq": seq,
        "item_count": len(codes_by_item),
        "over_limit": over,
        "invalidated": invalidated,
    }
