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
from .matching import detail_matches_item
from .platforms import (
    load_platform_registry,
    login_urls_for,
    pick_best_candidate,
    resolve_enabled_platforms,
    search_on_platform,
)
from .scraper import launch_context, open_detail, pick_manual, to_evidence, wait_user


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: Path | None) -> dict:
    defaults = {
        "pricing": {
            "tax_divisor": 1.13,
            "never_exceed_submit": True,
            "open_detail": True,
            "min_title_score": 1,
            # waterfall: A 详情规格匹配才采用，否则自动 B→C…
            # multi: 搜全部再取综合最优（旧行为）
            "platform_strategy": "waterfall",
            "detail_match_min_score": 0.55,
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
    if getattr(args, "auto_install", False):
        from .env_check import try_auto_install

        root = package_root()
        r = try_auto_install(root / "requirements.txt")
    else:
        r = check_environment(require_browser=not args.skip_browser)
    r.print()
    print(f"\npackage version: {__version__}")
    print(f"package root   : {package_root()}")
    if not r.ok:
        print("\n========== AGENT_ENV_FAIL ==========")
        print("请安装 Python3.10+ 与依赖后重试。可执行:")
        print(f"  {sys.executable} -m material_price_audit check --auto-install")
        print("或:")
        for h in r.hints:
            print(f"  {h}")
        print("========== AGENT_ENV_FAIL_END ==========")
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
    """Order platforms to try for one item. Waterfall uses enabled list as priority A→B→C."""
    if strategy == "waterfall":
        return list(enabled)
    if strategy == "preferred" and job_platform in enabled:
        return [job_platform] + [p for p in enabled if p != job_platform]
    if job_platform in enabled:
        return [job_platform] + [p for p in enabled if p != job_platform]
    return list(enabled)


def _read_platforms_file(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    # allow lines or comma-separated
    parts = []
    for line in text.replace(",", "\n").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            parts.append(line)
    return ",".join(parts)


def cmd_scrape(args):
    auto_install = bool(getattr(args, "auto_install", False))
    ensure_or_exit(require_browser=True, auto_install=auto_install)
    cfg = load_config(Path(args.config) if args.config else None)

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    evidence_path = Path(args.evidence).expanduser().resolve()
    profile = Path(args.profile).expanduser().resolve()

    if not input_path.exists():
        print(f"ERROR: 询价单不存在:\n  {input_path}", file=sys.stderr)
        print("请把询价表放到 data/input/inquiry.xlsx 或改 --input", file=sys.stderr)
        return 2

    tax = float(cfg["pricing"]["tax_divisor"])
    never_exceed = bool(cfg["pricing"]["never_exceed_submit"])
    open_detail_flag = (
        bool(cfg["pricing"]["open_detail"]) if args.open_detail is None else args.open_detail
    )
    # waterfall always opens detail for match
    strategy = str(
        getattr(args, "strategy", None)
        or cfg["pricing"].get("platform_strategy")
        or "waterfall"
    )
    if strategy == "waterfall":
        open_detail_flag = True
    min_score = int(cfg["pricing"].get("min_title_score", 1))
    detail_min = float(cfg["pricing"].get("detail_match_min_score", 0.55))
    timeout_ms = int(cfg["browser"]["page_timeout_ms"])
    sleep_s = float(cfg["browser"]["between_items_sleep"])
    wait_s = args.login_wait or cfg["browser"]["login_wait_seconds"]
    # non-interactive by default for automation unless --interactive
    non_interactive = not bool(getattr(args, "interactive", False))
    if getattr(args, "yes", False):
        non_interactive = True

    plat_arg = args.platforms or ""
    if not plat_arg:
        # optional file from HTML multi-select
        pf = package_root() / "data" / "output" / "platforms.selected"
        plat_arg = _read_platforms_file(pf) or None
    enabled = resolve_enabled_platforms(cfg, plat_arg)
    reg = load_platform_registry(cfg)
    unknown = [p for p in enabled if p not in reg]
    if unknown:
        print(f"ERROR: 未知平台 {unknown}。运行: python -m material_price_audit platforms", file=sys.stderr)
        return 2

    print("=== scrape (auto waterfall) ===")
    print(f"input    : {input_path}")
    print(f"output   : {output_path}")
    print(f"evidence : {evidence_path}")
    print(f"platforms: {', '.join(enabled)}")
    print(f"strategy : {strategy}  (A详情型号匹配→采用，否则自动B→C…)")
    print(f"tax      : /{tax}  never_exceed={never_exceed}")

    items = load_inquiry(input_path, cfg.get("excel") or {})
    jobs = build_jobs(items, platforms=None)
    if args.limit and args.limit > 0:
        jobs = jobs[: args.limit]

    evidence = load_evidence(evidence_path)
    print(
        f"items={len(items)} jobs={len(jobs)} verified="
        f"{sum(1 for e in evidence.values() if e.get('status')=='verified')}"
    )

    if not jobs:
        print("WARNING: 无可自动匹配项，生成 pending 结果 + 建议 rfq")
        write_result_workbook(input_path, output_path, items, evidence, tax)
        return 0

    skip_login = bool(getattr(args, "skip_login", False))
    pw, ctx, page = launch_context(
        profile, channel=cfg["browser"]["channel"], headless=bool(cfg["browser"]["headless"])
    )
    try:
        if not skip_login:
            print("打开各平台登录页（只需登录一次；已登录可直接等超时/回车）…")
            for pid, name, url in login_urls_for(enabled, reg):
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                wait_user(
                    f"【{name}】登录后继续（自动化模式将等待 {wait_s if non_interactive else '回车'}）",
                    wait_s if non_interactive else 0,
                    non_interactive,
                )

        done = 0
        for job in jobs:
            it = job.item
            key = it.key
            if args.skip_existing and evidence.get(key, {}).get("status") == "verified":
                print(f"skip {it.name[:32]}")
                continue

            order = _platform_order(job.platform, enabled, strategy)
            print(f"→ [{it.sheet}] {it.name[:40]}")
            print(f"   waterfall: {' → '.join(order)}")

            attempts = []
            chosen = None
            try:
                if strategy == "waterfall":
                    # A then B then C: first platform with detail match wins
                    for pid in order:
                        cands, st = search_on_platform(
                            page, pid, job.query, job.must, timeout_ms, min_score, reg
                        )
                        if st == "need_login":
                            wait_user(f"【{pid}】需要登录", wait_s, non_interactive)
                            cands, st = search_on_platform(
                                page, pid, job.query, job.must, timeout_ms, min_score, reg
                            )
                        if not cands:
                            attempts.append({"platform": pid, "status": st or "no_list"})
                            print(f"   [{pid}] 列表无结果，切换下一平台")
                            continue
                        # try top candidates on this platform until detail matches
                        platform_ok = False
                        for cand in cands[:5]:
                            spec = reg.get(pid)
                            extra = list(spec.detail_price_selectors) if spec else []
                            cand = open_detail(
                                page, cand, timeout_ms, extra_price_selectors=extra
                            )
                            title = cand.get("detail_title") or cand.get("title") or ""
                            # snippet of body for match
                            try:
                                body = page.inner_text("body")[:2500]
                            except Exception:
                                body = ""
                            mr = detail_matches_item(it, title, body, min_score=detail_min)
                            attempts.append(
                                {
                                    "platform": pid,
                                    "url": cand.get("final_url") or cand.get("url"),
                                    "price_tax": cand.get("price_tax"),
                                    "match_ok": mr.ok,
                                    "match_score": round(mr.score, 3),
                                    "match_detail": mr.detail,
                                    "title": title[:80],
                                }
                            )
                            if mr.ok and cand.get("price_tax"):
                                chosen = cand
                                chosen["match"] = mr.detail
                                platform_ok = True
                                print(
                                    f"   [{pid}] ✓ 详情匹配 {mr.detail} ¥{cand['price_tax']}"
                                )
                                break
                            print(f"   [{pid}] × 详情不匹配 ({mr.detail})，试下一条/下一站")
                        if platform_ok:
                            break
                else:
                    # legacy multi: collect then pick best, still detail-check
                    all_cands = []
                    for pid in order:
                        cands, st = search_on_platform(
                            page, pid, job.query, job.must, timeout_ms, min_score, reg
                        )
                        if st == "need_login":
                            wait_user(f"【{pid}】需要登录", wait_s, non_interactive)
                            cands, st = search_on_platform(
                                page, pid, job.query, job.must, timeout_ms, min_score, reg
                            )
                        if cands:
                            all_cands.extend(cands[:5])
                    if args.manual and all_cands:
                        chosen = pick_manual(all_cands, job.query)
                    elif all_cands:
                        for cand in sorted(
                            all_cands, key=lambda x: (-x.get("score", 0), x.get("price_tax", 1e9))
                        ):
                            pid = cand.get("platform")
                            spec = reg.get(pid or "")
                            extra = list(spec.detail_price_selectors) if spec else []
                            cand = open_detail(
                                page, cand, timeout_ms, extra_price_selectors=extra
                            )
                            title = cand.get("detail_title") or cand.get("title") or ""
                            try:
                                body = page.inner_text("body")[:2500]
                            except Exception:
                                body = ""
                            mr = detail_matches_item(it, title, body, min_score=detail_min)
                            if mr.ok:
                                chosen = cand
                                break

                if not chosen:
                    evidence[key] = {
                        "key": key,
                        "status": "no_match",
                        "name": it.name,
                        "query": job.query,
                        "platforms_tried": order,
                        "attempts": attempts,
                    }
                    print("   => 所有平台均无「规格/型号匹配的详情价」")
                    continue

                evidence[key] = to_evidence(key, it, chosen, tax, never_exceed)
                evidence[key]["attempts"] = attempts
                evidence[key]["strategy"] = strategy
                done += 1
                ev = evidence[key]
                print(
                    f"   => verified [{ev.get('platform')}] "
                    f"含税¥{ev['price_tax']} → 审定¥{ev['audit']}\n"
                    f"      {ev['url']}"
                )
            except Exception as e:
                print(f"   ERROR {type(e).__name__}: {e}")
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
                    meta={"input": str(input_path), "platforms": enabled, "strategy": strategy},
                )
            time.sleep(sleep_s)
    finally:
        ctx.close()
        pw.stop()

    save_evidence(
        evidence_path,
        evidence,
        meta={"input": str(input_path), "tax_divisor": tax, "platforms": enabled, "strategy": strategy},
    )
    hit = write_result_workbook(input_path, output_path, items, evidence, tax)
    # also write rfq automatically if requested later by run
    print(f"\nDONE verified={hit} / jobs={len(jobs)}")
    print(f"output  : {output_path}")
    print(f"evidence: {evidence_path}")
    return 0


def cmd_run(args):
    """
    One-shot automation:
      env check (+ optional auto-install) → init → login once → waterfall scrape → rfq
    """
    root = package_root()
    auto_install = bool(args.auto_install)

    # resolve platforms from args or HTML selection file
    plat = args.platforms or _read_platforms_file(root / "data" / "output" / "platforms.selected")
    if not plat:
        plat = "guangcai,huixun,lingcai,jd,1688"

    print("=== RUN 全自动流水线 ===")
    print(f"platforms: {plat}")

    # env
    try:
        ensure_or_exit(require_browser=True, auto_install=auto_install)
    except SystemExit:
        print("环境失败。Agent 请执行安装 hints 后重跑: python -m material_price_audit run --auto-install ...")
        return 2

    # init scaffold + config
    plats = [p.strip() for p in plat.split(",") if p.strip()]
    run_init(root=root, platforms=plats, force_config=bool(args.force_config))

    input_path = Path(args.input or (root / "data/input/inquiry.xlsx")).expanduser().resolve()
    output_path = Path(args.output or (root / "data/output/result.xlsx")).expanduser().resolve()
    evidence_path = Path(args.evidence or (root / "data/output/evidence.json")).expanduser().resolve()
    profile = Path(args.profile or (root / ".browser-profile")).expanduser().resolve()
    rfq_path = Path(args.rfq or (root / "data/output/rfq.xlsx")).expanduser().resolve()

    if not input_path.exists():
        print(f"ERROR: 找不到询价单 {input_path}")
        print("请把 Excel 放到 data/input/inquiry.xlsx 后重新 run（无需再逐步问答）")
        # write platforms file helper message
        print(f"平台已写入配置: {plat}")
        print("网页多选平台: 打开 docs/platform-select.html")
        return 2

    # fake args namespace for scrape
    class A:
        pass

    a = A()
    a.input = str(input_path)
    a.output = str(output_path)
    a.evidence = str(evidence_path)
    a.profile = str(profile)
    a.platforms = plat
    a.limit = args.limit or 0
    a.login_wait = args.login_wait if args.login_wait else 90
    a.yes = True
    a.interactive = False
    a.skip_existing = not args.no_skip_existing
    a.skip_login = bool(args.skip_login)
    a.open_detail = True
    a.manual = False
    a.auto_install = auto_install
    a.strategy = "waterfall"
    a.config = args.config or str(root / "config.yaml")

    rc = cmd_scrape(a)
    if rc != 0:
        return rc

    # rfq
    cfg = load_config(Path(a.config) if a.config else None)
    items = load_inquiry(input_path, cfg.get("excel") or {})
    evidence = load_evidence(evidence_path)
    n = export_rfq(items, evidence, rfq_path)
    print(f"RFQ 未命中项: {n} → {rfq_path}")
    print("\n=== RUN 完成 ===")
    print(f"结果: {output_path}")
    print(f"证据: {evidence_path}")
    print(f"RFQ : {rfq_path}")
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
    sp.add_argument("--auto-install", action="store_true", help="缺失依赖时自动 pip/playwright 安装")
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

    sp = sub.add_parser("scrape", help="瀑布抓取：A平台详情匹配才用，否则自动B→C")
    sp.add_argument("--input", required=True, help="询价单 Excel（入参）")
    sp.add_argument("--output", required=True, help="核价结果 Excel（出参）")
    sp.add_argument("--evidence", required=True, help="证据 JSON（出参）")
    sp.add_argument("--profile", required=True, help="浏览器配置目录")
    sp.add_argument(
        "--platforms",
        default="",
        help="平台优先级A,B,C… 如 guangcai,huixun,lingcai,jd,1688",
    )
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--yes", action="store_true", help="非交互（默认推荐）")
    sp.add_argument("--interactive", action="store_true", help="每步回车确认")
    sp.add_argument("--login-wait", type=int, default=0, help="每平台登录等待秒数")
    sp.add_argument("--skip-login", action="store_true", help="跳过登录页（已登录时）")
    sp.add_argument("--manual", action="store_true", help="人工挑选（关闭瀑布自动）")
    sp.add_argument("--strategy", default="", help="waterfall|multi|preferred")
    sp.add_argument("--auto-install", action="store_true")
    sp.add_argument("--skip-existing", action="store_true", default=True)
    sp.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    sp.add_argument("--open-detail", dest="open_detail", action="store_true", default=None)
    sp.add_argument("--no-open-detail", dest="open_detail", action="store_false")
    sp.set_defaults(func=cmd_scrape)

    sp = sub.add_parser(
        "run",
        help="【推荐】一键自动化：环境自检→配置→登录等待→瀑布抓取→导出RFQ",
    )
    sp.add_argument("--input", default="", help="默认 data/input/inquiry.xlsx")
    sp.add_argument("--output", default="", help="默认 data/output/result.xlsx")
    sp.add_argument("--evidence", default="", help="默认 data/output/evidence.json")
    sp.add_argument("--rfq", default="", help="默认 data/output/rfq.xlsx")
    sp.add_argument("--profile", default="", help="默认 .browser-profile")
    sp.add_argument(
        "--platforms",
        default="",
        help="优先级列表；也可先用 docs/platform-select.html 勾选生成 platforms.selected",
    )
    sp.add_argument("--limit", type=int, default=0, help="试跑条数，0=全量")
    sp.add_argument("--login-wait", type=int, default=90, help="每平台登录等待秒数")
    sp.add_argument("--skip-login", action="store_true")
    sp.add_argument("--auto-install", action="store_true", help="缺依赖自动 pip/playwright")
    sp.add_argument("--force-config", action="store_true")
    sp.add_argument("--no-skip-existing", action="store_true")
    sp.set_defaults(func=cmd_run)

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
