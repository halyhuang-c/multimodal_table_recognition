"""loguru 日志配置：集中式单文件 + 大小/日期滚动 + 任务上下文。

设计：
- 全部日志写入 logs/table-qa_{日期}.log：文件名含日期（按天分文件），
  单文件超 20MB 自动滚动（loguru 重命名加时间戳后缀），保留 30 天
- 并行场景可读性：格式含 [qid|file] 任务上下文（logger.contextualize 绑定）
  与线程号 T{id}，多题并行时每行日志可归位
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

# 项目根目录 = src/table_qa/ 向上两级
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = PROJECT_ROOT / "logs"

_FMT = ("{time:MM-DD HH:mm:ss.SSS} | {level:<7} | T{thread.id:<6} | "
        "[{extra[q]}|{extra[f]}] {message}")


def setup_logging(verbose: bool = False) -> None:
    """初始化日志。控制台精简（INFO/DEBUG），文件全量滚动记录。"""
    logger.remove()
    # Windows 控制台编码安全
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 保守降级
        pass
    # 上下文缺省值：未绑定 qid/file 的日志显示 [-|-] 而非报错
    logger.configure(extra={"q": "-", "f": "-"})
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level, format=_FMT)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(LOGS_DIR / "table-qa_{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        format=_FMT,
        rotation="20 MB",        # 单文件超 20MB 滚动（文件名已按日期分天）
        retention="30 days",     # 保留 30 天，过期自动清理
        encoding="utf-8",
        enqueue=True,            # 多线程写文件安全
    )
