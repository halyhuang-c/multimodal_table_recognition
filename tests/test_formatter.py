"""答案格式化官方规范单测（赛题说明 §答案规范）。

覆盖：千分位去除、数组空值填空串、数组数字元素数值化（官方示例
[1,"销售额"]）、前导零编号保护、科学计数法守卫、None/NaN 拦截、
复合题空占位与枚举题过滤的区分。
"""

import math

import numpy as np

from table_qa.answer.formatter import format_answer
from table_qa.schema import Question


def _q(question: str, fmt: str = "string") -> Question:
    return Question(id="t1", file_name="f.pdf", question_type="extract",
                    question=question, answer_format=fmt)


# ---------------------------------------------------------------------------
# number：去千分位、精度、科学计数法守卫
# ---------------------------------------------------------------------------


def test_number_thousands_separator_removed() -> None:
    # 官方示例：填写 125000，不要填写 125,000
    assert format_answer("125,000", "number") == "125000"
    assert format_answer("1,884,661,393.95", "number") == "1884661393.95"
    assert format_answer(125000, "number") == "125000"


def test_number_precision_directed() -> None:
    assert format_answer(0.125, "number", precision=2) == "0.12"
    assert format_answer(3, "number", precision=1) == "3.0"


def test_number_integer_no_decimal() -> None:
    assert format_answer(100.0, "number") == "100"
    assert format_answer(12.30, "number") == "12.3"


def test_number_scientific_notation_guard() -> None:
    # 极小浮点 repr 是 "1e-10"，不得泄漏进答案
    assert "e" not in format_answer(1e-10, "number")


def test_number_none_and_nan_intercepted() -> None:
    assert format_answer(None, "number") == ""
    assert format_answer(float("nan"), "number") == ""


def test_number_mask_and_notfound_not_fake_zero() -> None:
    # [MASK]/未找到是"数据不可辨认"，不得伪装成数值 0（qid=74 实测：
    # const [MASK] 被归一 0 后置信度 high，掩盖失败）——输出空串
    assert format_answer("[MASK]", "number") == ""
    assert format_answer("未找到", "number") == ""
    # 其余无数值残留仍归一 0（原行为不变，影响面隔离）
    assert format_answer("--", "number") == "0"
    assert format_answer(0, "number") == "0"


def test_number_european_decimal_comma() -> None:
    # 欧式小数逗号（葡语区 031.png 实测 "9,3" 被当千分位剥成 93）：
    # 千分位分组恒为 3 位，逗号后 1~2 位必是小数
    assert format_answer("9,3", "number") == "9.3"
    assert format_answer("9,4", "number") == "9.4"
    assert format_answer("10,25", "number") == "10.25"
    # 千分位（3 位分组）行为不变
    assert format_answer("1,234", "number") == "1234"
    assert format_answer("125,000", "number") == "125000"
    # 数组元素同样归一为数值（官方规范数字不带引号）
    assert format_answer(["9,3", "10"], "json_array") == '[9.3, 10]'


def test_string_unwraps_single_element_list() -> None:
    # string 题的单元素列表是提取层包装残留：解包为标量
    # （qid=95/96 fmt=string 却输出 JSON 数组导致格式不匹配）
    assert format_answer(["成果:产品原型v2.0"], "string") == "成果:产品原型v2.0"
    assert format_answer([69.49], "string") == "69.49"
    # 多元素列表仍按 JSON 数组（枚举语义）
    assert format_answer(["a", "b"], "string") == '["a", "b"]'
    # 字符串形式的单元素数组（提取层偶发）同样解包（qid=95）
    assert format_answer('["开发方案+技术路径"]', "string") == "开发方案+技术路径"


# ---------------------------------------------------------------------------
# json_array：空值填空字符串（不是 null）、数字元素不带引号
# ---------------------------------------------------------------------------


def test_json_array_null_becomes_empty_string() -> None:
    # 官方：数组中空值填写空字符串
    out = format_answer([1, "销售额", None], "json_array")
    assert out == '[1, "销售额", ""]'
    assert "null" not in out


def test_json_array_nan_becomes_empty_string() -> None:
    out = format_answer(["a", float("nan")], "json_array")
    assert out == '["a", ""]'


def test_json_array_number_elements_unquoted() -> None:
    # 官方示例：[1,"销售额","产品销售表"] 数字不带引号
    out = format_answer(["1", "销售额", "产品销售表"], "json_array")
    assert out == '[1, "销售额", "产品销售表"]'


def test_json_array_thousands_normalized() -> None:
    out = format_answer(["125,000", "x"], "json_array")
    assert out == '[125000, "x"]'


def test_json_array_leading_zero_id_kept_as_string() -> None:
    # "01" 这类编号不是数字 1
    out = format_answer(["01", "058"], "json_array")
    assert out == '["01", "058"]'


def test_json_array_compound_keeps_empty_placeholders() -> None:
    # 复合题（一题多问按字段顺序）：空占位防元素错位
    q = _q("应收票据和应收款项融资分别是多少", "json_array")
    out = format_answer(["", 46.0, "A"], "json_array", q=q)
    assert out == '["", 46, "A"]'


def test_json_array_enum_filters_empty_items() -> None:
    # 枚举题（"列出所有X"）：空串残留过滤
    q = _q("列出所有销售额超过100的产品", "json_array")
    out = format_answer(["A", "", "B", None], "json_array", q=q)
    assert out == '["A", "B"]'


# ---------------------------------------------------------------------------
# string / json / 入口守卫
# ---------------------------------------------------------------------------


def test_string_halfwidth_and_number_clean() -> None:
    assert format_answer("１２３４５", "string") == "12345"
    assert format_answer("125,000", "string") == "125000"
    assert format_answer(" 01 号 ", "string") == "01 号"


def test_string_none_intercepted() -> None:
    assert format_answer(None, "string") == ""
    assert format_answer(math.nan, "string") == ""


def test_json_passthrough_structure() -> None:
    value = '{"row_count": 2, "col_count": 1, "cells": []}'
    assert format_answer(value, "json") == value


# ---------------------------------------------------------------------------
# numpy 标量回归（qid=198）：np.int64 非 int 子类穿透致 json.dumps 崩溃
# ---------------------------------------------------------------------------


def test_json_array_numpy_int64_elements() -> None:
    # thinking seq/cell 取 df 单元格返回 np.int64，此前抛
    # "Object of type int64 is not JSON serializable" 整题崩为空框架
    out = format_answer([np.int64(9), "x", np.int64(125000)], "json_array")
    assert out == '[9, "x", 125000]'


def test_json_array_numpy_float_and_nan() -> None:
    out = format_answer([np.float64(1.5), np.float64("nan")], "json_array")
    assert out == '[1.5, ""]'


def test_number_numpy_scalars() -> None:
    assert format_answer(np.int64(125000), "number") == "125000"
    assert format_answer(np.float64(1.5), "number") == "1.5"


def test_strip_float_noise_numpy_in_ops_and_thinking() -> None:
    from table_qa.answer.ops import _strip_float_noise as strip_ops
    from table_qa.answer.thinking import _strip_float_noise as strip_think

    for strip in (strip_ops, strip_think):
        assert strip(np.int64(9)) == 9
        assert isinstance(strip(np.int64(9)), int)
        assert strip([np.int64(9), np.float64(0.1 + 0.2)]) == [9, 0.3]
