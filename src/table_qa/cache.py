"""识别产物持久化：文件级表格缓存（跨 run 复用）。

目录：cache/tables/{file_id}.json —— 该文件全部识别出的 NormalizedTable。
缓存 key 隐含 prompt 版本：file_id 变化或 prompt 指纹变化时由调用方失效。
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from table_qa.config import Settings
from table_qa.schema import NormalizedTable


class TableStore:
    """NormalizedTable 的文件级存储。"""

    def __init__(self, settings: Settings) -> None:
        self._dir = settings.paths.abs_path(settings.paths.cache_dir) / "tables"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._s = settings

    def _path(self, file_id: str) -> Path:
        return self._dir / f"{file_id}.json"

    def exists(self, file_id: str, prompt_fingerprint: str) -> bool:
        """缓存命中判定：文件存在且 prompt 指纹一致。"""
        p = self._path(file_id)
        if not p.exists():
            return False
        try:
            meta = json.loads(p.read_text(encoding="utf-8")).get("_meta", {})
            return meta.get("prompt_fp") == prompt_fingerprint
        except (json.JSONDecodeError, OSError):
            return False

    def save(self, file_id: str, tables: list[NormalizedTable], prompt_fingerprint: str) -> None:
        payload = {
            "_meta": {"prompt_fp": prompt_fingerprint, "count": len(tables)},
            "tables": [t.model_dump() for t in tables],
        }
        self._path(file_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logger.info("表格缓存写入: {} ({}) {} 张表", file_id, self._path(file_id).name, len(tables))

    def load(self, file_id: str) -> list[NormalizedTable]:
        p = self._path(file_id)
        if not p.exists():
            return []
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            return [NormalizedTable(**t) for t in payload.get("tables", [])]
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("表格缓存损坏，忽略: {} ({})", file_id, e)
            return []
