"""结构化意图（op）确定性解释器：thinking 题计算主路径。

设计（qid=14/15/17/19 系列教训的归纳）：
- LLM 生成任意 pandas 代码失败率高（白名单外函数/属性误用/类型比较），
  而输出 JSON 遵从率高——把"计算"约束成 6 个 op，解释器确定性执行；
- 行列定位用「精确匹配优先，包含匹配兜底」，容忍 LLM 关键词少字；
- 值经 _strip_float_noise 规整（大数运算二进制尾差）。
降级链：op 失败 → 沙箱代码 → LLM 直算（见 thinking.py）。
"""

from __future__ import annotations

import json
import re
from typing import Any

_OPS = ("cell", "cells", "diff", "sum", "avg", "count", "subitems", "nonzero",
        "ratio_sum", "const", "seq")

# 表格区块边界（层级）：中文序号(3) > 括号序号(2) > 冒号分组(1)。
# 利润表"一、营业总收入"、变动表"（一）综合收益总额"、资产负债表"非流动负债："。
_CN_SEQ = re.compile(r"^[一二三四五六七八九十]+、")
_PAREN_SEQ = re.compile(r"^[（(][一二三四五六七八九十]+[）)]")
_COLON_GROUP = re.compile(r"[:：]\s*$")
_TOTAL_SUFFIX = re.compile(r"(合计|总计)\s*$")
_CALC_PREFIX = re.compile(r"^[加减][:：]")   # "加：其他收益"/"减：营业成本" 一级计算项


def _boundary_level(name: str) -> int:
    """行名的区块边界层级：0=普通行。冒号分组的'其中：'不算边界（是子项）。"""
    if _CN_SEQ.match(name):
        return 3
    if _PAREN_SEQ.match(name):
        return 2
    if _COLON_GROUP.search(name) and not name.startswith("其中"):
        return 1
    return 0


def _find_row(df, needle: str) -> int | None:
    """首列定位：精确匹配优先（取最后一条，合计行惯例在后），包含匹配兜底。"""
    col0 = df.iloc[:, 0].map(lambda v: str(v).strip() if v is not None else "")
    exact = col0[col0 == needle.strip()]
    if len(exact):
        return int(exact.index[-1])
    contains = col0[col0.str.contains(needle.strip(), na=False, regex=False)]
    return int(contains.index[-1]) if len(contains) else None


def _find_col(df, needle: str) -> str | None:
    """列定位：精确优先，包含兜底（列名唯一性由 grid_to_dataframe 去重保证）。"""
    want = needle.strip()
    for c in df.columns:
        if str(c).strip() == want:
            return c
    for c in df.columns:
        if want in str(c):
            return c
    return None


def _count_match(cell: str, needle: str) -> bool:
    """计数匹配三级：精确 → 包含 → 反向包含（qid=859：题干"红色"vs
    表内"红"——VLM 常合并/丢失单字，反向包含兜底）。"""
    if not cell or not needle:
        return False
    return cell == needle or needle in cell or cell in needle


def _is_empty(v) -> bool:
    """空值判定：None/空串/NaN（float 自不等）/字符串'nan'（pandas 转换产物）。"""
    if v is None or v == "":
        return True
    if isinstance(v, float) and v != v:
        return True
    return str(v).strip().lower() == "nan"


def _cell(df, ref: dict) -> Any:
    row = _find_row(df, str(ref.get("row", "")))
    col = _find_col(df, str(ref.get("col", "")))
    if row is None or col is None:
        raise KeyError(f"定位失败: row={ref.get('row')!r} col={ref.get('col')!r}")
    v = df.at[row, col]
    if not _is_empty(v):
        return v
    # 合并格摘要行兜底（qid=388）：视觉上跨列合并的值常被 VLM 放到相邻列
    #（039.png R-squared 值落 Bootstrap SE 列、Coefficient 列空，三级降级
    # 全撞同一空格）。取该行最靠前的非空数值（多模型列组时对应第一组）。
    row_vals = [x for x in df.iloc[row].tolist()
                if not _is_empty(x) and not isinstance(x, str)]
    if row_vals:
        return row_vals[0]
    raise ValueError(f"单元格为空: {ref.get('row')} × {ref.get('col')}")


def _strip_float_noise(value):
    if isinstance(value, float):
        return float(f"{value:.12g}")
    if isinstance(value, list):
        return [_strip_float_noise(v) for v in value]
    return value


def _ref_values(df, refs) -> list[float]:
    """refs 格子列表转数值（sum/avg 的跨列聚合输入，qid=827）。"""
    if not refs:
        raise ValueError("缺 refs")
    return [float(_cell(df, r)) for r in refs]


def _op_subitems(df, item: str) -> list[str]:
    """目标行的子项/区块成员（子项区状态机恢复 tab 丢失的层级）。

    原 PDF 用 tab 缩进分层（财务费用 > 其中：利息费用/利息收入），
    识别时缩进丢失全变平级——靠结构线索恢复：
    - '其中：'行开启子项区，区内行延续条件 = 注释编号一致 + 有值性一致
      （利息收入[注41,有值]延续；以摊余成本[无注,无值]终止联营子项区）
    - '加：/减：'前缀、区块边界、合计行 = 回归一级
    两种输出模式：
    - 区块模式（目标是序号/分组等边界行）：收集一级成员，子项区折叠
      （Q19 营业总收入 → 14 个一级项，不含财务费用的细分）
    - 科目模式（目标是普通科目）：收集其子项区的行（财务费用 →
      [利息费用, 利息收入]；应付债券 → [优先股, 永续债]）
    """
    row = _find_row(df, item)
    if row is None:
        raise KeyError(f"定位失败: item={item!r}")
    first_text = str(df.iloc[row, 0]).strip()
    level = _boundary_level(first_text)
    section_mode = level >= 1
    stop_level = max(level, 1)   # 科目模式(level 0)遇任何边界即停；
    out: list[str] = []          # 序号区块(3)内的括号子区块(2)不截断
    zone = None          # 子项区状态 (note, has_value)；None=一级区

    for _, r in df.iloc[row + 1:].iterrows():
        name = str(r.iloc[0]).strip() if not _is_empty(r.iloc[0]) else ""
        if not name:
            continue
        note = str(r.iloc[1]).strip() if not _is_empty(r.iloc[1]) else None
        has_value = any(not _is_empty(v) for v in r.iloc[1:])

        # 全局终止：不低于目标层级的边界 / 合计行
        if _boundary_level(name) >= stop_level or _TOTAL_SUFFIX.search(name):
            break

        if name.startswith("其中："):
            zone = (note, has_value)                    # 开启子项区
            if not section_mode:
                out.append(name[len("其中："):].strip())  # 科目模式收集（去前缀）
            continue                                    # 区块模式折叠

        if zone is not None:
            # 子项区延续判定：注释一致 + 有值性一致；'加：/减：'回归一级
            if (_CALC_PREFIX.match(name) or note != zone[0] or
                    has_value != zone[1]):
                zone = None                              # 区终止，该行按一级
            else:
                if not section_mode:
                    out.append(name)                     # 科目模式收集子项
                continue                                # 区块模式折叠子项

        if section_mode:
            out.append(name)                             # 一级成员
        elif not has_value:
            out.append(name)                             # 裸行子项（缩进丢失）
        else:
            break                                       # 有值主科目边界
    return out


def execute_op(op: dict, df):
    """执行单个 op，返回原生类型值；失败抛异常（由调用方降级）。"""
    kind = op.get("op")
    if kind not in _OPS:
        raise ValueError(f"未知 op: {kind}")
    if kind == "const":
        # 常量答案（Q148：页码来自上下文、存在性判断"是/否"基于行名列表）
        v = op.get("value")
        if v is None or v == "":
            raise ValueError("const 缺 value")
        return v
    if kind == "seq":
        # 复合题顺序执行（Q148：多个子问按序各出一值，返回列表）
        subs = op.get("ops") or []
        if not subs:
            raise ValueError("seq 缺 ops")
        return [execute_op(sub, df) for sub in subs]
    if kind == "cell":
        return _strip_float_noise(_cell(df, op))
    if kind == "cells":
        refs = op.get("refs") or []
        if not refs:
            raise ValueError("cells 缺 refs")
        return [_strip_float_noise(_cell(df, r)) for r in refs]
    if kind == "diff":
        return _strip_float_noise(
            float(_cell(df, op["a"])) - float(_cell(df, op["b"])))
    if kind == "sum":
        if op.get("refs"):
            # 跨列格子相加（qid=827："X 的欧洲与亚洲销量合计"= 同行两列）
            return _strip_float_noise(sum(_ref_values(df, op["refs"])))
        col = _find_col(df, str(op.get("col", "")))
        if col is None:
            raise KeyError(f"定位失败: col={op.get('col')!r}")
        rows = op.get("rows")
        if rows:
            idx = [_find_row(df, str(r)) for r in rows]
            if any(i is None for i in idx):
                raise KeyError(f"行定位失败: {op.get('rows')!r}")
            vals = [df.at[i, col] for i in idx]
        else:
            vals = df[col].tolist()
        nums = [float(v) for v in vals
                if not _is_empty(v) and not isinstance(v, str)]
        if not nums:
            raise ValueError("求和列无数值")
        return _strip_float_noise(sum(nums))
    if kind == "avg":
        # 平均（qid=846：无 avg 时"平均值"退化成 cells 只罗列操作数）
        if op.get("refs"):
            vals = _ref_values(df, op["refs"])
        else:
            col = _find_col(df, str(op.get("col", "")))
            if col is None:
                raise KeyError(f"定位失败: col={op.get('col')!r}")
            rows = op.get("rows")
            if rows:
                idx = [_find_row(df, str(r)) for r in rows]
                if any(i is None for i in idx):
                    raise KeyError(f"行定位失败: {op.get('rows')!r}")
                raw = [df.at[i, col] for i in idx]
            else:
                raw = df[col].tolist()
            vals = [float(v) for v in raw
                    if not _is_empty(v) and not isinstance(v, str)]
        if not vals:
            raise ValueError("平均值无数值")
        return _strip_float_noise(sum(vals) / len(vals))
    if kind == "ratio_sum":
        # 加权比（Q48：合计计提比例 = Σ坏账准备 / Σ账面余额，比例列
        # 简单相加是语义错误 50+100=150≠57.93）。返回百分比量纲（与表内一致）。
        rows = op.get("rows") or []
        idx = [_find_row(df, str(r)) for r in rows]
        if any(i is None for i in idx):
            raise KeyError(f"行定位失败: {rows!r}")

        def _colsum(col_name: str) -> float:
            col = _find_col(df, str(col_name))
            if col is None:
                raise KeyError(f"定位失败: col={col_name!r}")
            nums = [float(df.at[i, col]) for i in idx
                    if not _is_empty(df.at[i, col])
                    and not isinstance(df.at[i, col], str)]
            if not nums:
                raise ValueError(f"列无数值: {col_name}")
            return sum(nums)

        den = _colsum(op["den_col"])
        if den == 0:
            raise ZeroDivisionError("分母列合计为 0")
        return round(100.0 * _colsum(op["num_col"]) / den, 2)  # 比例财务惯例2位
    if kind == "count":
        needle = str(op.get("contains", "")).strip()
        col_name = str(op.get("col", "") or "").strip()
        if col_name:
            # 指定列计数（qid=859："有多少红色"在颜色列数匹配格子——
            # 原实现忽略 col 永远搜首列，颜色在非首列时计成 0）
            col = _find_col(df, col_name)
            if col is None:
                raise KeyError(f"定位失败: col={col_name!r}")
            series = df[col]
        else:
            series = df.iloc[:, 0]
        cells = [str(v).strip() for v in series.tolist() if not _is_empty(v)]
        if not needle:
            return int(len(df))
        return sum(1 for v in cells if _count_match(v, needle))
    if kind == "subitems":
        return _op_subitems(df, str(op.get("item", "")))
    if kind == "nonzero":
        # 按值筛选行：返回 col 列有非零数值的首列行名（qid=22"哪些项今年有变动"）
        col = _find_col(df, str(op.get("col", "")))
        if col is None:
            raise KeyError(f"定位失败: col={op.get('col')!r}")
        start = 0
        if op.get("after_row"):        # 区块锚定：仅统计该行之后的行（排除
            anchor = _find_row(df, str(op["after_row"]))  # 上年余额等标题行噪音）
            if anchor is None:
                raise KeyError(f"锚定行定位失败: {op['after_row']!r}")
            start = anchor + 1
        out: list[str] = []
        for _, r in df.iloc[start:].iterrows():
            name = str(r.iloc[0]).strip() if not _is_empty(r.iloc[0]) else ""
            v = r[col]
            if not name or _is_empty(v) or isinstance(v, str):
                continue
            try:
                if abs(float(v)) > 1e-12:
                    out.append(name)
            except (TypeError, ValueError):
                continue
        return out
    raise AssertionError(kind)


def parse_op(raw: str) -> dict:
    """解析 LLM 输出：剥 code fence 后 json.loads，校验是 dict 且含 op。"""
    import re
    s = re.sub(r"^```(?:json|python)?\s*|\s*```$", "", raw.strip()).strip()
    obj = json.loads(s)
    if not isinstance(obj, dict) or "op" not in obj:
        raise ValueError(f"非法 op 结构: {str(obj)[:80]}")
    return obj
