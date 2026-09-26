"""CLI 入口：table-qa run / ingest / web。"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="table-qa", description="多模态表格识别与问答流水线")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="批处理运行题目（自动续跑：已完成题目跳过）")
    p_run.add_argument("--tests", default=None, help="tests.xlsx 路径（默认取配置）")
    p_run.add_argument("--limit", type=int, default=None, help="只跑前 N 题")
    p_run.add_argument("--only", default=None, help="只跑指定 id（逗号分隔）")
    p_run.add_argument("--submit", action="store_true", help="提交模式（不生成调试文件）")
    p_run.add_argument("--verbose", "-v", action="store_true")

    p_ing = sub.add_parser("ingest", help="纯识别阶段：识别题目文件并入向量库（不答题）")
    p_ing.add_argument("--tests", default=None, help="tests.xlsx 路径（默认取配置）")
    p_ing.add_argument("--limit", type=int, default=None, help="只识别前 N 题涉及的文件")
    p_ing.add_argument("--only", default=None, help="只识别指定 id 涉及的文件（逗号分隔）")
    p_ing.add_argument("--verbose", "-v", action="store_true")

    p_re = sub.add_parser("reindex", help="从识别缓存重建向量库（不重新识别，零 vision 额度）")
    p_re.add_argument("--no-annotate", action="store_true",
                      help="跳过 LLM 业务注释（chunk 用表头兑底，零 text 额度）")
    p_re.add_argument("--verbose", "-v", action="store_true")

    p_web = sub.add_parser("web", help="启动 Web 调试界面")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()

    if args.cmd == "run":
        _cmd_run(args)
    elif args.cmd == "ingest":
        _cmd_ingest(args)
    elif args.cmd == "reindex":
        _cmd_reindex(args)
    elif args.cmd == "web":
        _cmd_web(args)


def _cmd_run(args: argparse.Namespace) -> None:
    from table_qa.config import get_settings
    from table_qa.logging_setup import setup_logging
    from table_qa.runner import Runner

    settings = get_settings()
    if args.submit:
        settings.output.show_confidence = False
    setup_logging(verbose=args.verbose)

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    runner = Runner(settings)
    result = runner.run(limit=args.limit, only=only)
    print(f"DONE -> {result}")


def _cmd_ingest(args: argparse.Namespace) -> None:
    from table_qa.config import get_settings
    from table_qa.logging_setup import setup_logging
    from table_qa.runner import Runner

    settings = get_settings()
    setup_logging(verbose=args.verbose)

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    runner = Runner(settings)
    runner.ingest(limit=args.limit, only=only)
    print("DONE -> 识别阶段完成（缓存与向量库已更新，未答题）")


def _cmd_reindex(args: argparse.Namespace) -> None:
    from table_qa.config import get_settings
    from table_qa.logging_setup import setup_logging
    from table_qa.runner import Runner

    settings = get_settings()
    setup_logging(verbose=args.verbose)

    Runner(settings).reindex(annotate=not args.no_annotate)
    print("DONE -> 向量库已按新 chunk 模板重建（可直接 table-qa run）")


def _cmd_web(args: argparse.Namespace) -> None:
    import uvicorn

    from table_qa.logging_setup import setup_logging

    setup_logging()
    print(f"Web 调试界面: http://{args.host}:{args.port}")
    uvicorn.run("table_qa.web.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
