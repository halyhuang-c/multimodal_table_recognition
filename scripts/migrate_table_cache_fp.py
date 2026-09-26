"""一次性迁移：cache/tables/*.json 的 _meta.prompt_fp 改写为
「仅 recognize 类 prompt」的新指纹（配合 prompts.py 指纹收敛，
避免 answer 类 prompt 变更触发全量重识别烧额度）。零 VLM 调用。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from loguru import logger

from table_qa.config import get_settings
from table_qa.prompts import get_prompt_manager

logger.remove()

s = get_settings()
fp = get_prompt_manager().fingerprint()
tables_dir = s.paths.abs_path(s.paths.cache_dir) / "tables"
files = sorted(tables_dir.glob("*.json"))
print(f"新识别指纹: {fp}  待迁移缓存: {len(files)} 文件")

for p in files:
    payload = json.loads(p.read_text(encoding="utf-8"))
    old = payload.get("_meta", {}).get("prompt_fp")
    payload["_meta"]["prompt_fp"] = fp
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"  {p.name}: {old} -> {fp}")
print("迁移完成")
