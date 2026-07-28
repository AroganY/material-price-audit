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
from .excel_io import export_rfq, load_inquiry, resolve_inquiry_path, write_result_workbook
from .init_wizard import detect_state, print_agent_block, run_init, write_agent_guide
from .matcher import build_jobs
from .matching import detail_matches_item
from .platforms import (
    load_platform_registry,
    login_urls_for,
    pick_best_candidate,
    pick_platforms_interactive,
    resolve_enabled_platforms,
    save_platforms_selected,
    search_on_platform,
)
from .scraper import (
    agent_login_signal_path,
    clear_agent_login_signal,
    launch_context,
    open_detail,
    pick_manual,
    to_evidence,
    wait_for_login_agent,
)


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
            # 空 = 必须用户选择（--platforms / platforms.selected / 终端勾选）
            "enabled": [],
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
        # 默认快检；--force 才启浏览器。不谈升级 Python。
        r = check_environment(
            require_browser=not args.skip_browser and getattr(args, "force", False),
            force=bool(getattr(args, "force", False)),
            use_cache=not bool(getattr(args, "force", False)),
        )
    r.print(quiet_ok=False)
    print(f"\npackage version: {__version__}")
    print(f"package root   : {package_root()}")
    print("说明: 不自动升级 Python/pip；缺包才装。")
    if not r.ok:
        print("\n========== AGENT_ENV_FAIL ==========")
        print("只装缺失依赖即可，不要升级系统 Python。可执行:")
        print(f"  {sys.executable} -m material_price_audit check --auto-install")
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
    print("说明: 行首 * 表示当前启用。详见 docs/PLATFORMS.md")
    print("广材网 guangcai = https://www.gldjc.com/login")
    print("慧讯网 huixun   = https://services.iccchina.com/login （RCC瑞达恒，非广材）")
    print("领材网 lingcai  = https://www.hylcw.cn/userInfo/index.html")
    print("示例: --platforms guangcai,huixun,lingcai,jd,1688")
    return 0


def _platforms_selected_path(root: Path | None = None) -> Path:
    return (root or package_root()) / "data" / "output" / "platforms.selected"


def resolve_user_platforms(
    cfg: dict,
    cli_platforms: str | None,
    *,
    interactive: bool = True,
    root: Path | None = None,
    force_dialog: bool = False,
) -> list[str]:
    """
    用户显式选择的平台（顺序=优先级）。
    优先级：
      1) CLI --platforms
      2) 交互：弹窗勾选（默认；上次选择预勾）
      3) platforms.selected / config（仅非交互或弹窗不可用时）
    绝不静默默认全站登录。
    """
    root = root or package_root()
    if cli_platforms and str(cli_platforms).strip():
        return resolve_enabled_platforms(cfg, cli_platforms)

    selected_file = _platforms_selected_path(root)
    prev = resolve_enabled_platforms(cfg, _read_platforms_file(selected_file) or None)
    if not prev:
        prev = resolve_enabled_platforms(cfg, None)

    # 交互路径：弹窗让用户选（有上次选择则预勾）
    if interactive or force_dialog:
        reg = load_platform_registry(cfg)
        try:
            picked = pick_platforms_interactive(reg, preselected=prev or None, prefer_dialog=True)
        except Exception as e:
            print(f"[platforms] 选择失败: {e}")
            picked = []
        if picked:
            save_platforms_selected(selected_file, picked)
            print(f"[platforms] 用户勾选: {', '.join(picked)}")
            return picked
        # 用户取消弹窗
        if force_dialog or sys.stdin.isatty():
            print("未选择平台（弹窗已取消）。")
            return []

    # 非交互回退：文件 / config
    from_file = _read_platforms_file(selected_file)
    if from_file:
        ids = resolve_enabled_platforms(cfg, from_file)
        print(f"[platforms] 非交互，使用已选文件 → {', '.join(ids)}")
        return ids
    from_cfg = resolve_enabled_platforms(cfg, None)
    if from_cfg:
        print(f"[platforms] 非交互，使用 config → {', '.join(from_cfg)}")
        return from_cfg

    html = root / "docs" / "platform-select.html"
    print("ERROR: 未选择比价平台。", file=sys.stderr)
    print("  弹窗: python -m material_price_audit select-platforms", file=sys.stderr)
    print("  或:   --platforms jd,1688   （没广材会员就别写 guangcai）", file=sys.stderr)
    print(f"  或网页: {html}", file=sys.stderr)
    return []


def cmd_select_platforms(args):
    """弹窗勾选平台并写入 platforms.selected。"""
    root = package_root()
    cfg = load_config(Path(args.config) if args.config else None)
    reg = load_platform_registry(cfg)
    if args.platforms:
        picked = resolve_enabled_platforms(cfg, args.platforms)
    else:
        prev = resolve_enabled_platforms(cfg, _read_platforms_file(_platforms_selected_path(root)) or None)
        picked = pick_platforms_interactive(reg, preselected=prev or None, prefer_dialog=True)
    if not picked:
        print("未选择任何平台。")
        return 2
    path = _platforms_selected_path(root)
    save_platforms_selected(path, picked)
    print(f"已保存: {path}")
    print(f"平台  : {', '.join(picked)}")
    print("只会登录你勾的站。没广材会员就别勾广材网。")
    if getattr(args, "write_config", False):
        run_init(root=root, platforms=picked, force_config=True)
        print(f"已写 config.yaml enabled: {', '.join(picked)}")
    print("下一步: python -m material_price_audit run")
    return 0


def _login_timeout_s(args, cfg: dict) -> int:
    """登录等待超时上限（秒）。成功/Agent 信号会立刻返回。默认 600。"""
    w = getattr(args, "login_wait", None)
    if w is not None and int(w) > 0:
        return int(w)
    b = cfg.get("browser") or {}
    return int(
        b.get("login_timeout_seconds")
        or b.get("login_wait_seconds")
        or 600
    )


def _do_platform_logins(
    page,
    urls: list[tuple[str, str, str]],
    *,
    root: Path,
    timeout_s: int,
    timeout_ms: int = 60000,
) -> set[str]:
    """
    每个站最多 goto 一次，然后被动等（URL / Agent 信号 / 回车）。
    绝不在等待中刷新页面。返回本会话视为已处理的 platform id。
    """
    done: set[str] = set()
    clear_agent_login_signal(root)
    n = len(urls)
    for i, (pid, name, url) in enumerate(urls, 1):
        print(f"\n[{i}/{n}] {name} ({pid})")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            print(f"  [{name}] 打开失败: {e}（仍等待 Agent 确认）")
        st = wait_for_login_agent(
            page,
            platform_id=pid,
            name=name,
            login_url=url,
            package_root=root,
            timeout_s=timeout_s,
            allow_stdin=True,
        )
        print(f"  [{name}] 登录阶段结束: {st}")
        done.add(pid)
    return done


def cmd_login(args):
    ensure_or_exit(require_browser=False, quiet=True)
    cfg = load_config(Path(args.config) if args.config else None)
    root = package_root()
    profile = Path(args.profile).expanduser().resolve()
    timeout_s = _login_timeout_s(args, cfg)

    enabled = resolve_user_platforms(
        cfg, args.platforms or None, interactive=not args.yes, root=root
    )
    if not enabled:
        return 2
    reg = load_platform_registry(cfg)
    urls = login_urls_for(enabled, reg)
    if not urls:
        print("ERROR: 没有可登录的平台 URL", file=sys.stderr)
        return 2

    print(f"只打开你选的 {len(urls)} 个登录页各 1 次；登录后不刷新。")
    print(f"Agent 确认信号文件: {agent_login_signal_path(root)}")
    for pid, name, url in urls:
        print(f"  - [{pid}] {name}: {url}")

    pw, ctx, page = launch_context(
        profile, channel=cfg["browser"]["channel"], headless=False
    )
    try:
        _do_platform_logins(page, urls, root=root, timeout_s=timeout_s)
        print(f"\n登录流程结束。profile={profile}")
        print("下一步抓取请加 --skip-login，避免再进登录预检：")
        print(
            "  python -m material_price_audit run --skip-login --platforms "
            + ",".join(enabled)
        )
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
    # 快检 import，不反复升包/启浏览器
    ensure_or_exit(require_browser=False, auto_install=auto_install, quiet=True)
    cfg = load_config(Path(args.config) if args.config else None)
    root = package_root()

    try:
        input_path = resolve_inquiry_path(
            args.input or None,
            default_dir=root / "data" / "input",
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    output_path = Path(args.output).expanduser().resolve()
    evidence_path = Path(args.evidence).expanduser().resolve()
    profile = Path(args.profile).expanduser().resolve()

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
    # 登录超时上限（自动检测，不是固定 sleep）
    login_timeout = _login_timeout_s(args, cfg)
    non_interactive = not bool(getattr(args, "interactive", False))
    if getattr(args, "yes", False):
        non_interactive = True

    enabled = resolve_user_platforms(
        cfg,
        args.platforms or None,
        interactive=not non_interactive or not getattr(args, "yes", False),
        root=root,
    )
    # --yes 且非 TTY 时 interactive 可能已关；再允许一次若仍空则失败
    if not enabled and sys.stdin.isatty() and not getattr(args, "yes", False):
        enabled = resolve_user_platforms(cfg, None, interactive=True, root=root)
    if not enabled:
        return 2
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
    # 本会话：已登录处理 / 永久跳过（无会员、登录失败）
    session_login_done: set[str] = set()
    session_skip_platforms: set[str] = set()  # no_membership / dead login
    try:
        if not skip_login:
            urls = login_urls_for(enabled, reg)
            print(
                f"登录预检：每个站最多打开 1 次；被动等 URL/Agent 信号，绝不循环刷新。"
            )
            print(f"Agent 确认: touch {agent_login_signal_path(root)}")
            print("提示: 没广材/慧讯会员就别勾它们；登录页可直接关，程序会跳过该站。")
            for pid, name, url in urls:
                print(f"  · {name}  {url}")
            session_login_done |= _do_platform_logins(
                page, urls, root=root, timeout_s=login_timeout, timeout_ms=timeout_ms
            )
            print("登录预检结束 → 开始抓取（无会员站会自动跳过）")
        else:
            print("[login] --skip-login：不打开登录页，直接抓取")

        done = 0
        for job in jobs:
            it = job.item
            key = it.key
            if args.skip_existing and evidence.get(key, {}).get("status") == "verified":
                print(f"skip {it.name[:32]}")
                continue

            order = [
                p
                for p in _platform_order(job.platform, enabled, strategy)
                if p not in session_skip_platforms
            ]
            print(f"→ [{it.sheet}] {it.name[:40]}")
            print(f"   waterfall: {' → '.join(order) if order else '(无可用平台)'}")

            attempts = []
            chosen = None
            try:
                if strategy == "waterfall":
                    # A then B then C: first platform with detail match wins
                    for pid in order:
                        if pid in session_skip_platforms:
                            continue
                        cands, st = search_on_platform(
                            page, pid, job.query, job.must, timeout_ms, min_score, reg
                        )
                        if st == "no_membership":
                            session_skip_platforms.add(pid)
                            attempts.append({"platform": pid, "status": "no_membership"})
                            print(
                                f"   [{pid}] 无会员/无权限 → 本会话跳过该站，改试下一平台"
                            )
                            continue
                        if st == "need_login":
                            if pid in session_login_done:
                                # 已处理过登录仍要登 → 当无权限/无会员，跳过整站
                                session_skip_platforms.add(pid)
                                attempts.append({"platform": pid, "status": "need_login_skip"})
                                print(
                                    f"   [{pid}] 仍需登录（可能无会员）→ 跳过该站，不刷新死等"
                                )
                                continue
                            spec = reg.get(pid)
                            login_url = (spec.login_url if spec else "") or ""
                            print(
                                f"   [{pid}] 需要登录：打开 1 次。"
                                f"没账号/没会员可关页面或 touch LOGIN_CONTINUE 跳过"
                            )
                            if login_url:
                                try:
                                    page.goto(
                                        login_url,
                                        wait_until="domcontentloaded",
                                        timeout=timeout_ms,
                                    )
                                except Exception as e:
                                    print(f"   [{pid}] 打开登录页失败: {e}")
                            st_login = wait_for_login_agent(
                                page,
                                platform_id=pid,
                                name=spec.name if spec else pid,
                                login_url=login_url or page.url,
                                package_root=root,
                                timeout_s=min(login_timeout, 120),  # 别死等 10 分钟
                            )
                            session_login_done.add(pid)
                            if st_login == "timeout":
                                session_skip_platforms.add(pid)
                                print(f"   [{pid}] 登录超时 → 跳过该站")
                                attempts.append({"platform": pid, "status": "login_timeout"})
                                continue
                            cands, st = search_on_platform(
                                page, pid, job.query, job.must, timeout_ms, min_score, reg
                            )
                            if st in ("need_login", "no_membership"):
                                session_skip_platforms.add(pid)
                                print(f"   [{pid}] 登录后仍不可用({st}) → 跳过该站")
                                attempts.append({"platform": pid, "status": st})
                                continue
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
                        if pid in session_skip_platforms:
                            continue
                        cands, st = search_on_platform(
                            page, pid, job.query, job.must, timeout_ms, min_score, reg
                        )
                        if st in ("no_membership", "need_login") and pid in session_login_done:
                            session_skip_platforms.add(pid)
                            print(f"   [{pid}] {st} → 跳过该站")
                            continue
                        if st == "no_membership":
                            session_skip_platforms.add(pid)
                            print(f"   [{pid}] 无会员 → 跳过该站")
                            continue
                        if st == "need_login" and pid not in session_login_done:
                            session_skip_platforms.add(pid)  # multi 模式不打断流程等登录
                            print(f"   [{pid}] 需登录 → multi 模式跳过，请先 login")
                            continue
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
    One-shot:
      轻量 env → 用户已选平台 → 只登录所选 → 瀑布抓取 → rfq
    不默认全站、不升级 Python。
    """
    root = package_root()
    auto_install = bool(args.auto_install)

    print("=== RUN ===")
    try:
        ensure_or_exit(require_browser=False, auto_install=auto_install, quiet=True)
    except SystemExit:
        print("缺依赖时: python -m material_price_audit check --auto-install")
        return 2

    cfg = load_config(Path(args.config) if args.config else None)
    # 平台：有 --platforms 用 CLI；否则弹窗勾选（即使用 --yes 也尽量弹窗，Agent 可关）
    want_dialog = not bool(args.platforms)
    enabled = resolve_user_platforms(
        cfg,
        args.platforms or None,
        interactive=want_dialog,
        force_dialog=want_dialog,
        root=root,
    )
    if not enabled:
        return 2

    plat = ",".join(enabled)
    print(f"platforms（仅这些会登录）: {plat}")

    # 写入 selected + config.enabled，方便下次
    save_platforms_selected(_platforms_selected_path(root), enabled)
    run_init(root=root, platforms=enabled, force_config=bool(args.force_config))

    try:
        input_path = resolve_inquiry_path(
            args.input or None,
            default_dir=root / "data" / "input",
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        print("把任意文件名的询价 .xlsx 丢进 data/input/ 即可")
        return 2

    output_path = Path(args.output or (root / "data/output/result.xlsx")).expanduser().resolve()
    evidence_path = Path(args.evidence or (root / "data/output/evidence.json")).expanduser().resolve()
    profile = Path(args.profile or (root / ".browser-profile")).expanduser().resolve()
    rfq_path = Path(args.rfq or (root / "data/output/rfq.xlsx")).expanduser().resolve()

    # login_wait = 自动检测超时上限（秒），不是固定傻等
    use_yes = bool(getattr(args, "yes", False)) or not sys.stdin.isatty()
    a = type("A", (), {})()
    a.input = str(input_path)
    a.output = str(output_path)
    a.evidence = str(evidence_path)
    a.profile = str(profile)
    a.platforms = plat
    a.limit = args.limit or 0
    a.login_wait = int(args.login_wait or 0)  # 0 → 默认 180s 上限，检测成功立刻走
    a.yes = use_yes
    a.interactive = False  # 登录靠自动检测，不靠回车
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
    ensure_or_exit(require_browser=False, quiet=True)
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

    sp = sub.add_parser("check", help="轻量检查依赖（不升级 Python；默认不启浏览器）")
    sp.add_argument("--skip-browser", action="store_true", help="兼容旧参数")
    sp.add_argument(
        "--force",
        action="store_true",
        help="强制重检并尝试启动浏览器（日常 run 不需要）",
    )
    sp.add_argument(
        "--auto-install",
        action="store_true",
        help="仅安装缺失包，不 upgrade pip/Python",
    )
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

    sp = sub.add_parser(
        "select-platforms",
        help="【弹窗】勾选比价平台，写入 platforms.selected（只登录所选）",
    )
    sp.add_argument(
        "--platforms",
        default="",
        help="非交互直接写入，如 guangcai,jd（跳过勾选菜单）",
    )
    sp.add_argument(
        "--write-config",
        action="store_true",
        help="同时写回 config.yaml enabled",
    )
    sp.set_defaults(func=cmd_select_platforms)

    sp = sub.add_parser("login", help="只打开「已选平台」的登录页（不是全站挨个登）")
    sp.add_argument("--profile", required=True, help="浏览器配置目录（勿提交 git）")
    sp.add_argument(
        "--platforms",
        default="",
        help="逗号分隔，如 guangcai,jd；不传则读 platforms.selected / 交互勾选",
    )
    sp.add_argument("--yes", action="store_true", help="非交互（仍自动检测登录）")
    sp.add_argument(
        "--login-wait",
        type=int,
        default=0,
        help="自动检测登录的超时上限秒（默认180，成功立刻下一站）",
    )
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("scrape", help="瀑布抓取：A平台详情匹配才用，否则自动B→C")
    sp.add_argument(
        "--input",
        default="",
        help="询价 Excel 文件或目录；默认自动识别 data/input/ 下任意 .xlsx（不必叫 inquiry）",
    )
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
    sp.add_argument(
        "--login-wait",
        type=int,
        default=0,
        help="登录自动检测超时上限秒数（默认180；成功立刻继续，不是傻等）",
    )
    sp.add_argument("--skip-login", action="store_true", help="跳过登录预检（确定会话仍有效时）")
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
        help="一键：轻量环境→用户已选平台→只登录所选→抓取→RFQ",
    )
    sp.add_argument(
        "--input",
        default="",
        help="询价表路径或目录；默认自动识别 data/input/ 内任意 .xlsx",
    )
    sp.add_argument("--output", default="", help="默认 data/output/result.xlsx")
    sp.add_argument("--evidence", default="", help="默认 data/output/evidence.json")
    sp.add_argument("--rfq", default="", help="默认 data/output/rfq.xlsx")
    sp.add_argument("--profile", default="", help="默认 .browser-profile")
    sp.add_argument(
        "--platforms",
        default="",
        help="必选：如 guangcai,jd。不传则读 platforms.selected 或终端勾选（绝不默认全站）",
    )
    sp.add_argument("--limit", type=int, default=0, help="试跑条数，0=全量")
    sp.add_argument(
        "--login-wait",
        type=int,
        default=0,
        help="登录自动检测超时上限秒（默认180；检测到登录立刻继续）",
    )
    sp.add_argument("--yes", action="store_true", help="非交互（Agent 用）")
    sp.add_argument("--skip-login", action="store_true", help="跳过登录预检")
    sp.add_argument(
        "--auto-install",
        action="store_true",
        help="仅补缺失包，不升级 Python",
    )
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
