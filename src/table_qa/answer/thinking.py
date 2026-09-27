"""thinking 题：三级降级链计算。

主路径：结构化 op（LLM 输出 op JSON → 确定性解释器执行）——JSON 遵从率
远高于可运行代码，白名单/语法/属性错误整类消失（qid=14/15/17/19 归纳）。
降级1：沙箱 pandas 代码（op 表达不了的复杂逻辑）。
降级2：LLM 直算（无佐证，置信度 low）。
防护：max_tokens 截断空响应/空代码/None 结果一律视为失败走降级。
"""

from __future__ import annotations

import json
import re

import numpy as np

from loguru import logger

from table_qa.answer.ops import execute_op, parse_op
from table_qa.answer.sandbox import SandboxViolation, run_sandbox
from table_qa.llm_client import LLMError, get_llm_hub
from table_qa.prompts import get_prompt_manager
from table_qa.schema import NormalizedTable, Question
from table_qa.tables.dataframe import grid_to_dataframe

# 精度问句：字符类必须含"两"（U+4E24）——"保留两位小数"的"两"不在
# 一二三四五六七八九十 里，此前静默失配致 precision=None（qid=848 实测）
_PRECISION_PAT = re.compile(r"保留\s*([一两二三四五六七八九十\d]+)\s*位小数")
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
# 计数问句：subitems 等枚举 op 返回名单列表，但题目问的是"几项/多少种"，
# fmt=number 下列表会被格式化成 0（qid=10/12 实测）→ 取 len
_COUNTING_Q = re.compile(r"有几项|有几条|有多少|多少种|多少个|多少条|个数|项数|一共有几")


def _count_if_counting(question: Question, result):
    """计数问句 + number 题 + op 返回名单列表 → 以长度作答。"""
    if (isinstance(result, list) and question.answer_format == "number"
            and _COUNTING_Q.search(question.question)):
        logger.info("计数问句取名单长度 qid={} len={}", question.id, len(result))
        return len(result)
    return result


def _build_df(table: NormalizedTable):
    df = grid_to_dataframe(table.grid)
    if df.empty:
        raise ValueError("表格为空，无法构造 DataFrame")
    return df


def _render_ctx(question: Question, df, template: str, page: int = 0) -> str:
    pm = get_prompt_manager()
    head = df.head(3).to_string(max_colwidth=30)
    rows = [str(v) for v in df.iloc[:, 0].tolist()]
    return pm.render(template, columns=list(map(str, df.columns)),
                     head=head, rows=rows, question=question.question,
                     page=page)


def _gen_op(question: Question, df, page: int) -> dict:
    prompt = _render_ctx(question, df, "thinking_op", page=page)
    raw = get_llm_hub().chat(prompt, phase="thinking_op", question_id=question.id)
    if not raw.strip():
        raise LLMError(f"op 生成为空（疑似截断，qid={question.id}）")
    return parse_op(raw)


def _gen_code(question: Question, df, page: int) -> str:
    prompt = _render_ctx(question, df, "thinking_code", page=page)
    raw = get_llm_hub().chat(prompt, phase="thinking_code", question_id=question.id)
    code = _strip_fence(raw)
    if not code.strip():
        raise LLMError("代码生成为空（疑似 max_tokens 截断，qid="
                       f"{question.id}，raw_len={len(raw)}）")
    return code


def _strip_fence(raw: str) -> str:
    return re.sub(r"```(?:python)?|```", "", raw).strip()


def _strip_float_noise(value):
    """浮点二进制尾差清理：按 12 位有效数字规整，财务精度（≤2 位小数）无损。

    numpy 标量同转 Python 原生类型：沙箱 pandas 产物 np.int64 非 int 子类，
    穿透会使 AnswerRecord/JSON 序列化崩溃（qid=198）。
    """
    if isinstance(value, float):
        return float(f"{value:.12g}")
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(f"{float(value):.12g}")
    if isinstance(value, list):
        return [_strip_float_noise(v) for v in value]
    return value


def _invalid_result(result) -> bool:
    """无效结果判定：None 或 NaN（qid=48：沙箱返回 float('nan') 穿透 None 检查）。"""
    if result is None:
        return True
    if isinstance(result, float) and result != result:
        return True
    if isinstance(result, list) and not result:      # 空列表（qid=22：subitems
        return True                                  # 选错 op 时假空，降级再判）
    return False


def answer_thinking(question: Question, table: NormalizedTable) -> dict:
    """thinking 答题。返回 {value, code, exec_ok, precision}。

    value 为原始数值（格式化交给 formatter）；precision 为题干小数位要求。
    code 字段：op 路径存 op JSON，沙箱路径存 python 源码（trace 可区分）。
    """
    m = (_PRECISION_PAT.search(question.question)
         or (_PRECISION_PAT.search(question.format_note) if question.format_note else None))
    precision = _CN_NUM.get(m.group(1)) if m else (int(m.group(1)) if m else None)

    df = _build_df(table)

    # ---- 主路径：结构化 op ----
    try:
        op = _gen_op(question, df, table.page)
        result = execute_op(op, df)
        if _invalid_result(result):
            raise ValueError(f"op 结果不可信: {result!r}")
        result = _count_if_counting(question, result)
        logger.info("op 主路径成功 qid={} op={}", question.id,
                    json.dumps(op, ensure_ascii=False)[:120])
        return {"value": _strip_float_noise(result),
                "code": json.dumps(op, ensure_ascii=False),
                "exec_ok": True, "precision": precision}
    except (LLMError, ValueError, KeyError, json.JSONDecodeError,
            TypeError, ArithmeticError) as e:
        logger.warning("op 主路径失败，降级沙箱 qid={}: {}", question.id, e)

    # ---- 降级1：沙箱代码 ----
    code = ""
    for attempt in (1, 2):
        try:
            code = (_gen_code(question, df, table.page)
                    if attempt == 1 or not code else code)
        except LLMError as e:
            logger.warning("代码生成失败(第{}次) qid={}: {}", attempt, question.id, e)
            continue                      # 重试生成；两轮皆空走直算
        try:
            result = run_sandbox(code, df)
            # None/NaN 视为失败（qid=14 空代码静默通过 / qid=48 nan 穿透）
            if result is None or (isinstance(result, float) and result != result):
                raise RuntimeError("沙箱返回 None/NaN（空代码或未产出 result）")
            # 空列表不再直接采信（qid=42：换行把类别拆成两行，代码扫描为 []，
            # 但表内确有分类）——降级 thinking_direct 终审；真无子项时 LLM 看
            # 全表同样会给 []（qid=19 语义不变）
            if isinstance(result, list) and not result:
                raise RuntimeError("沙箱枚举结果为空，降级直算终审")
            return {"value": _strip_float_noise(result), "code": code,
                    "exec_ok": True, "precision": precision}
        except (RuntimeError, SandboxViolation, ValueError) as e:
            logger.warning("沙箱执行失败(第{}次) qid={}: {}", attempt, question.id, e)
            code = ""                     # 下轮强制重新生成

    # ---- 降级2：LLM 直算（无代码佐证）----
    logger.warning("op/沙箱均失败，LLM 直算降级 qid={}", question.id)
    raw = get_llm_hub().chat(
        f"表格所在页码：{table.page}\n"
        f"表格列名：{list(map(str, df.columns))}\n"
        f"表格内容（前40行）：\n{df.head(40).to_string(max_colwidth=40)}\n\n"
        f"问题：{question.question}\n"
        f"要求：数值计算给出最终数值；列举/枚举类问题（如'包括哪几部分''有哪些'）"
        f"按表格内容列出各项，输出 JSON 数组（如 [\"a\",\"b\"]）；"
        f"枚举分类时，带'其中：'前缀的是下级子项、'合计/小计/总计'是汇总行，"
        f"均不计入分类（qid=42 实测混入子项）；"
        f"复合题（一题多问）按问序逐项作答输出 JSON 数组；只输出答案本身。",
        phase="thinking_direct", question_id=question.id)
    return {"value": _parse_direct(raw, question.question), "code": code,
            "exec_ok": False, "precision": precision}


def _parse_direct(raw: str, question_text: str = ""):
    """直算响应解析：数值 → number；JSON 数组 → list；其余保留文本。

    复合题（一题多问）未按 JSON 输出时按分隔符拆分（qid=128 "未知, 未知, ..."、
    qid=318 "79、吴沙、无、3"、qid=138 换行拼接），否则多问答案粘连为单字符串。
    """
    s = raw.strip()
    try:
        value = float(s.replace(",", ""))
        if value.is_integer() and "." not in s:
            value = int(value)
        return value
    except ValueError:
        pass
    try:
        return json.loads(s)              # 枚举题 JSON 数组
    except (json.JSONDecodeError, ValueError):
        pass
    # 复合题文本兜底：拆分多问粘连（qid=128 "未知, 未知, ..."、qid=318
    # "79、吴沙、无、3"、qid=138 换行拼接）；守卫（qid=338 致歉长句防误拆）
    # 与 replay_format.py 共用 split_compound_text 单一实现
    from table_qa.answer.formatter import split_compound_text
    parts = split_compound_text(s, question_text)
    return parts if parts is not None else s
