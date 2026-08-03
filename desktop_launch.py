#!/usr/bin/env python3
"""
桌面一键启动器（macOS / Windows 双击即可）。

首次运行：
  1. 创建 .venv（写到可写目录，避开 macOS App Translocation 只读盘）
  2. pip 安装本项目依赖
  3. 安装 Playwright Chromium
  4. 启动本地向导并自动打开浏览器

之后再次双击：直接启动向导并打开界面。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = os.environ.get("MPA_HOST", "127.0.0.1")
PORT = int(os.environ.get("MPA_PORT", "8765"))
URL = f"http://{HOST}:{PORT}/"

# 全局：源码目录（可能只读）与可写工作目录
_SOURCE_ROOT: Path | None = None
_WORK_ROOT: Path | None = None
_LOG_FILE: Path | None = None


def source_root() -> Path:
    """包源码所在目录（.app 内或便携包）。"""
    global _SOURCE_ROOT
    if _SOURCE_ROOT is not None:
        return _SOURCE_ROOT
    if getattr(sys, "frozen", False):
        me = Path(sys.executable).resolve().parent
        for cand in (me / "app", me.parent / "Resources" / "app", me):
            if (cand / "material_price_audit").is_dir() or (cand / "pyproject.toml").is_file():
                _SOURCE_ROOT = cand
                return _SOURCE_ROOT
        _SOURCE_ROOT = me
        return _SOURCE_ROOT
    here = Path(__file__).resolve().parent
    res_app = here.parent / "Resources" / "app"
    if (res_app / "material_price_audit").is_dir():
        _SOURCE_ROOT = res_app
        return _SOURCE_ROOT
    _SOURCE_ROOT = here
    return _SOURCE_ROOT


def path_writable(path: Path) -> bool:
    probe = path / f".mpa_write_probe_{os.getpid()}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except Exception:
            pass
        return True
    except Exception:
        return False


def is_app_translocated(path: Path) -> bool:
    s = str(path)
    return "AppTranslocation" in s or "/var/folders/" in s and "/T/" in s


def work_root(source: Path) -> Path:
    """
    可写运行目录：.venv / data / .browser-profile。

    macOS 从下载打开 .app 时会 App Translocation 到只读临时目录，
    必须把 venv 建到用户 Library 下。
    """
    global _WORK_ROOT
    if _WORK_ROOT is not None:
        return _WORK_ROOT

    env_home = os.environ.get("MATERIAL_PRICE_AUDIT_HOME", "").strip()
    if env_home:
        p = Path(env_home).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        _WORK_ROOT = p
        return _WORK_ROOT

    if path_writable(source) and not is_app_translocated(source):
        _WORK_ROOT = source
        return _WORK_ROOT

    if sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "MaterialPriceAudit"
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(base) / "MaterialPriceAudit"
    else:
        p = Path.home() / ".material-price-audit"
    p.mkdir(parents=True, exist_ok=True)
    _WORK_ROOT = p
    return _WORK_ROOT


def log(msg: str) -> None:
    global _LOG_FILE
    line = f"[材料询价] {msg}"
    print(line, flush=True)
    try:
        if _LOG_FILE is None:
            wr = _WORK_ROOT or work_root(source_root())
            _LOG_FILE = wr / "data" / "output" / "desktop_launch.log"
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def venv_python(work: Path) -> Path:
    if sys.platform == "win32":
        return work / ".venv" / "Scripts" / "python.exe"
    return work / ".venv" / "bin" / "python"


def ensure_venv(work: Path) -> Path:
    py = venv_python(work)
    if py.is_file():
        return py
    log(f"首次运行：在可写目录创建虚拟环境 …\n  → {work / '.venv'}")
    venv_dir = work / ".venv"
    try:
        subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"创建虚拟环境失败（{e}）。\n"
            "若路径在 AppTranslocation 只读区，请把 App 拖到「应用程序」后重试。\n"
            "或确认已安装 Python 3.10+。"
        ) from e
    if not py.is_file():
        raise RuntimeError("创建虚拟环境失败，请确认已安装 Python 3.10+")
    return py


def pip_install(py: Path, source: Path, work: Path) -> None:
    log("安装 / 更新依赖（首次可能需几分钟）…")
    subprocess.check_call(
        [str(py), "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"]
    )
    # 只读源码不能 pip install -e；用普通 install
    writable_src = path_writable(source) and not is_app_translocated(source)
    if (source / "pyproject.toml").is_file():
        if writable_src:
            log("pip install -e （源码可写）…")
            subprocess.check_call([str(py), "-m", "pip", "install", "-e", str(source)])
        else:
            log("pip install 源码包（App 只读 / Translocation 兼容）…")
            subprocess.check_call([str(py), "-m", "pip", "install", str(source)])
    else:
        whls = list(source.glob("material*price*audit*.whl")) + list(source.glob("*.whl"))
        if whls:
            subprocess.check_call([str(py), "-m", "pip", "install", str(whls[0])])
        elif (source / "requirements.txt").is_file():
            subprocess.check_call(
                [str(py), "-m", "pip", "install", "-r", str(source / "requirements.txt")]
            )
        else:
            raise RuntimeError("找不到 pyproject.toml / wheel / requirements.txt")
    log("安装 Playwright Chromium（首次下载较大）…")
    subprocess.check_call([str(py), "-m", "playwright", "install", "chromium"])
    # 可选：把 example 配置拷到工作目录
    try:
        ex = source / "config.example.yaml"
        dst = work / "config.example.yaml"
        if ex.is_file() and not dst.is_file():
            shutil.copy2(ex, dst)
    except Exception:
        pass


def marker_path(work: Path) -> Path:
    return work / ".venv" / ".mpa_ready"


def ensure_ready(source: Path, work: Path) -> Path:
    py = ensure_venv(work)
    marker = marker_path(work)
    if marker.is_file():
        try:
            subprocess.check_call(
                [str(py), "-c", "import material_price_audit, playwright"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return py
        except Exception:
            log("环境不完整，重新安装…")
    pip_install(py, source, work)
    try:
        marker.write_text(
            f"ready\nsource={source}\nwork={work}\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    return py


def wait_http(url: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            return s.connect_ex((host, port)) == 0
        except Exception:
            return False


def start_server(py: Path, work: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # 强制运行数据写到可写目录（data / .browser-profile）
    env["MATERIAL_PRICE_AUDIT_HOME"] = str(work)
    cmd = [
        str(py),
        "-m",
        "material_price_audit",
        "serve",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    log(f"启动向导服务 {URL}")
    log(f"数据目录 MATERIAL_PRICE_AUDIT_HOME={work}")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        cmd,
        cwd=str(work),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def pump_output(proc: subprocess.Popen) -> None:
    if not proc.stdout:
        return
    try:
        for raw in iter(proc.stdout.readline, b""):
            if not raw:
                break
            try:
                text = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                text = str(raw)
            if text:
                print(text, flush=True)
    except Exception:
        pass


def show_status_window(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        # macOS 后备
        if sys.platform == "darwin":
            try:
                safe = message.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.call(
                    [
                        "osascript",
                        "-e",
                        f'display dialog "{safe}" buttons {{"好"}} default button 1 with title "{title}"',
                    ]
                )
            except Exception:
                pass


def main() -> int:
    source = source_root()
    work = work_root(source)
    os.environ["MATERIAL_PRICE_AUDIT_HOME"] = str(work)

    (work / "data" / "output").mkdir(parents=True, exist_ok=True)
    (work / "data" / "input").mkdir(parents=True, exist_ok=True)

    log(f"源码目录: {source}")
    log(f"工作目录: {work}")
    if is_app_translocated(source) or source != work:
        log(
            "检测到 macOS 隔离/只读运行路径（App Translocation）。"
            "虚拟环境与数据已改存到用户目录，无需写 App 内部。"
        )
        log("建议：将 App 拖到「应用程序」文件夹，并执行：")
        log('  xattr -cr "材料询价工作台.app"')
    log(f"Python: {sys.version.split()[0]} ({sys.executable})")

    if (
        not shutil.which("python3")
        and not shutil.which("python")
        and not getattr(sys, "frozen", False)
    ):
        show_status_window(
            "缺少 Python",
            "未检测到 Python。请先安装 Python 3.10+：\nhttps://www.python.org/downloads/\n"
            "Windows 安装时请勾选 Add python.exe to PATH。",
        )
        return 2

    try:
        py = ensure_ready(source, work)
    except Exception as e:
        log(f"安装失败: {e}")
        tip = (
            f"自动安装未成功：\n{e}\n\n"
            f"日志：{work / 'data' / 'output' / 'desktop_launch.log'}\n\n"
            "macOS 请先：\n"
            "1) 把「材料询价工作台.app」拖到「应用程序」\n"
            "2) 终端执行：xattr -cr /Applications/材料询价工作台.app\n"
            "3) 再双击打开\n"
        )
        show_status_window("安装失败", tip)
        return 2

    if port_in_use(HOST, PORT) and wait_http(URL, timeout=3):
        log("检测到服务已在运行，打开浏览器…")
        webbrowser.open(URL)
        show_status_window("材料询价工作台", f"向导已在运行。\n浏览器打开：{URL}")
        return 0

    proc = start_server(py, work)
    t = threading.Thread(target=pump_output, args=(proc,), daemon=True)
    t.start()

    if not wait_http(URL, timeout=120):
        log("服务启动超时")
        try:
            proc.terminate()
        except Exception:
            pass
        show_status_window(
            "启动超时",
            f"未能在 120 秒内打开 {URL}\n请查看：\n{work / 'data' / 'output' / 'desktop_launch.log'}",
        )
        return 2

    log(f"打开浏览器：{URL}")
    log("价联通客户端下载：https://www.scjcio.site/tools/download")
    webbrowser.open(URL)
    try:
        if sys.platform == "win32" or os.environ.get("MPA_DESKTOP_NOTIFY") == "1":
            show_status_window(
                "材料询价工作台已启动",
                f"浏览器应已打开：\n{URL}\n\n关闭本窗口不会停止服务。\n"
                "要结束服务，请关闭启动时的终端窗口，\n或在任务管理器结束 python 进程。",
            )
    except Exception:
        pass

    try:
        return int(proc.wait())
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
