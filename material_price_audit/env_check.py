"""Environment checks for Python / Playwright. Agent-friendly auto-install."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    python_exe: str = ""

    def print(self) -> None:
        print("=== 环境检查 Environment Check ===")
        print(f"Python : {sys.version.split()[0]} ({sys.executable})")
        if self.ok:
            print("状态   : OK — 可运行自动化流程")
        else:
            print("状态   : FAIL — 请先安装缺失依赖")
        for e in self.errors:
            print(f"  [ERROR] {e}")
        for w in self.warnings:
            print(f"  [WARN]  {w}")
        if self.hints:
            print("\n安装建议 / Install hints:")
            for h in self.hints:
                print(f"  {h}")


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def find_system_python() -> str | None:
    """Prefer python3, then python — for agent messages when current process is broken."""
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            try:
                out = subprocess.check_output(
                    [p, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                    text=True,
                    timeout=5,
                ).strip()
                major, minor = map(int, out.split(".")[:2])
                if (major, minor) >= (3, 10):
                    return p
            except Exception:
                continue
    return None


def check_environment(require_browser: bool = True) -> CheckResult:
    res = CheckResult(ok=True, python_exe=sys.executable)

    # detect if running under weird embedded python without pip
    if sys.version_info < (3, 10):
        res.ok = False
        res.errors.append(
            f"需要 Python >= 3.10，当前 {sys.version_info.major}.{sys.version_info.minor}"
        )
        alt = find_system_python()
        if alt:
            res.hints.append(f"请改用: {alt} -m material_price_audit ...")
        else:
            res.hints.append("请安装 Python 3.10+：https://www.python.org/downloads/")
            res.hints.append("macOS: brew install python@3.12")
            res.hints.append("Windows: 从 python.org 安装并勾选 Add to PATH")

    if not shutil.which("python3") and not shutil.which("python"):
        # rare when launched via absolute path only
        res.warnings.append("PATH 中未找到 python3/python 命令，Agent 请使用绝对路径调用当前解释器")

    missing_pkgs = []
    if not _has_module("openpyxl"):
        missing_pkgs.append("openpyxl")
    if not _has_module("yaml"):
        missing_pkgs.append("PyYAML")
    if not _has_module("playwright"):
        missing_pkgs.append("playwright")

    if missing_pkgs:
        res.ok = False
        res.errors.append("缺少 Python 包: " + ", ".join(missing_pkgs))
        res.hints.append(f"{sys.executable} -m pip install -r requirements.txt")
        res.hints.append(f"{sys.executable} -m pip install {' '.join(missing_pkgs)}")

    if "playwright" in (missing_pkgs or []) or not _has_module("playwright"):
        res.hints.append(f"{sys.executable} -m playwright install chromium")
        return res

    if require_browser:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True)
                    browser.close()
                except Exception as e:
                    try:
                        browser = p.chromium.launch(channel="chrome", headless=True)
                        browser.close()
                        res.warnings.append("将使用本机 Chrome (channel=chrome)")
                    except Exception:
                        res.ok = False
                        res.errors.append(f"无法启动浏览器: {e}")
                        res.hints.append(f"{sys.executable} -m playwright install chromium")
                        res.hints.append("或安装 Google Chrome")
        except Exception as e:
            res.ok = False
            res.errors.append(f"Playwright 初始化失败: {e}")
            res.hints.append(f"{sys.executable} -m playwright install chromium")

    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac_chrome.exists() or shutil.which("google-chrome") or shutil.which("chromium"):
        res.warnings.append("检测到本机 Chrome，推荐 channel=chrome")
    else:
        res.warnings.append("未检测到本机 Chrome，将使用 Playwright Chromium")

    if not res.errors:
        res.ok = True
    return res


def try_auto_install(requirements_file: Path | None = None) -> CheckResult:
    """Attempt pip install + playwright install. Returns fresh check result."""
    print("=== 尝试自动安装依赖 auto-install ===")
    py = sys.executable
    cmds = [
        [py, "-m", "pip", "install", "--upgrade", "pip"],
    ]
    if requirements_file and requirements_file.exists():
        cmds.append([py, "-m", "pip", "install", "-r", str(requirements_file)])
    else:
        cmds.append([py, "-m", "pip", "install", "openpyxl", "playwright", "PyYAML"])
    cmds.append([py, "-m", "playwright", "install", "chromium"])

    for cmd in cmds:
        print(">", " ".join(cmd))
        try:
            subprocess.check_call(cmd)
        except Exception as e:
            print(f"  FAILED: {e}")
            r = check_environment(require_browser=True)
            r.errors.append(f"自动安装失败: {e}")
            return r
    return check_environment(require_browser=True)


def ensure_or_exit(require_browser: bool = True, auto_install: bool = False) -> None:
    r = check_environment(require_browser=require_browser)
    r.print()
    if r.ok:
        return
    if auto_install:
        root = Path(__file__).resolve().parents[1]
        req = root / "requirements.txt"
        r2 = try_auto_install(req if req.exists() else None)
        r2.print()
        if r2.ok:
            return
        r = r2
    print("\n环境未就绪。请按上方 hints 安装后重试。", file=sys.stderr)
    print("Agent: 向用户展示安装命令，或使用 --auto-install 重试。", file=sys.stderr)
    raise SystemExit(2)
