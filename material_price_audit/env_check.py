"""Environment checks for Playwright / Python deps."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class CheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    def print(self) -> None:
        print("=== 环境检查 Environment Check ===")
        print(f"Python : {sys.version.split()[0]} ({sys.executable})")
        if self.ok:
            print("状态   : OK — 可运行 scrape / login")
        else:
            print("状态   : FAIL — 请先安装缺失依赖")
        for e in self.errors:
            print(f"  [ERROR] {e}")
        for w in self.warnings:
            print(f"  [WARN]  {w}")
        if self.hints:
            print("\n安装建议:")
            for h in self.hints:
                print(f"  {h}")


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_environment(require_browser: bool = True) -> CheckResult:
    res = CheckResult(ok=True)

    if sys.version_info < (3, 10):
        res.ok = False
        res.errors.append(f"需要 Python >= 3.10，当前 {sys.version_info.major}.{sys.version_info.minor}")

    if not _has_module("openpyxl"):
        res.ok = False
        res.errors.append("缺少 openpyxl")
        res.hints.append(f"{sys.executable} -m pip install openpyxl")

    if not _has_module("yaml"):
        res.ok = False
        res.errors.append("缺少 PyYAML")
        res.hints.append(f"{sys.executable} -m pip install PyYAML")

    if not _has_module("playwright"):
        res.ok = False
        res.errors.append("缺少 playwright Python 包")
        res.hints.append(f"{sys.executable} -m pip install playwright")
        res.hints.append(f"{sys.executable} -m playwright install chromium")
        res.hints.append("（可选）安装本机 Chrome 后可用 channel=chrome")
        return res

    if require_browser:
        # Check playwright browsers
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                # chromium always installable via playwright
                try:
                    browser = p.chromium.launch(headless=True)
                    browser.close()
                except Exception as e:
                    # try chrome channel
                    try:
                        browser = p.chromium.launch(channel="chrome", headless=True)
                        browser.close()
                        res.warnings.append(
                            "Playwright 自带 chromium 不可用，但检测到本机 Chrome，将使用 channel=chrome"
                        )
                    except Exception:
                        res.ok = False
                        res.errors.append(f"无法启动浏览器: {e}")
                        res.hints.append(f"{sys.executable} -m playwright install chromium")
                        res.hints.append("或安装 Google Chrome 并在 config 中设置 browser.channel: chrome")
        except Exception as e:
            res.ok = False
            res.errors.append(f"Playwright 初始化失败: {e}")
            res.hints.append(f"{sys.executable} -m playwright install chromium")

    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    # macOS Chrome path not always in PATH
    from pathlib import Path

    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac_chrome.exists() or chrome:
        res.warnings.append("检测到本机 Chrome，推荐 channel=chrome（登录更稳）")
    else:
        res.warnings.append("未检测到本机 Chrome，将使用 Playwright Chromium")

    if not res.errors:
        res.ok = True
    return res


def ensure_or_exit(require_browser: bool = True) -> None:
    r = check_environment(require_browser=require_browser)
    r.print()
    if not r.ok:
        print("\n请安装环境后重试。Accuracy-first：未就绪不会启动抓取。", file=sys.stderr)
        raise SystemExit(2)
