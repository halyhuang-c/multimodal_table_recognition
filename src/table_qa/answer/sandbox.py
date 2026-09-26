"""LLM 生成代码的受限执行沙箱（thinking 题确定性计算）。

安全设计（设计文档 §5.5.1）：
- AST 预检：禁止 import / open / eval / exec / dunder 属性 / 任意函数调用白名单外
- 执行隔离：ProcessPoolExecutor 子进程 + 超时（Windows 信号限制的替代方案）
- 符号白名单：df / pd / np / len / sum / round / max / min / sorted / abs / range / str / int / float
"""

from __future__ import annotations

import ast
from concurrent.futures import ProcessPoolExecutor
from typing import Any

_ALLOWED_FUNCS = {"len", "sum", "round", "max", "min", "sorted", "abs", "range",
                  "str", "int", "float", "list", "set", "enumerate", "zip",
                  "any", "all", "next", "dict", "tuple", "bool", "map", "filter",
                  "isinstance", "reversed"}
_BANNED_ATTR_PREFIX = "__"
_TIMEOUT_SECONDS = 15


class SandboxViolation(PermissionError):
    """代码未通过 AST 预检。"""


def check_code(code: str) -> None:
    """AST 预检：不合法直接抛 SandboxViolation（执行前调用）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxViolation(f"语法错误: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            raise SandboxViolation("禁止 import")
        if isinstance(node, ast.Name):
            if node.id in {"eval", "exec", "compile", "open", "__import__", "globals", "locals"}:
                raise SandboxViolation(f"禁止内置: {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith(_BANNED_ATTR_PREFIX):
                raise SandboxViolation(f"禁止 dunder 属性: {node.attr}")
            # 方法调用（df['x'].sum() 等）合法；pd./np. 前缀属性由 env 注入限定
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id not in _ALLOWED_FUNCS:
                raise SandboxViolation(f"函数不在白名单: {func.id}")
            if isinstance(func, ast.Attribute):
                # pd.xxx(...) / np.xxx(...) 已由 Attribute 检查放行
                pass


def _execute(code: str, df_dict: dict) -> Any:
    """子进程工作函数：受限 globals 执行，返回 result。"""
    import builtins
    import numpy as np
    import pandas as pd

    df = pd.DataFrame(df_dict)  # 从 dict 重建（跨进程序列化）
    env: dict[str, Any] = {
        "df": df, "pd": pd, "np": np,
        **{name: getattr(builtins, name) for name in _ALLOWED_FUNCS},
        "__builtins__": {},   # 禁止访问其余内置（open/eval/__import__ 等）
    }
    loc: dict[str, Any] = {}
    try:
        exec(code, env, loc)  # noqa: S102 - 受控执行（AST 预检 + 子进程隔离）
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}
    return loc.get("result")


def run_sandbox(code: str, df) -> Any:
    """在子进程中执行生成的 pandas 代码。

    返回执行结果（str/int/float/list，numpy 标量已转原生）；
    超时/违规/运行时错误抛 RuntimeError（由调用方降级处理）。
    """
    code = _ensure_result_assignment(code)
    check_code(code)
    df_dict = df.to_dict(orient="list")
    with ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute, code, df_dict)
        try:
            out = future.result(timeout=_TIMEOUT_SECONDS)
        except TimeoutError as e:
            raise RuntimeError(f"沙箱超时(>{_TIMEOUT_SECONDS}s)") from e
    if isinstance(out, dict) and "__error__" in out:
        raise RuntimeError(out["__error__"])
    out = _to_native(out)
    if isinstance(out, (str, int, float, list, type(None))):
        return out
    return str(out)


def _ensure_result_assignment(code: str) -> str:
    """末尾表达式自动包装为 result = (expr)，保证 loc 中有 result。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    has_result = any(
        isinstance(n, ast.Assign | ast.AugAssign | ast.AnnAssign)
        and _assigns_to(n, "result")
        for n in ast.walk(tree)
    )
    if not has_result and tree.body and isinstance(tree.body[-1], ast.Expr):
        assign = ast.Assign(
            targets=[ast.Name(id="result", ctx=ast.Store())],
            value=tree.body[-1].value,
        )
        ast.copy_location(assign, tree.body[-1])
        tree.body[-1] = assign
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)
    if not has_result:
        return code.rstrip().rstrip(";") + "\nresult = None"
    return code


def _assigns_to(node: ast.AST, name: str) -> bool:
    targets = getattr(node, "targets", None) or ([node.target] if getattr(node, "target", None) else [])
    return any(isinstance(t, ast.Name) and t.id == name for t in targets)


def _to_native(value: Any) -> Any:
    """numpy 标量/数组转 Python 原生类型（JSON 与 Excel 序列化安全）。"""
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return value
