# -*- coding: utf-8 -*-
"""L2 运行时探测准确率验证：与调研分类清单对照（不依赖 L1）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from table_qa.config import get_settings
from table_qa.ingest.categorize import _detect_category


def main() -> int:
    settings = get_settings()
    import json
    from collections import Counter

    # 调研真值
    truth = json.loads(
        (settings.paths.abs_path(settings.paths.categories)).read_text(encoding="utf-8"))

    files_dir = settings.paths.abs_path(settings.paths.files_dir)
    matrix = Counter()
    details: list[str] = []
    for path in sorted(files_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
            continue
        detected, reason = _detect_category(path, settings)
        real = truth.get(path.name, {}).get("category", "?")
        matrix[(real, detected)] += 1
        if real != detected:
            details.append(f"  {path.name}: 真值={real} 探测={detected} ({reason})")

    # 汇总
    print("混淆矩阵 (真值 -> 探测): ")
    for (real, det), n in sorted(matrix.items()):
        mark = "OK " if real == det else "MISS"
        print(f"  {real} -> {det}: {n} {mark}")
    total = sum(matrix.values())
    correct = sum(n for (r, d), n in matrix.items() if r == d)
    print(f"\n总体准确率: {correct}/{total} = {correct / total:.1%}")
    print("\n误判明细（图片 F/C 与 PDF D/B 边界属预期近似）:")
    print("\n".join(details[:30]) if details else "  无")

    # 分类别召回（图片类 F 检测最关键）
    for cat in "ABCDF":
        n_real = sum(n for (r, _), n in matrix.items() if r == cat)
        if n_real:
            hit = matrix.get((cat, cat), 0)
            print(f"{cat} 类召回: {hit}/{n_real}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
