"""范围解析（正则路径）与 answer_format 规范化单测。

覆盖官方规范关键语义：
- "第N行/第N列" 指单独第 N 行/列（0 基 N-1），不是前 N 行；
- "前N行/前N列" 是 [0, N-1]；
- answer_format 自然语言补充说明按关键词推断为四枚举。
"""

from table_qa.answer.structure import parse_range
from table_qa.ingest.questions import _normalize_answer_format
from table_qa.schema import Question


def _q(question: str) -> Question:
    return Question(id="t1", file_name="f.pdf", question_type="structure",
                    question=question, answer_format="json")


def test_nth_row_is_single_row() -> None:
    # “第3行” = 单独第 3 行（0 基 2），不是前 3 行
    rng, how = parse_range(_q("恢复第3行的表格结构"))
    assert how == "regex"
    assert rng.row_range == (2, 2)
    assert rng.col_range is None


def test_nth_col_is_single_col() -> None:
    rng, how = parse_range(_q("恢复第2列的结构"))
    assert how == "regex"
    assert rng.col_range == (1, 1)


def test_first_n_rows() -> None:
    rng, how = parse_range(_q("恢复前3行"))
    assert how == "regex"
    assert rng.row_range == (0, 2)


def test_first_n_rows_and_first_col() -> None:
    # 官方示例题型：“前1行和前1列” → 行集 ∪ 列集
    rng, how = parse_range(_q("恢复一下前1行和前1列的表格结构"))
    assert how == "regex"
    assert rng.row_range == (0, 0)
    assert rng.col_range == (0, 0)


def test_header_rows() -> None:
    rng, how = parse_range(_q("恢复表头行结构"))
    assert how == "regex"
    assert rng.row_range == (0, 1)


def test_page_range() -> None:
    rng, how = parse_range(_q("恢复第2页的表格"))
    assert how == "regex"
    assert rng.page_range == (2, 2)


# ---------------------------------------------------------------------------
# answer_format 自然语言兼容
# ---------------------------------------------------------------------------


def test_format_enum_passthrough() -> None:
    for fmt in ("string", "number", "json_array", "json"):
        assert _normalize_answer_format(fmt) == (fmt, False)


def test_format_note_number() -> None:
    assert _normalize_answer_format("百分比，保留一位小数") == ("number", True)


def test_format_note_json_array() -> None:
    assert _normalize_answer_format("JSON数组") == ("json_array", True)


def test_format_note_json() -> None:
    assert _normalize_answer_format("表格结构 JSON") == ("json", True)


def test_format_note_plain_text_falls_to_string() -> None:
    assert _normalize_answer_format("填写产品名称") == ("string", True)
