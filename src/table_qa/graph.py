"""LangGraph 编排：ingestion 图（文件→识别→入库）与 answering 图（题目→检索→答题）。

设计要点：
- 确定性逻辑（引擎/沙箱/转换）保持原生 Python 函数，LangGraph 只管节点串联与状态传递
- 每个节点向 state 写入 TraceStep（Web 调试界面按此渲染）与置信度事件
- 置信度：初始 1.0，按事件扣分（幻觉信号/降级路径），下限 0.2
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from loguru import logger
from rapidfuzz import fuzz

from table_qa.answer.extract import answer_extract, value_empty, vision_extract
from table_qa.answer.formatter import format_answer
from table_qa.answer.structure import answer_structure, parse_range
from table_qa.answer.thinking import answer_thinking
from table_qa.cache import TableStore
from table_qa.config import Settings
from table_qa.engines.pdfplumber_engine import PdfplumberEngine
from table_qa.engines.qwen_engine import QwenEngine
from table_qa.indexing.vector_store import TableVectorStore
from table_qa.ingest.categorize import FileProfile
from table_qa.ingest.loader import load_pages
from table_qa.prompts import get_prompt_manager
from table_qa.schema import NormalizedTable, ParsedRange, Question, TraceStep

# 置信度扣分表（事件 → 扣分）
_PENALTY: dict[str, float] = {
    "rag_miss": 0.10,            # RAG 检索未命中
    "table_select_low": 0.10,    # 表格匹配置信度低
    "range_fallback_full": 0.10, # 范围解析失败按整表
    "extract_unverified": 0.20,  # 提取值未在表中回查到（幻觉信号）
    "vision_fallback": 0.10,     # 矩阵取不到值，VLM 看原图直答（无回查，轻度扣分）
    "sandbox_fail_direct": 0.20, # 沙箱失败降级 LLM 直算
    "mask_in_table": 0.10,       # 选中表含不可辨认单元格
    "no_table": 0.40,            # 该文件没识别出任何表
}


class PipelineContext:
    """图构建依赖容器（引擎/存储/配置），节点闭包捕获。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = TableStore(settings)
        self.vector = TableVectorStore(settings)
        self.qwen = QwenEngine()


# ===========================================================================
# Ingestion 图：文件 → 识别 → 缓存/向量入库
# ===========================================================================


class IngestState(TypedDict, total=False):
    profile: FileProfile
    _pages: list           # 内部传递：PageAsset 列表（不参与输出）
    tables: list[NormalizedTable]
    pages_count: int
    error: str | None


def build_ingest_graph(ctx: PipelineContext):
    """构建文件摄取图（线性：load → recognize → persist）。"""

    def load_node(state: IngestState) -> dict:
        pages = load_pages(state["profile"], ctx.settings,
                           ctx.settings.paths.abs_path(ctx.settings.paths.cache_dir) / "pages")
        return {"pages_count": len(pages), "_pages": pages}

    def recognize_node(state: IngestState) -> dict:
        profile = state["profile"]
        pages: list = state.get("_pages", [])  # type: ignore[assignment]
        tables: list[NormalizedTable] = []
        if profile.category in ("A", "B"):
            engine = PdfplumberEngine(profile)
            for p in pages:
                tables.extend(engine.recognize(p))
            # B 类抽表失败（多级表头/无线框）→ VLM 兜底
            if not tables:
                logger.warning("{} 抽表为空，VLM 兜底", profile.file_name)
                for p in pages:
                    tables.extend(ctx.qwen.recognize(p))
        else:
            for p in pages:
                tables.extend(ctx.qwen.recognize(p, table_hint=None, category=profile.category))
        return {"tables": tables}

    def persist_node(state: IngestState) -> dict:
        fp = get_prompt_manager().fingerprint()
        tables = state.get("tables", [])
        if tables:
            ctx.store.save(state["profile"].file_id, tables, fp)
            ctx.vector.upsert(tables)
        else:
            # 0 张表不落缓存：识别失败（额度耗尽/VLM 异常被容错吞掉）曾把
            # 空结果缓存成"正常"，后续 run 永远命中毒缓存（实测 12 个文件）。
            # 跳过缓存让下次 run 自动重试；真正无表的页面多花一次识别可接受。
            logger.warning("识别结果为空，不写缓存（下次运行自动重试）: {}",
                           state["profile"].file_name)
        return {}

    builder = StateGraph(IngestState)
    builder.add_node("load", load_node)
    builder.add_node("recognize", recognize_node)
    builder.add_node("persist", persist_node)
    builder.add_edge(START, "load")
    builder.add_edge("load", "recognize")
    builder.add_edge("recognize", "persist")
    builder.add_edge("persist", END)
    return builder.compile()


# ===========================================================================
# Answering 图：题目 → RAG 检索 → 范围解析 → 选表 → 答题 → 格式化
# ===========================================================================


class AnswerState(TypedDict, total=False):
    question: Question
    profile: FileProfile
    tables: list[NormalizedTable]
    retrieval: list[dict]
    rng: ParsedRange
    rng_method: str
    table: NormalizedTable | None
    raw_result: dict
    precision: int | None
    answer_value: str
    events: Annotated[list[str], operator.add]
    steps: Annotated[list[TraceStep], operator.add]
    marker: int          # usage 记录起点（按题切片）


def build_answer_graph(ctx: PipelineContext):
    """构建答题图（条件路由：structure / extract / thinking）。"""
    s = ctx.settings

    # ------------------------------------------------------------------ 检索
    def retrieve_node(state: AnswerState) -> dict:
        q = state["question"]
        profile = state["profile"]
        step = TraceStep(step="retrieve", title="RAG 检索")
        tables = ctx.store.load(profile.file_id)
        query = q.question + (f" {q.table_hint}" if q.table_hint else "")
        hits = ctx.vector.retrieve(query, file_name=profile.file_name, question_id=q.id)
        step.data = {
            "query": query,
            "hits": [{
                "table_name": h["metadata"].get("table_name"),
                "page": h["metadata"].get("page"),
                "similarity": h["similarity"],
                "rerank_bonus": h.get("rerank_bonus", 0),
                "adjusted_score": h.get("adjusted_score", h["similarity"]),
            } for h in hits],
            "tables_in_store": len(tables),
        }
        events: list[str] = []
        if not tables:
            events.append("no_table")
            step.status = "fail"
        elif not hits:
            events.append("rag_miss")
            step.status = "warn"
        # RAG 命中排序：命中的表排前面（page+表名匹配）
        if hits:
            ranked = sorted(tables, key=lambda t: -_retrieval_rank(t, hits))
            tables = ranked
            step.data["ranking"] = [t.table_name or f"p{t.page}" for t in ranked[:3]]
        return {"tables": tables, "retrieval": hits, "events": events, "steps": [step]}

    # -------------------------------------------------------------- 范围解析
    def range_node(state: AnswerState) -> dict:
        q = state["question"]
        rng, method = parse_range(q)
        step = TraceStep(step="range", title="范围解析", data={
            "method": method,
            "page_range": rng.page_range, "row_range": rng.row_range,
            "col_range": rng.col_range,
        })
        events = ["range_fallback_full"] if method == "fallback_full" else []
        if method == "fallback_full":
            step.status = "warn"
        return {"rng": rng, "rng_method": method, "events": events, "steps": [step]}

    # ------------------------------------------------------------------ 选表
    def select_node(state: AnswerState) -> dict:
        q = state["question"]
        tables = state.get("tables", [])
        rng = state.get("rng") or ParsedRange()
        if not tables:
            return {"table": None,
                    "steps": [TraceStep(step="select", title="表格选择", status="fail",
                                        data={"reason": "无候选表"})]}
        # 碎表过滤：候选需 ≥2 行且有数据行（表头行之外、行名列之外
        # 存在非空单元格）。018.jpg 1x4 表头-only 碎表与 007.pdf p1
        # '1．固定资产情况' 2x6 跨页残头（表头+空行，数据在 p2 续页）
        # 实测曾抢走真数据表 → extract 在空表上必然产出空值
        usable = ([t for t in tables if t.n_rows >= 2 and _has_data_rows(t)]
                  or [t for t in tables if t.n_rows >= 2] or tables)
        best, score = _select_table(usable, q, rng,
                                    hits=state.get("retrieval"))
        step = TraceStep(step="select", title="表格选择", data={
            "candidates": len(tables),
            "selected": best.table_name or f"p{best.page}",
            "score": round(score, 3),
            "rows": best.n_rows, "cols": best.n_cols,
            "engine": best.source_engine,
            "table_html": best.html[:4000],   # 前端预览（排查幻觉用）
        })
        events = []
        if score < 0.3:
            events.append("table_select_low")
            step.status = "warn"
        if any(c.is_masked for row in best.grid for c in row):
            events.append("mask_in_table")
            step.status = "warn"
        return {"table": best, "events": events, "steps": [step]}

    # ------------------------------------------------------------ 三类答题
    def structure_node(state: AnswerState) -> dict:
        q, table, rng = state["question"], state["table"], state.get("rng") or ParsedRange()
        if table is None:
            return _no_table_result()
        result, partial = answer_structure(q, table, rng)
        step = TraceStep(step="answer", title="结构恢复", data={
            "partial": partial, "row_count": result["row_count"],
            "col_count": result["col_count"],
        })
        return {"raw_result": {"kind": "structure", "value": result},
                "steps": [step]}

    def extract_node(state: AnswerState) -> dict:
        q, table = state["question"], state["table"]
        if table is None:
            return _no_table_result()
        result = answer_extract(q, table)
        # 视觉降级：矩阵取不到值（答案在原图不在表格：是否含二维码/
        # 最左侧表头/卡片字段等，qid=484 等 17 题实测）→ VLM 看原图直答
        if value_empty(result.get("value")) and state.get("profile") is not None:
            vision = vision_extract(q, state["profile"], table)
            if vision is not None:
                result = vision
        step = TraceStep(step="answer", title="内容提取", data={
            "value": result["value"], "locate": result["locate"],
            "reason": result["reason"], "verified": result["verified"],
        })
        if result.get("vision"):
            events = ["vision_fallback"]
            step.status = "warn"
        else:
            events = ["extract_unverified"] if not result["verified"] else []
            if events:
                step.status = "warn"
        return {"raw_result": {"kind": "extract", **result}, "events": events,
                "steps": [step]}

    def thinking_node(state: AnswerState) -> dict:
        q, table = state["question"], state["table"]
        # 空表/表头-only 表降级：无数据行可计算（曾在此抛
        # "表格为空，无法构造 DataFrame" 导致 runner_error）
        if table is None or not table.grid or len(table.grid) < 2:
            return _no_table_result()
        result = answer_thinking(q, table)
        step = TraceStep(step="answer", title="推理计算", data={
            "code": result["code"], "exec_ok": result["exec_ok"],
            "value": result["value"], "precision": result["precision"],
        })
        events = ["sandbox_fail_direct"] if not result["exec_ok"] else []
        if events:
            step.status = "warn"
        return {"raw_result": {"kind": "thinking", **result}, "events": events,
                "precision": result["precision"], "steps": [step]}

    # -------------------------------------------------------------- 格式化
    def format_node(state: AnswerState) -> dict:
        q = state["question"]
        raw = state.get("raw_result") or {"kind": "empty", "value": _empty_value(q.answer_format)}
        value = format_answer(raw.get("value"), q.answer_format,
                              state.get("precision"), q)
        # 空值兜底：答案永不为空（schema 合法空框架）；防御非 str 中间值
        if not str(value).strip():
            value = _empty_value(q.answer_format)
        events = list(state.get("events", []))
        if raw.get("kind") == "empty":
            events.append("no_table")
        score = _confidence_score(events)
        step = TraceStep(step="format", title="格式化与置信度", data={
            "answer_format": q.answer_format, "value": value[:200],
            "score": score, "events": events,
        })
        return {"answer_value": value, "events": [], "confidence_score": score,
                "steps": [step]}

    builder = StateGraph(AnswerState)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("range", range_node)
    builder.add_node("select", select_node)
    builder.add_node("structure", structure_node)
    builder.add_node("extract", extract_node)
    builder.add_node("thinking", thinking_node)
    builder.add_node("format", format_node)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "range")
    builder.add_edge("range", "select")
    builder.add_conditional_edges(
        "select", lambda st: st["question"].question_type,
        {"structure": "structure", "extract": "extract", "thinking": "thinking"},
    )
    for n in ("structure", "extract", "thinking"):
        builder.add_edge(n, "format")
    builder.add_edge("format", END)
    return builder.compile()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _retrieval_rank(table: NormalizedTable, hits: list[dict]) -> float:
    """表在 RAG 命中中的排名分（page 与表名同时命中得高分）。"""
    best = 0.0
    for h in hits:
        meta = h["metadata"]
        if meta.get("page") == table.page:
            best = max(best, h["similarity"])
            name = meta.get("table_name", "")
            if name and table.table_name and fuzz.ratio(name, table.table_name) > 70:
                best = max(best, h["similarity"] + 0.2)
    return best


def _has_data_rows(t: NormalizedTable) -> bool:
    """有数据行判定：表头行之外，行名列（col0）之外存在非空单元格。

    007.pdf p1 '1．固定资产情况' 2x6 跨页残头实测：表头 +
    ['一.账面原值','','','','','']——行数够但数据列全空，
    n_rows>=2 过滤放行后抢走 p2 真数据表。
    """
    for row in t.grid[1:]:
        cells = [c.text.strip() for c in row]
        if any(cells[1:]):
            return True
    return False


def _select_table(tables: list[NormalizedTable], q: Question,
                  rng: ParsedRange,
                  hits: list[dict] | None = None) -> tuple[NormalizedTable, float]:
    """表格选择打分（设计文档 §5.5 select.py）。

    hint 匹配表名 0.5 + 题干关键词与表头重叠 0.3 + 页范围一致 0.2
    + RAG 命中相对分 0.4（表在检索命中中的排名分 / 候选最高分，
    top 表 = 0.4 满分）。
    RAG 项必要性（007.pdf Q55-59 教训）：hint/表头为字面匹配，
    跨页残头碎表 '1．固定资产情况' 与概览碎表凭表名字面完胜真数据
    表 '财务报表附注'（表名泛化），而 RAG 检索已正确按内容相似度
    把真表排第一——选表必须融合该信号。
    """
    hint = (q.table_hint or "").strip()
    q_tokens = set(_tokenize(q.question))
    rag_top = 0.0
    if hits:
        rag_top = max((_retrieval_rank(t, hits) for t in tables), default=0.0)

    def score(t: NormalizedTable) -> float:
        s = 0.0
        if hint and t.table_name:
            s += 0.5 * fuzz.partial_ratio(hint, t.table_name) / 100
        elif hint:
            # 表名缺失时与首列表头比
            head = " ".join(c.text for c in t.grid[0]) if t.grid else ""
            s += 0.3 * fuzz.partial_ratio(hint, head) / 100
        header_tokens = set()
        for row in t.grid[:2]:
            header_tokens |= _tokenize(" ".join(c.text for c in row))
        if q_tokens and header_tokens:
            s += 0.3 * len(q_tokens & header_tokens) / max(len(q_tokens), 1)
        if rng.page_range and rng.page_range[0] <= t.page <= rng.page_range[1]:
            s += 0.2
        if rag_top > 0:
            s += 0.4 * _retrieval_rank(t, hits) / rag_top
        return s

    ranked = sorted(tables, key=score, reverse=True)
    return ranked[0], score(ranked[0])


def _tokenize(text: str) -> set[str]:
    """简单中文分词：2-gram + 英文/数字词。"""
    import re
    tokens: set[str] = set()
    ascii_words = re.findall(r"[A-Za-z0-9]{2,}", text)
    tokens.update(w.lower() for w in ascii_words)
    cn = re.sub(r"[^\u4e00-\u9fff]", "", text)
    tokens.update(cn[i:i + 2] for i in range(len(cn) - 1))
    return tokens


def _confidence_score(events: list[str]) -> float:
    return max(0.2, round(1.0 - sum(_PENALTY.get(e, 0.1) for e in events), 2))


def _empty_value(answer_format: str) -> str:
    """schema 合法空框架（答案永不为空原则）。"""
    if answer_format == "number":
        return "0"
    if answer_format == "json_array":
        return "[]"
    if answer_format == "json":
        return '{"row_count": 1, "col_count": 1, "cells": [{"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": ""}]}'
    return ""


def _no_table_result() -> dict:
    return {"raw_result": {"kind": "empty", "value": None},
            "steps": [TraceStep(step="answer", title="答题", status="fail",
                                data={"reason": "无可用表格，输出空框架"})]}
