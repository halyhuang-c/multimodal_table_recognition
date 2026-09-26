"""页面资产加载：PDF 渲染页图 / 图片直接载入。

PageAsset 是识别引擎的统一输入：
- 扫描 PDF / 图片（C/D/F 类）：渲染 220DPI 页图供视觉识别
- 数字原生 PDF（A/B 类）：pdfplumber 直接消费文件本身，PageAsset 携带 text_layer 校验源
"""

from __future__ import annotations

from pathlib import Path

import pymupdf  # PyMuPDF（fitz 别名已弃用）
from loguru import logger
from pydantic import BaseModel

from table_qa.config import Settings
from table_qa.schema import FileCategory, FileProfile

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class PageAsset(BaseModel):
    """一个文件的一页。"""

    file_id: str
    file_name: str
    page_no: int                        # 1-based
    image_path: Path | None = None      # 视觉引擎输入；数字 PDF 可为空
    text_layer: str | None = None       # PDF 文本层（数字校验源）
    category: FileCategory = "C"


def render_pdf_pages(profile: FileProfile, settings: Settings, out_dir: Path) -> list[PageAsset]:
    """渲染 PDF 每页为 PNG，返回 PageAsset 列表。"""
    zoom = settings.pdf.dpi / 72
    out_dir.mkdir(parents=True, exist_ok=True)
    assets: list[PageAsset] = []
    with pymupdf.open(profile.path) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            img_path = out_dir / f"{profile.file_id}_p{i}.png"
            pix.save(img_path)
            assets.append(PageAsset(
                file_id=profile.file_id,
                file_name=profile.file_name,
                page_no=i,
                image_path=img_path,
                text_layer=page.get_text() or None,
                category=profile.category,
            ))
    logger.info("渲染页图: {} 共 {} 页 @{}DPI", profile.file_name, len(assets), settings.pdf.dpi)
    return assets


def load_pages(profile: FileProfile, settings: Settings, pages_dir: Path) -> list[PageAsset]:
    """加载文件全部页面资产。

    - 图片文件：单页，image_path 指向原文件
    - PDF：渲染页图（视觉引擎用），text_layer 一并带回
    """
    if profile.path.suffix.lower() in IMAGE_EXTS:
        return [PageAsset(
            file_id=profile.file_id,
            file_name=profile.file_name,
            page_no=1,
            image_path=profile.path,
            category=profile.category,
        )]
    return render_pdf_pages(profile, settings, pages_dir)
