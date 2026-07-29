#!/usr/bin/env python3
"""对本地 Web 向导做真实截图（Playwright），写入 docs/images/。

前置：
  python -m material_price_audit serve --host 127.0.0.1 --port 8765

用法：
  python scripts/capture_screenshots.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
BASE = "http://127.0.0.1:8765"


def shot(page, name: str, full: bool = True) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print("saved", path.relative_to(ROOT), path.stat().st_size)


def show_step(page, n: int) -> None:
    page.evaluate(
        """(n) => {
          for (let i = 1; i <= 6; i++) {
            const el = document.getElementById('card' + i);
            if (el) el.style.display = i === n ? 'block' : 'none';
          }
          document.querySelectorAll('.step-pill').forEach((el) => {
            const s = +el.dataset.s;
            el.classList.toggle('on', s === n);
            el.classList.toggle('done', s < n);
          });
        }""",
        n,
    )
    page.wait_for_timeout(350)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            device_scale_factor=2,
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        try:
            page.wait_for_selector("#platList input[type=checkbox]", timeout=10000)
        except Exception:
            pass

        show_step(page, 1)
        page.evaluate(
            """() => {
              document.querySelectorAll('#platList input[type=checkbox]')
                .forEach((b, i) => { b.checked = i < 3; });
            }"""
        )
        page.wait_for_timeout(250)
        shot(page, "01-step1-platforms")

        show_step(page, 2)
        page.wait_for_timeout(250)
        shot(page, "02-step2-upload")

        show_step(page, 3)
        page.wait_for_timeout(300)
        shot(page, "03-step3-schema")

        show_step(page, 4)
        try:
            page.evaluate(
                """async () => {
                  const st = await (await fetch('/api/state')).json();
                  const plats = st.platforms || [];
                  if (plats.length) {
                    await fetch('/api/login/init', {
                      method: 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({platforms: plats}),
                    });
                  }
                }"""
            )
            page.wait_for_timeout(700)
            if page.locator("#btnLoginRefresh").count():
                page.click("#btnLoginRefresh")
                page.wait_for_timeout(500)
        except Exception as e:
            print("login init warn:", e)
        show_step(page, 4)
        page.wait_for_timeout(300)
        shot(page, "04-step4-login")

        show_step(page, 5)
        page.evaluate(
            """() => {
              const tools = document.getElementById('runTools');
              if (tools) tools.style.display = 'block';
              ['btnPause', 'btnStop'].forEach((id) => {
                const el = document.getElementById(id);
                if (el) {
                  el.style.display = 'inline-flex';
                  el.style.alignItems = 'center';
                  el.style.justifyContent = 'center';
                }
              });
            }"""
        )
        page.wait_for_timeout(300)
        shot(page, "05-step5-run")
        if page.locator("#scopeBox").count():
            page.locator("#scopeBox").screenshot(path=str(OUT / "05b-scope-box.png"))
            print("saved", "docs/images/05b-scope-box.png")
        if page.locator("#usagePanel").count():
            page.locator("#usagePanel").screenshot(path=str(OUT / "05c-usage-panel.png"))
            print("saved", "docs/images/05c-usage-panel.png")

        show_step(page, 6)
        page.evaluate(
            """async () => {
              const st = await (await fetch('/api/state')).json();
              const tb = document.getElementById('resultBody');
              const items = st.item_results || [];
              const dm = document.getElementById('doneMsg');
              if (dm) dm.textContent = st.message || '询价结果';
              if (tb && items.length) {
                const label = (s) => ({
                  full_k: '绿·已凑满', partial: '绿·部分',
                  need_review: '黄·候选待核', no_match: '灰·没查到',
                }[s] || s || '—');
                tb.innerHTML = items.slice(0, 15).map((r) => {
                  const price = r.price != null ? r.price
                    : (r.review_list && r.review_list[0] && r.review_list[0].price) || '—';
                  return `<tr>
                    <td>${r.row ?? ''}</td>
                    <td><strong>${(r.name || '').slice(0, 48)}</strong>
                      <div class="result-detail">${(r.spec || '').slice(0, 80)}</div></td>
                    <td>${label(r.status)}</td>
                    <td class="price-cell">${price}</td>
                    <td><div class="result-detail">${(r.message || '').slice(0, 100)}</div></td>
                  </tr>`;
                }).join('');
              }
            }"""
        )
        page.wait_for_timeout(400)
        shot(page, "06-step6-results")

        browser.close()
    print("OK →", OUT)


if __name__ == "__main__":
    main()
