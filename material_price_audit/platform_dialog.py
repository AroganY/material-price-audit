"""Native popup for multi-select platforms (tkinter, no extra deps)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .platforms import PlatformSpec


def can_show_dialog() -> bool:
    if os.environ.get("MPA_NO_DIALOG") or os.environ.get("CI"):
        return False
    try:
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        r.update()
        r.destroy()
        return True
    except Exception:
        return False


def pick_platforms_dialog(
    registry: dict[str, "PlatformSpec"] | None = None,
    *,
    preselected: list[str] | None = None,
    title: str = "选择比价平台",
) -> list[str]:
    """
    弹出勾选窗口。用户点「开始」返回平台 id 列表（顺序=勾选顺序/列表顺序）。
    取消/关闭返回 []。
    """
    from .platforms import SELECTABLE_PLATFORM_IDS, load_platform_registry

    reg = registry or load_platform_registry({})
    choices: list[tuple[str, str, str]] = []  # id, name, login
    for pid in SELECTABLE_PLATFORM_IDS:
        if pid in reg:
            s = reg[pid]
            choices.append((pid, s.name, s.login_url or ""))
    for pid, s in reg.items():
        if pid not in {c[0] for c in choices}:
            choices.append((pid, s.name, s.login_url or ""))

    pre = set(preselected or [])

    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as e:
        print(f"[platforms] 无法加载弹窗(tkinter): {e}")
        return []

    result: list[str] = []

    root = tk.Tk()
    root.title(title)
    root.geometry("640x560")
    root.minsize(520, 420)
    try:
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    # center
    try:
        root.update_idletasks()
        w, h = 640, 560
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(
        frm,
        text="勾选要比价的网站（顺序=优先级 A→B→C）\n只勾你有账号/能查的站；没会员的别勾，勾了也会自动跳过。",
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 8))

    # scrollable checkboxes
    canvas = tk.Canvas(frm, highlightthickness=0)
    scroll = ttk.Scrollbar(frm, orient=tk.VERTICAL, command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
    )
    canvas.create_window((0, 0), window=inner, anchor=tk.NW)
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)

    vars_by_id: dict[str, tk.BooleanVar] = {}
    for i, (pid, name, login) in enumerate(choices):
        var = tk.BooleanVar(value=(pid in pre))
        vars_by_id[pid] = var
        row = ttk.Frame(inner)
        row.pack(fill=tk.X, pady=2)
        cb = ttk.Checkbutton(row, text=f"{name}  ({pid})", variable=var)
        cb.pack(side=tk.LEFT)
        ttk.Label(row, text=login[:56], foreground="#666").pack(side=tk.LEFT, padx=8)

    def on_mousewheel(event):
        try:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    canvas.bind_all("<MouseWheel>", on_mousewheel)

    btns = ttk.Frame(frm)
    btns.pack(fill=tk.X, pady=(10, 0))

    def select_cost():
        cost = {"guangcai", "huixun", "lingcai", "jcnet", "gldjc_hangqing", "gldjc_xunjia"}
        for pid, var in vars_by_id.items():
            var.set(pid in cost)

    def select_shop():
        shop = {"jd", "1688", "taobao", "tmall", "zkh", "suning"}
        for pid, var in vars_by_id.items():
            var.set(pid in shop)

    def select_none():
        for var in vars_by_id.values():
            var.set(False)

    def on_ok():
        nonlocal result
        picked = [pid for pid, _n, _u in choices if vars_by_id[pid].get()]
        if not picked:
            messagebox.showwarning("未选择", "请至少勾选一个平台。\n没有广材会员就别勾广材，勾京东/1688 即可。")
            return
        result = picked
        root.destroy()

    def on_cancel():
        nonlocal result
        result = []
        root.destroy()

    ttk.Button(btns, text="仅造价站", command=select_cost).pack(side=tk.LEFT, padx=2)
    ttk.Button(btns, text="仅电商", command=select_shop).pack(side=tk.LEFT, padx=2)
    ttk.Button(btns, text="清空", command=select_none).pack(side=tk.LEFT, padx=2)
    ttk.Button(btns, text="取消", command=on_cancel).pack(side=tk.RIGHT, padx=2)
    ttk.Button(btns, text="开始核价 →", command=on_ok).pack(side=tk.RIGHT, padx=2)

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    root.mainloop()

    try:
        canvas.unbind_all("<MouseWheel>")
    except Exception:
        pass

    return result
