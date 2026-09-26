"""全量答案逻辑审计：不调 LLM，纯规则从逻辑层面筛可疑答案。

规则（启发式）：
 R1 题型-答案类型不匹配：问"多少/数值"答案非数字；问"哪些/包括"答案单值；
    问"是否"答案非是否
 R2 复合题元素数不匹配："A和B/A、B、C"子问数 vs 答案元素数
 R3 数值合理性：百分比题答案>100或<0；计数题答案>该文件最大表行数
 R4 可疑内容：[MASK]/[UNSURE/直算降级(sandbox_fail_direct)/回显题干关键词
 R5 重复值：多元素答案全同（取错格信号）
 R6 格式不匹配：json_array 答案单值；string 答案是数组
输出：可疑清单按命中规则数排序。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.config import get_settings  # noqa: E402
from table_qa.indexing.vector_store import TableVectorStore  # noqa: E402

NUM_Q = re.compile(r"多少|数值|金额|余额|比例|百分比|增长率|相差|合计|计数|几个|几项")
LIST_Q = re.compile(r"哪些|包括|列出|组成|都有")
BOOL_Q = re.compile(r"是否|有没有|是不是")
AND_ITEMS = re.compile(r"[、和]|和上期|与上期|分别")

MASK_PAT = re.compile(r"\[MASK\]|\[UNSURE|无法读取|数据缺失")


def main() -> None:
    res_path = ROOT / "output" / "submission.xlsx"
    if not res_path.exists():
        res_path = ROOT / "output" / "result.xlsx"   # 旧文件名兼容
    res = pd.read_excel(res_path, keep_default_na=False)
    dbg = pd.read_excel(ROOT / "output" / "result_debug.xlsx",
                        keep_default_na=False)
    qdf = pd.read_excel(ROOT / "data" / "tests.xlsx")
    dbg["key"] = dbg["id"].astype(str)
    dmap = dbg.set_index("key")
    qdf["key"] = qdf["id"].astype(str)
    qmap = {r["key"]: r for _, r in qdf.iterrows()}

    store = TableVectorStore(get_settings())
    stats = store.stats()
    by_file = stats.get("by_file", {})

    def parse_answer(ans: str):
        s = ans.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                v = json.loads(s)
                return v if isinstance(v, list) else [v]
            except json.JSONDecodeError:
                pass
        return [s]

    rows_out = []
    for r in res.itertuples():
        qid = str(r.id)
        ans = str(r.answer).strip()
        q = qmap.get(qid)
        d = dmap.loc[qid] if qid in dmap.index else None
        if q is None:
            continue
        question = str(q["question"])
        fmt = str(q["answer_format"])
        events = str(d["events"]) if d is not None else ""
        items = parse_answer(ans)
        flags: list[str] = []

        # R4 可疑内容
        if MASK_PAT.search(ans):
            flags.append("R4可疑内容")
        if "sandbox_fail_direct" in events:
            flags.append("R4直算降级")
        # R4b 回显题干：答案项与题干中某词完全相同且非数字
        for it in items:
            its = str(it).strip()
            if (its and not its.replace(",", "").replace(".", "").replace("-", "")
                    .isdigit() and its in question and len(its) >= 4):
                flags.append("R4回显题干")
                break

        # R1 题型-答案类型
        first = str(items[0]) if items else ""
        is_num = bool(re.fullmatch(
            r"-?[\d,]+\.?\d*%?", first.replace("¥", "").replace("$", "")
            .replace("€", "").strip()))
        if NUM_Q.search(question) and not is_num and not MASK_PAT.search(ans):
            flags.append("R1问数值答文本")
        if LIST_Q.search(question) and len(items) == 1 and is_num:
            flags.append("R1问哪些答单值")
        if BOOL_Q.search(question) and first not in ("是", "否", "Yes", "No",
                                                     "yes", "no", "TRUE",
                                                     "FALSE"):
            if not MASK_PAT.search(ans):
                flags.append("R1问是否答其他")

        # R2 复合题元素数
        if fmt == "json_array" and ans.startswith("["):
            n_ask = len(re.findall(r"[、，,]", question.split("：")[-1])) + 1 \
                if ("：" in question or ":" in question) else 0
            if n_ask >= 2 and len(items) not in (n_ask,):
                flags.append(f"R2元素数{len(items)}≠子问{n_ask}")

        # R3 数值合理性
        if is_num:
            try:
                num = float(first.replace(",", "").rstrip("%"))
                if ("比例" in question or "百分比" in question or
                        "percent" in question.lower()) and (num > 100 or num < 0):
                    flags.append("R3百分比越界")
                if re.search(r"几个|几项|多少个|数量", question):
                    f = str(q["file_name"])
                    max_rows = max((by_file.get(f, 0), 60))
                    if num > max_rows:
                        flags.append("R3计数超表行")
            except ValueError:
                pass

        # R5 重复值
        if len(items) >= 2 and len({str(i) for i in items}) == 1 \
                and items[0] not in ("", "是", "否"):
            flags.append("R5全同值")

        # R6 格式
        if fmt == "json_array" and not ans.startswith("["):
            flags.append("R6应数组实单值")
        if fmt == "string" and ans.startswith("[") and len(items) > 1:
            flags.append("R6应单值实数组")

        if flags:
            rows_out.append((qid, len(flags), "；".join(flags), ans[:44],
                             question[:38]))

    rows_out.sort(key=lambda x: -x[1])
    print(f"可疑题 {len(rows_out)} / {len(res)}：\n")
    for qid, n, fl, ans, qs in rows_out[:60]:
        print(f"Q{qid} [{fl}] {ans}  ← {qs}")
    if len(rows_out) > 60:
        print(f"... 另有 {len(rows_out)-60} 题略（多为单 R4 直算降级）")


if __name__ == "__main__":
    main()
