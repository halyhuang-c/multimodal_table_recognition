"""Dump pdfplumber 原始表格提取结果（识别层调试：对比向量库内容，定位丢列/拆表）。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\dump_pdf_tables.py files\\006.pdf 2
第二个参数为 1-based 页码；不传则 dump 全部页。
"""
from __future__ import annotations

import sys

import pdfplumber


def dump_page(pdf_path: str, page_no: int, text_mode: bool = False) -> None:
    import pdfplumber
    settings = ({"vertical_strategy": "text", "horizontal_strategy": "text"}
                if text_mode else None)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no - 1]
        tables = page.extract_tables(table_settings=settings) or []
        mode = "text策略" if text_mode else "默认lines策略"
        print(f"===== {pdf_path} 第 {page_no} 页 [{mode}]: 提取到 {len(tables)} 张表 =====")
        for i, t in enumerate(tables):
            print(f"----- 表 {i}: {len(t)} 行 x {max(len(r) for r in t)} 列 -----")
            for row in t:
                print(" | ".join((c or "").replace("\n", "⏎") for c in row))
            print()
        text = page.extract_text() or ""
        print(f"----- 页面原始文本（前 2000 字符）-----")
        print(text[:2000])


def main() -> None:
    pdf_path = sys.argv[1]
    text_mode = "text" in sys.argv
    pages = [int(a) for a in sys.argv[2:] if a.isdigit()] or None
    if pages:
        for p in pages:
            dump_page(pdf_path, p, text_mode)
    else:
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
        for p in range(1, n + 1):
            dump_page(pdf_path, p, text_mode)


if __name__ == "__main__":
    main()
