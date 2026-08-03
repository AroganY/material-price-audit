#!/usr/bin/env python3
"""
桌面一键启动器（macOS / Windows 双击即可）。

首次运行：
  1. 创建 .venv
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


def app_root() -> Path:
    """包根目录：开发树 / 便携包 / .app/Contents/Resources/app。"""
    if getattr(sys, "frozen", False):
        # PyInstaller：与 exe 同目录的 app/ 或上一级 Resources/app
        me = Path(sys.executable).resolve().parent
        for cand in (me / "app", me.parent / "Resources" / "app", me):
            if (cand / "material_price_audit").is_dir() or (cand / "pyproject.toml").is_file():
                return cand
        return me
    here = Path(__file__).resolve().parent
    # .app/Contents/MacOS/../Resources/app
    res_app = here.parent / "Resources" / "app"
    if (res_app / "material_price_audit").is_dir():
        return res_app
    return here


def log(msg: str) -> None:
    line = f"[材料询价] {msg}"
    print(line, flush=True)
    try:
        root = app_root()
        log_path = root / "data" / "output" / "desktop_launch.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def ensure_venv(root: Path) -> Path:
    py = venv_python(root)
    if py.is_file():
        return py
    log("首次运行：创建虚拟环境 .venv …")
    subprocess.check_call([sys.executable, "-m", "venv", str(root / ".venv")])
    if not py.is_file():
        raise RuntimeError("创建虚拟环境失败，请确认已安装 Python 3.10+")
    return py


def pip_install(py: Path, root: Path) -> None:
    log("安装 / 更新依赖（首次可能需几分钟）…")
    subprocess.check_call([str(py), "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"])
    # 优先 editable 源码；否则 wheel
    if (root / "pyproject.toml").is_file():
        subprocess.check_call([str(py), "-m", "pip", "install", "-e", str(root)])
    else:
        whls = list(root.glob("material*price*audit*.whl")) + list(root.glob("*.whl"))
        if whls:
            subprocess.check_call([str(py), "-m", "pip", "install", str(whls[0])])
        elif (root / "requirements.txt").is_file():
            subprocess.check_call([str(py), "-m", "pip", "install", "-r", str(root / "requirements.txt")])
        else:
            raise RuntimeError("找不到 pyproject.toml / wheel / requirements.txt")
    log("安装 Playwright Chromium（首次下载较大）…")
    subprocess.check_call([str(py), "-m", "playwright", "install", "chromium"])


def marker_path(root: Path) -> Path:
    return root / ".venv" / ".mpa_ready"


def ensure_ready(root: Path) -> Path:
    py = ensure_venv(root)
    marker = marker_path(root)
    if marker.is_file():
        # 轻量自检：入口可 import
        try:
            subprocess.check_call(
                [str(py), "-c", "import material_price_audit, playwright"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return py
        except Exception:
            log("环境不完整，重新安装…")
    pip_install(py, root)
    marker.write_text(f"ready\n", encoding="utf-8")
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


def start_server(py: Path, root: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # 工作目录固定到 app root，保证 data/ 与 .browser-profile 位置正确
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
    creationflags = 0
    if sys.platform == "win32":
        # 不弹黑色控制台附属窗口（主进程仍可有控制台）
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        cmd,
        cwd=str(root),
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
    """可选：用 Tk 弹一个简单状态窗（失败时不影响主流程）。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        pass


def main() -> int:
    root = app_root()
    os.chdir(root)
    (root / "data" / "output").mkdir(parents=True, exist_ok=True)
    (root / "data" / "input").mkdir(parents=True, exist_ok=True)

    log(f"应用目录: {root}")
    log(f"Python: {sys.version.split()[0]} ({sys.executable})")

    if not shutil.which("python3") and not shutil.which("python") and not getattr(sys, "frozen", False):
        show_status_window(
            "缺少 Python",
            "未检测到 Python。请先安装 Python 3.10+：\nhttps://www.python.org/downloads/\n"
            "Windows 安装时请勾选 Add python.exe to PATH。",
        )
        return 2

    try:
        py = ensure_ready(root)
    except Exception as e:
        log(f"安装失败: {e}")
        show_status_window(
            "安装失败",
            f"自动安装未成功：\n{e}\n\n请查看 data/output/desktop_launch.log\n"
            "或手动运行 install.sh / install.ps1",
        )
        return 2

    # 已有服务则直接开浏览器
    if port_in_use(HOST, PORT) and wait_http(URL, timeout=3):
        log("检测到服务已在运行，打开浏览器…")
        webbrowser.open(URL)
        show_status_window("材料询价工作台", f"向导已在运行。\n浏览器打开：{URL}")
        return 0

    proc = start_server(py, root)
    t = threading.Thread(target=pump_output, args=(proc,), daemon=True)
    t.start()

    if not wait_http(URL, timeout=90):
        log("服务启动超时")
        try:
            proc.terminate()
        except Exception:
            pass
        show_status_window(
            "启动超时",
            f"未能在 90 秒内打开 {URL}\n请查看 data/output/desktop_launch.log",
        )
        return 2

    log(f"打开浏览器：{URL}")
    log("价联通客户端下载：https://www.scjcio.site/tools/download")
    webbrowser.open(URL)
    # 非阻塞提示（部分环境双击无终端，给用户反馈）
    try:
        # 仅在 Windows 无控制台时弹窗；macOS .command 有终端
        if sys.platform == "win32" or os.environ.get("MPA_DESKTOP_NOTIFY") == "1":
            show_status_window(
                "材料询价工作台已启动",
                f"浏览器应已打开：\n{URL}\n\n关闭本窗口不会停止服务。\n"
                "要结束服务，请在任务管理器结束 python 进程，\n或关闭启动时的终端窗口。",
            )
    except Exception:
        pass

    # 前台等待服务退出（终端/双击启动时保持进程）
    try:
        return int(proc.wait())
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
