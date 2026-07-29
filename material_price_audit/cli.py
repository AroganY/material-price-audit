"""Small command-line shell around the browser-first product."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .env_check import check_environment, ensure_or_exit, try_auto_install
from .excel_io import resolve_inquiry_path
from .normalize import load_canonical_items, save_canonical_json
from .runtime import get_user_settings, load_config, project_root
from .schema_map import detect_workbook_schema, dump_schema_preview


def cmd_check(args: argparse.Namespace) -> int:
    if args.auto_install:
        result = try_auto_install(project_root() / "requirements.txt")
    else:
        result = check_environment(
            require_browser=bool(args.force),
            force=bool(args.force),
            use_cache=not bool(args.force),
        )
    result.print(quiet_ok=False)
    print(f"\npackage version: {__version__}")
    print(f"project root   : {project_root()}")
    if not result.ok:
        print("\n只安装缺失依赖即可：", file=sys.stderr)
        print(
            f"  {sys.executable} -m material_price_audit check --auto-install",
            file=sys.stderr,
        )
    return 0 if result.ok else 2


def cmd_parse(args: argparse.Namespace) -> int:
    """Developer diagnostic: recognize workbook columns without opening sites."""
    ensure_or_exit(require_browser=False, quiet=True)
    root = project_root()
    config_path = Path(args.config) if args.config else root / "config.yaml"
    config = load_config(config_path if config_path.exists() else None)
    settings = get_user_settings(root, config)
    if args.no_llm:
        settings.llm_enabled = False
    try:
        input_path = resolve_inquiry_path(args.input or None, root / "data/input")
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    schema = detect_workbook_schema(
        input_path,
        root,
        settings,
        use_cache=not args.refresh_schema,
        force_refresh=args.refresh_schema,
    )
    if not schema.sheets:
        print("ERROR: 未识别到材料工作表，请在向导中检查识表预览。", file=sys.stderr)
        return 2

    schema_path = Path(args.schema_out) if args.schema_out else root / "data/output/schema.json"
    items_path = (
        Path(args.canonical_out)
        if args.canonical_out
        else root / "data/output/canonical_items.json"
    )
    dump_schema_preview(schema, schema_path)
    items = load_canonical_items(input_path, schema)
    save_canonical_json(items, items_path)
    print(f"识别完成：{len(schema.sheets)} 个工作表，{len(items)} 条材料")
    print(f"schema: {schema_path}")
    print(f"items : {items_path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    ensure_or_exit(require_browser=False, quiet=True)
    from .webapp.server import run_server

    run_server(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="material-price-audit",
        description="材料询价向导：上传 Excel、分站登录、严格匹配、导出结果",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="", help="可选 config.yaml 路径")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="检查运行环境")
    check.add_argument("--force", action="store_true", help="同时验证浏览器能否启动")
    check.add_argument("--auto-install", action="store_true", help="只安装缺失依赖")
    check.set_defaults(func=cmd_check)

    serve = commands.add_parser("serve", help="启动浏览器询价向导（主入口）")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true", help="不自动打开向导页面")
    serve.set_defaults(func=cmd_serve)

    parse = commands.add_parser("parse", help="仅识别 Excel，不访问报价平台")
    parse.add_argument("--input", default="", help="Excel 文件或 data/input 目录")
    parse.add_argument("--schema-out", default="")
    parse.add_argument("--canonical-out", default="")
    parse.add_argument("--refresh-schema", action="store_true")
    parse.add_argument("--no-llm", action="store_true")
    parse.set_defaults(func=cmd_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
