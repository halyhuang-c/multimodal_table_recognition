"""文件加载与路由探测：实现设计文档 §2.3 路由决策树。

L2 运行时探测（文本密度 / 抽表可行性），不依赖任何预置清单。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from loguru import logger

from table_qa.config import Settings
from table_qa.schema import FileCategory, FileProfile

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
PDF_EXTS = {".pdf"}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _text_layer_garbled(text: str, settings: Settings) -> bool:
    """文本层是否为乱码（字体 ToUnicode 映射损坏的指纹）。

    损坏 PDF 提取出的"文字"全是符号与碎片数字（071-073 实测：符号占比
    0.71~0.88、CJK 为 0），但字符密度非空，会绕过密度路由被误当数字原生
    PDF。双条件判定，避免误伤英文表/数字密集表：
    - 符号（isalnum=False）占非空白字符比例超 garbled_symbol_ratio；
    - 且 CJK 与拉丁整词字符占比低于 garbled_word_ratio。
    """
    chars = [ch for ch in text if not ch.isspace()]
    n = len(chars)
    if n < 100:                      # 文本过少：交给密度逻辑路由
        return False
    symbol_n = sum(1 for ch in chars if not ch.isalnum())
    word_n = len(_CJK_RE.findall(text)) + sum(
        len(m) for m in _LATIN_WORD_RE.findall(text))
    return (symbol_n / n > settings.pdf.garbled_symbol_ratio
            and word_n / n < settings.pdf.garbled_word_ratio)


def file_sha(path: Path) -> str:
    """内容 sha1 前 12 位（识别缓存与去重键，010≡056 天然合并）。"""
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _detect_category(path: Path, settings: Settings) -> tuple[FileCategory, str]:
    """L2 运行时探测（真实赛题主路径，不依赖任何预置清单）。

    规则（设计文档 §2.3 决策树）：
    - PDF：文本密度 > 阈值 → B 类（数字原生，pdfplumber 主源）；否则 D 类（扫描件）
    - 图片：OpenCV 长直线检测——线框表格必有多条贯穿长线；
            卡片/树图/热力图等非线性布局几乎没有 → F 类（语义重建）
    """
    if path.suffix.lower() in IMAGE_EXTS:
        return _detect_image_category(path)
    if path.suffix.lower() in PDF_EXTS:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            pages = pdf.pages
            texts = [p.extract_text() or "" for p in pages]
            density = sum(len(t) for t in texts) / max(len(pages), 1)
        if density > settings.pdf.text_density_threshold:
            if _text_layer_garbled("\n".join(texts), settings):
                # 密度非空但文本层是符号乱码（ToUnicode 损坏）：pdfplumber 抽表
                # 只会得到乱码，按扫描件处理走 VLM（071-073 实测教训）
                return "D", f"garbled_text_layer({density:.0f}c/p)"
            return "B", f"digital_pdf({density:.0f}c/p)"   # 数字 PDF 默认 B，抽表成功后细化
        return "D", f"scanned_pdf({density:.0f}c/p)"
    return "C", "unknown"


def _detect_image_category(path: Path) -> tuple[FileCategory, str]:
    """图片 F/C 判别：结构线 + 色彩饱和度双信号。

    F 类（非线性可视化：树图/热力图/日程卡片/信息图）特征：
    - 少贯穿长直线（无行列网格），或
    - 大面积高饱和色块且几乎无白底（treemap 例外：矩形边多但满屏彩色）
    常规表格截图特征：白底为主、色块占比低。
    OpenCV 缺失时保守返回 C。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return "C", "image(no-cv2)"

    img = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return "C", "image(decode-fail)"
    # 降采样加速（分类不需要原始分辨率）
    h, w = img.shape[:2]
    scale = 1200 / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)

    # 信号 1：贯穿长直线数量（水平/垂直）
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=120,
                            minLineLength=int(img.shape[1] * 0.35),
                            maxLineGap=6)
    structural = 0
    if lines is not None and len(lines):
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            if dx > 4 * dy or dy > 4 * dx:
                structural += 1

    # 信号 2：高饱和色块占比 与 白底占比
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1].astype(np.float32), hsv[:, :, 2].astype(np.float32)
    colorful = float(np.mean((sat > 90) & (val > 60)))     # 彩色像素占比
    white = float(np.mean((val > 220) & (sat < 40)))       # 白底占比

    # F 判定：无线网格（lines<3）。实测色彩信号不可靠（F 类白底卡片 colorful=0.02、
    # C 类彩色截图 colorful=0.92 均存在），仅作参考记录不参与判定。
    # 漏检 F（线数不少的热力图/树图）由 qwen_engine 空结果重试链兜底切 semantic_rebuild。
    if structural < 3:
        return "F", f"nonlinear(lines={structural}, colorful={colorful:.2f})"
    return "C", f"image(lines={structural}, colorful={colorful:.2f})"


def load_profiles(settings: Settings) -> dict[str, FileProfile]:
    """加载 files/ 全部文件的档案：sha1 去重 + L2 运行时分类探测。

    返回 {file_name: FileProfile}。
    """
    files_dir = settings.paths.abs_path(settings.paths.files_dir)

    seen_sha: dict[str, str] = {}   # sha1 -> 首个文件名
    profiles: dict[str, FileProfile] = {}
    for path in sorted(files_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (IMAGE_EXTS | PDF_EXTS):
            continue
        sha = file_sha(path)
        dedup_of = seen_sha.get(sha)
        if dedup_of is None:
            seen_sha[sha] = path.name

        cat, reason = _detect_category(path, settings)
        logger.debug("L2 探测 {} -> {} ({})", path.name, cat, reason)

        profiles[path.name] = FileProfile(
            file_id=sha,
            file_name=path.name,
            path=path,
            category=cat,
            dedup_of=dedup_of,
        )
    return profiles
