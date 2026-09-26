"""强制重识别指定文件（删缓存+清chunk+重识别入库），验证 vision 调用可恢复性。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\reingest.py 087.png 089.png
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.cache import TableStore  # noqa: E402
from table_qa.config import get_settings  # noqa: E402
from table_qa.ingest.categorize import load_profiles  # noqa: E402
from table_qa.indexing.vector_store import TableVectorStore  # noqa: E402
from table_qa.logging_setup import setup_logging  # noqa: E402
from table_qa.runner import Runner  # noqa: E402


def main() -> None:
    names = sys.argv[1:]
    setup_logging()
    s = get_settings()
    profiles = load_profiles(s)
    store = TableStore(s)
    vector = TableVectorStore(s)
    runner = Runner(s)

    for name in names:
        profile = profiles.get(name)
        if profile is None:
            print(f"!! 档案缺失: {name}")
            continue
        cache = store._path(profile.file_id)
        if cache.exists():
            cache.unlink()
            print(f"[1] 已删缓存: {cache.name}")
        vector._collection.delete(where={"file_name": name})
        print(f"[2] 已清 {name} 向量 chunk")

    runner.ingest(only=None) if False else None
    # 直接识别这些文件（不走题目过滤）
    runner._ingest_files(set(names), profiles)
    print("[3] 重识别完成")


if __name__ == "__main__":
    main()
