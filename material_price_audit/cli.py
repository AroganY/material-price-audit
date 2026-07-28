#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
material-price-audit CLI

Accuracy-first construction material inquiry price verification.
Platforms are user-selectable (jd / 1688 / taobao / tmall / zkh / suning / custom).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

from . import __version__
from .env_check import check_environment, ensure_or_exit
from .excel_io import export_rfq, load_inquiry, write_result_workbook
from .init_wizard import detect_state, print_agent_block, run_init, write_agent_guide
from .matcher import build_jobs
from .platforms import (
    load_platform_registry,
    login_urls_for,
    pick_best_candidate,
    resolve_enabled_platforms,
    search_on_platform,
)
from .scraper import launch_context, open_detail, pick_manual, score_title, to_evidence, wait_user


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: Path | None) -> dict:
    defaults = {
        "pricing": {
            "tax_divisor": 1.13,
            "never_exceed_submit": True,
            "open_detail": True,
            "min_title_score": 1,
            # multi: try all enabled platforms and pick best score / lowest price
            # preferred: try job.preferred platform first, then others
            "platform_strategy": "multi",
        },
        "browser": {
            "channel": "chrome",
            "headless": False,
            "login_wait_seconds": 120,
            "page_timeout_ms": 60000,
            "between_items_sleep": 1.2,
        },
        "platforms": {
            "enabled": ["guangcai", "huixun", "lingcai", "jd", "1688"],
            "definitions": {},
        },
        "excel": {},
    }
    if path and path.exists() and yaml:
        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(defaults.get(k), dict):
                # deep-ish: platforms.enabled replace entirely if provided
                if k == "platforms":
                    if isinstance(v, list):
                        defaults["platforms"] = {"enabled": v, "definitions": {}}
                    else:
                        defaults["platforms"].update(v)
                else:
                    defaults[k].update(v)
            else:
                defaults[k] = v
    return defaults


def load_evidence(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for e in data.get("results", []):
        if e.get("key"):
            out[e["key"]] = e
    return out


def save_evidence(path: Path, evidence: dict[str, dict], meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": __version__,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "results": list(evidence.values()),
    }
    if meta:
        payload["meta"] = meta
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_check(args):
    r = check_environment(require_browser=not args.skip_browser)
    r.print()
    print(f"\npackage version: {__version__}")
    print(f"package root   : {package_root()}")
    return 0 if r.ok else 2


def cmd_init(args):
    """Scaffold folders + config, then print agent/user guide."""
    root = package_root()
    plats = None
    if args.platforms:
        plats = [p.strip() for p in args.platforms.split(",") if p.strip()]
    st = run_init(
        root=root,
        platforms=plats,
        tax_divisor=float(args.tax or 1.13),
        force_config=bool(args.force),
    )
    guide = write_agent_guide(root, st)
    print("=== init 完成 ===")
    print(f"root     : {st.root}")
    print(f"config   : {st.config_path} (exists={st.config_exists})")
    print(f"input    : {st.input_path} (exists={st.input_exists})")
    print(f"output   : {st.output_dir}")
    print(f"profile  : {st.profile_dir}")
    print(f"platforms: {', '.join(st.platforms_enabled)}")
    print(f"env_ok   : {st.env_ok}")
    if not st.env_ok:
        print("环境未就绪，请先安装依赖（见下方 hints）")
        for h in st.env_hints:
            print(f"  {h}")
    print(f"guide    : {guide}")
    print_agent_block(st, guide)
    # init 本身成功即 0；环境问题通过 phase/AGENT_GUIDE 表达，便于 Agent 继续引导
    return 0


def cmd_guide(args):
    """Recompute phase and tell agent what to do / ask next."""
    root = package_root()
    st = detect_state(root)
    guide = write_agent_guide(root, st)
    print("=== guide ===")
    print(f"phase: {st.phase}")
    print(f"guide_file: {guide}")
    print_agent_block(st, guide)
    # also print file content path for agents to read
    print(f"\nAgent 请阅读: {guide}")
    return 0


def cmd_platforms(args):
    cfg = load_config(Path(args.config) if args.config else None)
    reg = load_platform_registry(cfg)
    enabled = resolve_enabled_platforms(cfg, args.platforms or None)
    print("=== 可用平台 Platforms ===")
    print(f"当前启用 enabled: {', '.join(enabled)}")
    print("")
    print(f"{'ID':<12} {'名称':<16} {'内置/自定义':<10} 登录页")
    print("-" * 72)
    for pid, spec in reg.items():
        from .platforms import BUILTIN

        kind = "内置" if pid in BUILTIN else "自定义"
        mark = "*" if pid in enabled else " "
        print(f"{mark}{pid:<11} {spec.name:<16} {kind:<10} {spec.login_url}")
    print("")
    print("说明: 行首 * 表示当前启用。用 --platforms 或 config.yaml platforms.enabled 指定。")
    print("造价常用: guangcai(广材网) huixun(慧讯网) lingcai(领材网) — 均属广联达材料价体系，需登录")
    print("电商补充: jd 1688 taobao tmall zkh suning")
    print("自定义站点: 在 config.yaml → platforms.definitions 添加 search_url / login_url / 选择器。")
    print("示例: --platforms guangcai,huixun,lingcai,jd,1688")
    return 0


def cmd_login(args):
    ensure_or_exit(require_browser=True)
    cfg = load_config(Path(args.config) if args.config else None)
    profile = Path(args.profile).expanduser().resolve()
    wait_s = args.login_wait or cfg["browser"]["login_wait_seconds"]
    enabled = resolve_enabled_platforms(cfg, args.platforms or None)
    reg = load_platform_registry(cfg)
    urls = login_urls_for(enabled, reg)
    if not urls:
        print("ERROR: 没有可登录的平台，请检查 --platforms 或 config", file=sys.stderr)
        return 2

    print("将依次打开以下平台，请在浏览器完成登录：")
    for pid, name, url in urls:
        print(f"  - [{pid}] {name}: {url}")

    pw, ctx, page = launch_context(
        profile, channel=cfg["browser"]["channel"], headless=False
    )
    try:
        for pid, name, url in urls:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            wait_user(
                f"【{name} / {pid}】请在浏览器登录（需要登录的站点请完成登录；可跳过仅浏览站）",
                wait_s,
                args.yes,
            )
        print(f"登录流程结束。配置目录: {profile}")
        print(f"已覆盖平台: {', '.join(p for p,_,_ in urls)}")
    finally:
        ctx.close()
        pw.stop()
    return 0


def _platform_order(job_platform: str, enabled: list[str], strategy: str) -> list[str]:
    """Order platforms to try for one item."""
    if strategy == "preferred" and job_platform in enabled:
        rest = [p for p in enabled if p != job_platform]
        return [job_platform] + rest
    # multi / default: try all enabled; put preferred first if present
    if job_platform in enabled:
        return [job_platform] + [p for p in enabled if p != job_platform]
    return list(enabled)


def cmd_scrape(args):
    ensure_or_exit(require_browser=True)
    cfg = load_config(Path(args.config) if args.config else None)

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    evidence_path = Path(args.evidence).expanduser().resolve()
    profile = Path(args.profile).expanduser().resolve()

    if not input_path.exists():
        print(f"ERROR: 询价单不存在 / input not found:\n  {input_path}", file=sys.stderr)
        print("请用 --input 指定清晰路径，例如: --input ./data/input/inquiry.xlsx", file=sys.stderr)
        return 2

    tax = float(cfg["pricing"]["tax_divisor"])
    never_exceed = bool(cfg["pricing"]["never_exceed_submit"])
    open_detail_flag = (
        bool(cfg["pricing"]["open_detail"]) if args.open_detail is None else args.open_detail
    )
    min_score = int(cfg["pricing"].get("min_title_score", 1))
    strategy = str(cfg["pricing"].get("platform_strategy") or "multi")
    timeout_ms = int(cfg["browser"]["page_timeout_ms"])
    sleep_s = float(cfg["browser"]["between_items_sleep"])
    wait_s = args.login_wait or cfg["browser"]["login_wait_seconds"]

    enabled = resolve_enabled_platforms(cfg, args.platforms or None)
    reg = load_platform_registry(cfg)
    unknown = [p for p in enabled if p not in reg]
    if unknown:
        print(f"ERROR: 未知平台 {unknown}。运行: python -m material_price_audit platforms", file=sys.stderr)
        return 2

    print("=== scrape ===")
    print(f"input    : {input_path}")
    print(f"output   : {output_path}")
    print(f"evidence : {evidence_path}")
    print(f"profile  : {profile}")
    print(f"platforms: {', '.join(enabled)}  strategy={strategy}")
    print(f"tax      : /{tax}  never_exceed={never_exceed}  open_detail={open_detail_flag}")

    items = load_inquiry(input_path, cfg.get("excel") or {})
    # preferred platform is only a hint; scrape tries all --platforms
    jobs = build_jobs(items, platforms=None)
    if args.limit and args.limit > 0:
        jobs = jobs[: args.limit]

    evidence = load_evidence(evidence_path)
    print(
        f"items={len(items)} matchable_jobs={len(jobs)} existing_verified="
        f"{sum(1 for e in evidence.values() if e.get('status')=='verified')}"
    )

    if not jobs:
        print("WARNING: 没有可自动匹配的型号/规则项。将只生成 pending 结果。")
        write_result_workbook(input_path, output_path, items, evidence, tax)
        return 0

    pw, ctx, page = launch_context(
        profile, channel=cfg["browser"]["channel"], headless=bool(cfg["browser"]["headless"])
    )
    try:
        # login tour for enabled platforms
        for pid, name, url in login_urls_for(enabled, reg):
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            wait_user(
                f"请确认已登录【{name}/{pid}】（不需要登录的站可直接回车）",
                wait_s if args.yes else 0,
                args.yes,
            )

        done = 0
        for job in jobs:
            it = job.item
            key = it.key
            if args.skip_existing and evidence.get(key, {}).get("status") == "verified":
                print(f"skip existing {it.name[:32]}")
                continue

            order = _platform_order(job.platform, enabled, strategy)
            print(f"→ [{it.sheet}] {it.name[:36]} | try {order} | {job.query}")

            all_cands: list[dict] = []
            try:
                for pid in order:
                    cands, st = search_on_platform(
                        page, pid, job.query, job.must, timeout_ms, min_score, reg
                    )
                    if st == "need_login":
                        wait_user(f"【{pid}】需要登录，请登录后继续", wait_s, args.yes)
                        cands, st = search_on_platform(
                            page, pid, job.query, job.must, timeout_ms, min_score, reg
                        )
                    if st.startswith("error:"):
                        print(f"  [{pid}] {st}")
                        continue
                    if cands:
                        print(f"  [{pid}] candidates={len(cands)} best¥{cands[0]['price_tax']}")
                        all_cands.extend(cands[:5])
                    else:
                        print(f"  [{pid}] no_match")

                if not all_cands:
                    evidence[key] = {
                        "key": key,
                        "status": "no_match",
                        "name": it.name,
                        "query": job.query,
                        "platforms_tried": order,
                    }
                    print("  => no_match on all platforms")
                    continue

                if args.manual:
                    cand = pick_manual(all_cands, job.query)
                    if not cand:
                        evidence[key] = {
                            "key": key,
                            "status": "skipped",
                            "name": it.name,
                        }
                        continue
                else:
                    cand = pick_best_candidate(all_cands)

                if open_detail_flag and cand:
                    spec = reg.get(cand.get("platform") or "", None)
                    extra = list(spec.detail_price_selectors) if spec else []
                    cand = open_detail(page, cand, timeout_ms, extra_price_selectors=extra)
                    title = (cand.get("detail_title") or cand.get("title") or "")
                    if score_title(title, job.must) == 0 and job.confidence == "high":
                        print(f"  reject detail title mismatch: {title[:60]}")
                        evidence[key] = {
                            "key": key,
                            "status": "rejected_title",
                            "name": it.name,
                            "url": cand.get("final_url") or cand.get("url"),
                            "title": title,
                            "platform": cand.get("platform"),
                        }
                        continue

                evidence[key] = to_evidence(key, it, cand, tax, never_exceed)
                # keep alternatives for audit trail
                evidence[key]["alternatives"] = [
                    {
                        "platform": c.get("platform"),
                        "price_tax": c.get("price_tax"),
                        "url": c.get("url"),
                        "score": c.get("score"),
                        "title": (c.get("title") or "")[:80],
                    }
                    for c in all_cands[:8]
                ]
                done += 1
                ev = evidence[key]
                print(
                    f"  => verified [{ev.get('platform')}] 含税¥{ev['price_tax']} "
                    f"→ 不含税¥{ev['price_ex_tax']} → 审定¥{ev['audit']}\n"
                    f"     {ev['url']}"
                )
            except Exception as e:
                print(f"  ERROR {type(e).__name__}: {e}")
                evidence[key] = {
                    "key": key,
                    "status": "error",
                    "name": it.name,
                    "error": str(e),
                }

            if done and done % 3 == 0:
                save_evidence(
                    evidence_path,
                    evidence,
                    meta={
                        "input": str(input_path),
                        "tax_divisor": tax,
                        "platforms": enabled,
                    },
                )
            time.sleep(sleep_s)
    finally:
        ctx.close()
        pw.stop()

    save_evidence(
        evidence_path,
        evidence,
        meta={"input": str(input_path), "tax_divisor": tax, "platforms": enabled},
    )
    hit = write_result_workbook(input_path, output_path, items, evidence, tax)
    print(f"\nDONE verified_in_excel={hit}")
    print(f"output  : {output_path}")
    print(f"evidence: {evidence_path}")
    return 0


def cmd_merge(args):
    ensure_or_exit(require_browser=False)
    cfg = load_config(Path(args.config) if args.config else None)
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    evidence_path = Path(args.evidence).expanduser().resolve()
    if not input_path.exists():
        print(f"ERROR input not found: {input_path}", file=sys.stderr)
        return 2
    if not evidence_path.exists():
        print(f"ERROR evidence not found: {evidence_path}", file=sys.stderr)
        return 2
    items = load_inquiry(input_path, cfg.get("excel") or {})
    evidence = load_evidence(evidence_path)
    tax = float(cfg["pricing"]["tax_divisor"])
    hit = write_result_workbook(input_path, output_path, items, evidence, tax)
    print(f"merged verified={hit} → {output_path}")
    return 0


def cmd_rfq(args):
    ensure_or_exit(require_browser=False)
    cfg = load_config(Path(args.config) if args.config else None)
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    evidence_path = Path(args.evidence).expanduser().resolve() if args.evidence else None
    if not input_path.exists():
        print(f"ERROR input not found: {input_path}", file=sys.stderr)
        return 2
    items = load_inquiry(input_path, cfg.get("excel") or {})
    evidence = load_evidence(evidence_path) if evidence_path else {}
    n = export_rfq(items, evidence, output_path)
    print(f"RFQ rows={n} → {output_path}")
    return 0


def cmd_status(args):
    cfg = load_config(Path(args.config) if args.config else None)
    input_path = Path(args.input).expanduser().resolve() if args.input else None
    evidence_path = Path(args.evidence).expanduser().resolve() if args.evidence else None
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    enabled = resolve_enabled_platforms(cfg, args.platforms or None)
    print("=== status ===")
    print(f"version  : {__version__}")
    print(f"root     : {package_root()}")
    print(f"platforms: {', '.join(enabled)}")
    if input_path:
        print(f"input    : {input_path} exists={input_path.exists()}")
        if input_path.exists():
            items = load_inquiry(input_path, cfg.get("excel") or {})
            n_jobs = len(build_jobs(items, platforms=None))
            print(f"  items={len(items)} matchable={n_jobs}")
    if evidence_path:
        print(f"evidence : {evidence_path} exists={evidence_path.exists()}")
        if evidence_path.exists():
            ev = load_evidence(evidence_path)
            v = sum(1 for e in ev.values() if e.get("status") == "verified")
            print(f"  records={len(ev)} verified={v}")
    if output_path:
        print(f"output   : {output_path} exists={output_path.exists()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="material-price-audit",
        description="Accuracy-first material inquiry price audit (multi-platform Playwright + Excel)",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", default="", help="optional config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", help="检查 Python / Playwright 环境")
    sp.add_argument("--skip-browser", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser(
        "init",
        help="【Agent 入口】初始化目录/config，并输出引导问题与下一步命令",
    )
    sp.add_argument(
        "--platforms",
        default="",
        help="初始化时写入启用平台，如 jd,1688,zkh",
    )
    sp.add_argument("--tax", default="1.13", help="含税÷此数≈不含税，默认 1.13")
    sp.add_argument("--force", action="store_true", help="覆盖已有 config.yaml")
    sp.add_argument(
        "--allow-broken-env",
        action="store_true",
        help="环境不完整也完成脚手架（仍会提示安装）",
    )
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser(
        "guide",
        help="【Agent】根据当前状态刷新 AGENT_NEXT.md，告诉下一步问用户什么",
    )
    sp.set_defaults(func=cmd_guide)

    sp = sub.add_parser("platforms", help="列出内置/自定义平台与当前启用项")
    sp.add_argument("--platforms", default="", help="仅用于预览解析结果")
    sp.set_defaults(func=cmd_platforms)

    sp = sub.add_parser("login", help="按指定平台依次打开登录页")
    sp.add_argument("--profile", required=True, help="浏览器配置目录（勿提交 git）")
    sp.add_argument(
        "--platforms",
        default="",
        help="逗号分隔，如 jd,1688,zkh,taobao（默认读 config）",
    )
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--login-wait", type=int, default=0)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("scrape", help="多平台抓取证据价并输出核价 Excel")
    sp.add_argument("--input", required=True, help="询价单 Excel（入参）")
    sp.add_argument("--output", required=True, help="核价结果 Excel（出参）")
    sp.add_argument("--evidence", required=True, help="证据 JSON（出参）")
    sp.add_argument("--profile", required=True, help="浏览器配置目录")
    sp.add_argument(
        "--platforms",
        default="",
        help="启用平台，逗号分隔：jd,1688,zkh,taobao,tmall,suning 或自定义 id",
    )
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--login-wait", type=int, default=0)
    sp.add_argument("--manual", action="store_true", help="多平台候选人工挑选")
    sp.add_argument("--skip-existing", action="store_true", default=True)
    sp.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    sp.add_argument("--open-detail", dest="open_detail", action="store_true", default=None)
    sp.add_argument("--no-open-detail", dest="open_detail", action="store_false")
    sp.set_defaults(func=cmd_scrape)

    sp = sub.add_parser("merge", help="evidence JSON → Excel")
    sp.add_argument("--input", required=True)
    sp.add_argument("--output", required=True)
    sp.add_argument("--evidence", required=True)
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("rfq", help="导出未 verified 询价单")
    sp.add_argument("--input", required=True)
    sp.add_argument("--output", required=True)
    sp.add_argument("--evidence", default="")
    sp.set_defaults(func=cmd_rfq)

    sp = sub.add_parser("status", help="查看状态")
    sp.add_argument("--input", default="")
    sp.add_argument("--output", default="")
    sp.add_argument("--evidence", default="")
    sp.add_argument("--platforms", default="")
    sp.set_defaults(func=cmd_status)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "config", None):
        cand = package_root() / "config.yaml"
        args.config = str(cand) if cand.exists() else ""
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
