#!/usr/bin/env python3
"""发布 Gitee Release 并上传 dist/ 中的 v0.3.5 安装包。

用法：
  export GITEE_TOKEN=你的私人令牌   # https://gitee.com/profile/personal_access_tokens
  # 勾选 projects 权限
  python3 scripts/publish_gitee_release.py

或：
  GITEE_TOKEN=xxx python3 scripts/publish_gitee_release.py --tag v0.3.5
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER = "arogan"
REPO = "material-price-audit"
API = f"https://gitee.com/api/v5/repos/{OWNER}/{REPO}"


def api(method: str, url: str, token: str, data: dict | None = None) -> tuple[int, dict | list]:
    if "?" in url:
        url = f"{url}&access_token={urllib.parse.quote(token)}"
    else:
        url = f"{url}?access_token={urllib.parse.quote(token)}"
    headers = {"User-Agent": "material-price-audit-gitee-publish"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:500]}


def upload_attach(token: str, release_id: int, path: Path) -> tuple[int, dict]:
    """Gitee attach_files 需 multipart，用 curl 更稳。"""
    import subprocess

    url = f"{API}/releases/{release_id}/attach_files?access_token={urllib.parse.quote(token)}"
    r = subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            url,
            "-F",
            f"file=@{path}",
        ],
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(r.stdout) if r.stdout else {}
    except Exception:
        data = {"message": r.stdout[:300] or r.stderr[:300]}
    # curl 成功时 HTTP 在 data 里
    code = 200 if "browser_download_url" in data or "name" in data or "file" in data else 400
    if r.returncode != 0:
        code = 500
        data = {"message": r.stderr or r.stdout}
    return code, data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v0.3.5")
    ap.add_argument("--token", default=os.environ.get("GITEE_TOKEN", "").strip())
    args = ap.parse_args()
    token = args.token
    if not token:
        print(
            "缺少 GITEE_TOKEN。\n"
            "1) 打开 https://gitee.com/profile/personal_access_tokens\n"
            "2) 生成私人令牌，勾选 projects\n"
            "3) 执行：export GITEE_TOKEN=你的令牌\n"
            "4) 再运行本脚本",
            file=sys.stderr,
        )
        return 2

    tag = args.tag
    notes = (ROOT / "docs" / f"release-notes-{tag.lstrip('v')}.md")
    # allow release-notes-v0.3.5.md naming
    if not notes.is_file():
        notes = ROOT / "docs" / f"release-notes-{tag}.md"
    if not notes.is_file():
        notes = ROOT / "docs" / "release-notes-v0.3.5.md"
    body = notes.read_text(encoding="utf-8") if notes.is_file() else f"{tag} 发行版"
    title = f"{tag} 材料询价工作台（可用版）"

    # verify token
    st, me = api("GET", "https://gitee.com/api/v5/user", token)
    if st != 200:
        print("令牌无效：", me, file=sys.stderr)
        return 2
    print("Gitee 用户：", me.get("login") or me.get("name"))

    # list existing
    st, releases = api("GET", f"{API}/releases", token)
    rel = None
    if st == 200 and isinstance(releases, list):
        for r in releases:
            if r.get("tag_name") == tag or r.get("tag_name") == tag.lstrip("v"):
                rel = r
                break

    if not rel:
        print("创建 Release…")
        st, rel = api(
            "POST",
            f"{API}/releases",
            token,
            data={
                "tag_name": tag,
                "name": title,
                "body": body,
                "target_commitish": "main",
            },
        )
        print("create", st, rel.get("id") or rel.get("message"))
        if st not in (200, 201) or not rel.get("id"):
            return 1
    else:
        print("已有 Release id=", rel.get("id"), "更新描述…")
        # Gitee may use PATCH differently; try recreate body via edit if available
        st2, rel2 = api(
            "PATCH",
            f"{API}/releases/{rel['id']}",
            token,
            data={"name": title, "body": body, "tag_name": tag},
        )
        if st2 in (200, 201):
            rel = rel2
        print("update", st2)

    rid = int(rel["id"])
    files = [
        ROOT / "dist" / f"MaterialPriceAudit-macOS-{tag}.zip",
        ROOT / "dist" / f"MaterialPriceAudit-Windows-{tag}.zip",
        ROOT / "dist" / f"material-price-audit-{tag.lstrip('v')}-portable.zip",
        ROOT / "dist" / f"material_price_audit-{tag.lstrip('v')}-py3-none-any.whl",
        ROOT / "dist" / f"material_price_audit-{tag.lstrip('v')}.tar.gz",
    ]
    # also try without 'v' in desktop names
    ver = tag.lstrip("v")
    files = [
        ROOT / "dist" / f"MaterialPriceAudit-macOS-v{ver}.zip",
        ROOT / "dist" / f"MaterialPriceAudit-Windows-v{ver}.zip",
        ROOT / "dist" / f"material-price-audit-{ver}-portable.zip",
        ROOT / "dist" / f"material_price_audit-{ver}-py3-none-any.whl",
        ROOT / "dist" / f"material_price_audit-{ver}.tar.gz",
    ]

    for p in files:
        if not p.is_file():
            print("缺少文件，跳过：", p.name)
            continue
        print("上传", p.name, p.stat().st_size)
        code, data = upload_attach(token, rid, p)
        print(" ", code, data.get("browser_download_url") or data.get("name") or data.get("message") or data)

    print("完成：https://gitee.com/{}/{}/releases".format(OWNER, REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
