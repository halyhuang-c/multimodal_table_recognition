"""核心数据模型：全流水线统一使用 pydantic v2 强类型。

依赖方向（设计文档 §10.1）：本模块不依赖任何其他业务模块。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 题目与文件
# ---------------------------------------------------------------------------

QuestionType = Literal["structure", "extract", "thinking"]
AnswerFormat = Literal["string", "number", "json_array", "json"]
FileCategory = Literal["A", "B", "C", "D", "F"]
Confidence = Literal["high", "medium", "low"]


class Question(BaseModel):
    """tests.xlsx 中的一行（已修复）。"""

    model_config = ConfigDict(frozen=True)

    id: str
    file_name: str
    question_type: QuestionType
    question: str
    table_hint: str | None = None
    answer_format: AnswerFormat = "string"
    # answer_format 列的原始文本（官方语义是"对答案格式的补充说明"，可能是
    # 自然语言如"百分比，保留一位小数"）：规范化枚举之外的信息保留于此，
    # 供精度/单位解析与答题 prompt 使用
    format_note: str = ""
    # 修复标记（审计用）
    repaired: list[str] = Field(default_factory=list)


class FileProfile(BaseModel):
    """文件档案：运行时 L2 探测（文本密度 / 结构线）生成。"""

    file_id: str                      # 内容 sha1 前 12 位
    file_name: str
    path: Path
    category: FileCategory = "C"      # 未知文件默认 C（双引擎）
    dedup_of: str | None = None       # 与其他文件内容相同（如 056≡010）


class ParsedRange(BaseModel):
    """题目范围解析结果。"""

    page_range: tuple[int, int] | None = None   # 1-based，含端点
    row_range: tuple[int, int] | None = None    # 0-based，含端点
    col_range: tuple[int, int] | None = None


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------


class Cell(BaseModel):
    """展开矩阵中的一个单元格（锚点格携带合并跨度）。"""

    text: str = ""
    row: int = 0
    col: int = 0
    rowspan: int = 1
    colspan: int = 1
    is_anchor: bool = True            # 合并区域的左上角

    @property
    def is_masked(self) -> bool:
        return "[MASK]" in self.text or "[UNSURE" in self.text


class NormalizedTable(BaseModel):
    """融合/识别后的规范表格——系统核心资产。

    grid 为展开矩阵：合并区域用同一 Cell 引用的副本填充（is_anchor=False），
    便于 DataFrame 直接消费；序列化结构 JSON 时只取锚点格。
    """

    file_id: str
    file_name: str
    table_name: str | None = None
    page: int = 1
    html: str
    grid: list[list[Cell]]
    source_engine: str = "unknown"    # pdfplumber / qwen-vl-max / ...
    unit_note: str | None = None
    business_annotation: str | None = None
    confidence: Confidence = "medium"

    @property
    def n_rows(self) -> int:
        return len(self.grid)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.grid), default=0)

    def anchor_cells(self) -> list[Cell]:
        """只返回合并区域的锚点格（结构序列化用）。"""
        return [c for row in self.grid for c in row if c.is_anchor]


# ---------------------------------------------------------------------------
# 答题与可观测性
# ---------------------------------------------------------------------------


class TraceStep(BaseModel):
    """单个处理步骤的留痕（Web 调试界面按此渲染）。"""

    step: str                         # 节点名，如 retrieve / extract
    title: str                        # 中文标题
    status: Literal["ok", "warn", "fail", "skip"] = "ok"
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=lambda: time.time())


class UsageRecord(BaseModel):
    """一次模型调用的 token 用量（按 phase 分类统计）。"""

    phase: str                        # recognize / repair / annotate / extract / ...
    model: str
    question_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AnswerRecord(BaseModel):
    """一道题的最终作答。"""

    question_id: str
    value: str                        # 按 answer_format 序列化后的答案
    confidence: Confidence = "medium"
    confidence_score: float = 1.0     # 0~1 连续分（初始 1.0，事件扣分）
    confidence_events: list[str] = Field(default_factory=list)
    error_tag: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)
    # LLM 对话留痕（chat 视图渲染）：prompt/响应/模型/耗时/token，随 trace 持久化
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)


def score_to_confidence(score: float) -> Confidence:
    """连续分 → 三档置信度。阈值来自设计文档：>=0.8 high，>=0.5 medium。"""
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"
