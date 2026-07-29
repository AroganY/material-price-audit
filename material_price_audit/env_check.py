"""Environment checks — fast, quiet, no pip upgrade spam."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# 成功检查缓存：避免每次 run 都启动浏览器 / 刷屏
_CACHE_NAME = ".env_ok_cache.json"
_CACHE_TTL_SEC = 7 * 24 * 3600  # 7 天


@dataclass
class CheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    python_exe: str = ""
    from_cache: bool = False

    def print(self, quiet_ok: bool = False) -> None:
        if quiet_ok and self.ok and not self.errors:
            # 正常可跑时一行带过，别逼逼升级
            print(f"[env] OK  Python {sys.version.split()[0]}")
            return
        print("=== 环境检查 ===")
        print(f"Python : {sys.version.split()[0]} ({sys.executable})")
        if self.from_cache:
            print("状态   : OK（缓存，跳过重复检测）")
        elif self.ok:
            print("状态   : OK")
        else:
            print("状态   : FAIL — 缺依赖才装，不会自动升级 Python")
        for e in self.errors:
            print(f"  [ERROR] {e}")
        for w in self.warnings:
            print(f"  [WARN]  {w}")
        if self.hints:
            print("安装（仅缺失时）:")
            for h in self.hints:
                print(f"  {h}")


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cache_path() -> Path:
    return _package_root() / _CACHE_NAME


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - float(data.get("ts", 0)) > _CACHE_TTL_SEC:
            return None
        if data.get("python") != sys.executable:
            return None
        if data.get("version") != list(sys.version_info[:3]):
            return None
        return data
    except Exception:
        return None


def _write_cache(ok: bool) -> None:
    if not ok:
        return
    try:
        _cache_path().write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "python": sys.executable,
                    "version": list(sys.version_info[:3]),
                    "ok": True,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def find_system_python() -> str | None:
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


def check_environment(
    require_browser: bool = False,
    *,
    force: bool = False,
    use_cache: bool = True,
) -> CheckResult:
    """
    轻量检查：
    - 默认只验证 import 是否成功（不启动浏览器、不检查升级）
    - require_browser=True 且无缓存时，才试启动一次浏览器
    - 成功结果缓存 7 天，run 不再反复折腾
    """
    if use_cache and not force:
        cached = _read_cache()
        if cached and cached.get("ok"):
            # 再确认关键包仍在（缓存命中后极速）
            if all(_has_module(m) for m in ("openpyxl", "yaml", "playwright")):
                return CheckResult(
                    ok=True,
                    python_exe=sys.executable,
                    from_cache=True,
                )

    res = CheckResult(ok=True, python_exe=sys.executable)

    if sys.version_info < (3, 10):
        res.ok = False
        res.errors.append(
            f"需要 Python >= 3.10，当前 {sys.version_info.major}.{sys.version_info.minor}"
        )
        alt = find_system_python()
        if alt:
            res.hints.append(f"请改用: {alt} -m material_price_audit ...")
        else:
            res.hints.append("请安装 Python 3.10+（不自动升级系统 Python）")
        return res

    missing_pkgs = []
    if not _has_module("openpyxl"):
        missing_pkgs.append("openpyxl")
    if not _has_module("yaml"):
        missing_pkgs.append("PyYAML")
    if not _has_module("playwright"):
        missing_pkgs.append("playwright")

    if missing_pkgs:
        res.ok = False
        res.errors.append("缺少包: " + ", ".join(missing_pkgs))
        res.hints.append(f"{sys.executable} -m pip install {' '.join(missing_pkgs)}")
        res.hints.append(f"{sys.executable} -m material_price_audit check --auto-install")
        return res

    # 默认不启动浏览器。仅 force / 显式 require_browser 且无缓存时探一次
    if require_browser and (force or not _read_cache()):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                launched = False
                last_err: Exception | None = None
                for kwargs in ({"channel": "chrome"}, {},):
                    try:
                        browser = p.chromium.launch(headless=True, **kwargs)
                        browser.close()
                        launched = True
                        if kwargs.get("channel") == "chrome":
                            res.warnings.append("使用本机 Chrome")
                        break
                    except Exception as e:
                        last_err = e
                if not launched:
                    res.ok = False
                    res.errors.append(f"无法启动浏览器: {last_err}")
                    res.hints.append(f"{sys.executable} -m playwright install chromium")
        except Exception as e:
            res.ok = False
            res.errors.append(f"Playwright 失败: {e}")
            res.hints.append(f"{sys.executable} -m playwright install chromium")

    if res.ok:
        _write_cache(True)
    return res


def try_auto_install(requirements_file: Path | None = None) -> CheckResult:
    """
    只装缺失的包。绝不 pip upgrade pip / 升级 Python。
    """
    print("=== 安装缺失依赖（不升级 Python / 不升级 pip）===")
    py = sys.executable
    missing = []
    if not _has_module("openpyxl"):
        missing.append("openpyxl")
    if not _has_module("yaml"):
        missing.append("PyYAML")
    need_pw = not _has_module("playwright")
    if need_pw:
        missing.append("playwright")

    if not missing and _has_module("playwright"):
        print("包已齐全，跳过 pip")
        r = check_environment(require_browser=True, force=True, use_cache=False)
        if r.ok:
            return r
        # 可能缺 chromium
        print(">", py, "-m", "playwright", "install", "chromium")
        try:
            subprocess.check_call([py, "-m", "playwright", "install", "chromium"])
        except Exception as e:
            r.errors.append(f"playwright install 失败: {e}")
            r.ok = False
            return r
        return check_environment(require_browser=True, force=True, use_cache=False)

    if requirements_file and requirements_file.exists() and missing:
        cmd = [py, "-m", "pip", "install", "-r", str(requirements_file)]
    else:
        cmd = [py, "-m", "pip", "install", *missing] if missing else None

    if cmd:
        print(">", " ".join(cmd))
        try:
            subprocess.check_call(cmd)
        except Exception as e:
            print(f"  FAILED: {e}")
            r = check_environment(require_browser=False, force=True, use_cache=False)
            r.errors.append(f"自动安装失败: {e}")
            r.ok = False
            return r

    if need_pw or not _has_module("playwright"):
        print(">", py, "-m", "playwright", "install", "chromium")
        try:
            subprocess.check_call([py, "-m", "playwright", "install", "chromium"])
        except Exception as e:
            print(f"  FAILED: {e}")
            r = check_environment(require_browser=False, force=True, use_cache=False)
            r.errors.append(f"playwright install 失败: {e}")
            r.ok = False
            return r

    return check_environment(require_browser=True, force=True, use_cache=False)


def ensure_or_exit(
    require_browser: bool = False,
    auto_install: bool = False,
    *,
    quiet: bool = True,
) -> None:
    """
    日常 serve/parse：静默快检，OK 就一行。
    只有缺依赖才报错；--auto-install 只补缺，不升级。
    """
    r = check_environment(require_browser=require_browser, use_cache=True)
    r.print(quiet_ok=quiet)
    if r.ok:
        return
    if auto_install:
        root = _package_root()
        req = root / "requirements.txt"
        r2 = try_auto_install(req if req.exists() else None)
        r2.print(quiet_ok=False)
        if r2.ok:
            return
        r = r2
    print("\n环境未就绪。只装缺失包即可，不要升级 Python。", file=sys.stderr)
    for h in r.hints:
        print(f"  {h}", file=sys.stderr)
    raise SystemExit(2)
