#!/usr/bin/env python3
"""对本地 Web 向导做真实截图（Playwright），写入 docs/images/。

前置：
  python -m material_price_audit serve --host 127.0.0.1 --port 8765

用法：
  python scripts/capture_screenshots.py

说明：
  - 截图用演示材料名/价，不依赖本机真实询价结果
  - 第⑤步展示「我已登录，继续」与用量面板（当前 UI）
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
BASE = "http://127.0.0.1:8765"

# 演示数据：发版截图用，避免真实工程材料名/价泄漏
_DEMO_PREVIEW = [
    {"sheet": "给排水", "row": 3, "name": "闸阀", "spec": "DN50 铜芯", "submit": 128},
    {"sheet": "给排水", "row": 4, "name": "截止阀", "spec": "DN40 不锈钢", "submit": 96},
    {"sheet": "电气", "row": 2, "name": "电缆桥架", "spec": "200×100 热镀锌", "submit": 85},
    {"sheet": "电气", "row": 5, "name": "配电箱", "spec": "12回路 明装", "submit": 520},
]

_DEMO_RESULTS = [
    {
        "row": 3,
        "name": "闸阀",
        "spec": "DN50 铜芯",
        "status": "full_k",
        "price": 118.0,
        "message": "正式价 · guangcai · 名称+规格匹配",
    },
    {
        "row": 4,
        "name": "截止阀",
        "spec": "DN40 不锈钢",
        "status": "need_review",
        "price": 89.5,
        "message": "候选待核 · 规格略差一截，请点链接人工确认",
    },
    {
        "row": 2,
        "name": "电缆桥架",
        "spec": "200×100 热镀锌",
        "status": "partial",
        "price": 79.0,
        "message": "部分命中 · 已收录 1/3 价",
    },
    {
        "row": 5,
        "name": "配电箱",
        "spec": "12回路 明装",
        "status": "no_match",
        "price": None,
        "message": "没查到 · 可导出 RFQ 问厂家",
    },
]


def shot(page, name: str, full: bool = True) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print("saved", path.relative_to(ROOT), path.stat().st_size)


def show_step(page, n: int) -> None:
    page.evaluate(
        """(n) => {
          for (let i = 1; i <= 7; i++) {
            const el = document.getElementById('card' + i);
            if (el) el.style.display = i === n ? 'block' : 'none';
          }
          document.querySelectorAll('.step-pill').forEach((el) => {
            const s = +el.dataset.s;
            el.classList.toggle('on', s === n);
            el.classList.toggle('done', s < n && s <= 6);
          });
        }""",
        n,
    )
    page.wait_for_timeout(350)


def main() -> None:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            device_scale_factor=2,
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
        try:
            page.wait_for_selector("#platList input[type=checkbox]", timeout=12000)
        except Exception:
            pass

        # ① 平台：勾选前三个核心站，匹配模式 practical
        # 清空本机 AI Key 展示，避免截图泄漏「已配置」痕迹
        show_step(page, 1)
        page.evaluate(
            """() => {
              document.querySelectorAll('#platList input[type=checkbox]')
                .forEach((b, i) => { b.checked = i < 3; });
              const mm = document.getElementById('matchMode');
              if (mm) mm.value = 'practical';
              const q = document.getElementById('quotes');
              if (q) q.value = '3';
              // 演示：AI 保持关闭，清空 Key/Base 展示
              const en = document.getElementById('llmEnabled');
              if (en) en.checked = false;
              const fields = document.getElementById('aiFields');
              if (fields) fields.style.display = 'none';
              ['llmApiBase', 'llmApiKey', 'llmModel'].forEach((id) => {
                const el = document.getElementById(id);
                if (el) el.value = '';
              });
              const keyEnv = document.getElementById('llmKeyEnv');
              if (keyEnv) keyEnv.value = 'OPENAI_API_KEY';
              // 地区演示：四川省（通用）
              const prov = document.querySelector('[name=regionProvince], #regionProvince, #defaultProvince');
              if (prov && 'value' in prov) prov.value = '四川省';
            }"""
        )
        page.wait_for_timeout(300)
        shot(page, "01-step1-platforms")

        # ② 上传
        show_step(page, 2)
        page.evaluate(
            """() => {
              const box = document.getElementById('selectedFileBox');
              if (box) box.textContent = '当前文件：demo-询价样例.xlsx（演示）';
            }"""
        )
        page.wait_for_timeout(250)
        shot(page, "02-step2-upload")

        # ③ 识表：演示预览行
        show_step(page, 3)
        page.evaluate(
            """(rows) => {
              const tb = document.getElementById('previewBody');
              const sum = document.getElementById('parseSummary');
              if (sum) sum.textContent = '演示识表：2 个 Sheet · 4 条材料（样例数据，非真实工程）';
              if (tb) {
                tb.innerHTML = rows.map((r) => `<tr>
                  <td>${r.sheet}</td><td>${r.row}</td>
                  <td><strong>${r.name}</strong></td>
                  <td>${r.spec}</td><td>${r.submit}</td>
                </tr>`).join('');
              }
            }""",
            _DEMO_PREVIEW,
        )
        page.wait_for_timeout(300)
        shot(page, "03-step3-schema")

        # ④ 登录面板
        show_step(page, 4)
        try:
            page.evaluate(
                """async () => {
                  const st = await (await fetch('/api/state')).json();
                  let plats = st.platforms || [];
                  if (!plats.length) {
                    plats = ['guangcai', 'lingcai', 'huixun'];
                  }
                  await fetch('/api/login/init', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({platforms: plats.slice(0, 4)}),
                  });
                }"""
            )
            page.wait_for_timeout(800)
            if page.locator("#btnLoginRefresh").count():
                page.click("#btnLoginRefresh")
                page.wait_for_timeout(600)
        except Exception as e:
            print("login init warn:", e)
        # 若列表仍空，注入演示登录卡片
        page.evaluate(
            """() => {
              const list = document.getElementById('loginList');
              if (!list) return;
              if (list.children.length > 0) return;
              const demo = [
                {name: '广材网', status: '已通过', url: 'https://www.gldjc.com/...'},
                {name: '领材网', status: '待校验', url: 'https://www.hylcw.cn/userInfo/...'},
                {name: '慧讯网', status: '待打开', url: 'https://services.iccchina.com/login'},
              ];
              list.innerHTML = demo.map((p) => `
                <div class="login-card">
                  <div class="meta">
                    <strong>${p.name} · ${p.status}</strong>
                    <small>登录页：${p.url}</small>
                  </div>
                  <div class="actions">
                    <button type="button" class="ghost">打开登录页</button>
                    <button type="button" class="primary">本站已登录，校验</button>
                  </div>
                </div>`).join('');
              const sum = document.getElementById('loginSummary');
              if (sum) sum.textContent = '演示：已通过 1/3，待登录：领材网、慧讯网';
            }"""
        )
        show_step(page, 4)
        page.wait_for_timeout(350)
        shot(page, "04-step4-login")

        # ⑤ 执行：范围 + 运行控件 + 我已登录继续 + 用量
        show_step(page, 5)
        page.evaluate(
            """() => {
              const tools = document.getElementById('runTools');
              if (tools) tools.style.display = 'block';
              ['btnPause', 'btnStop', 'btnLoginDone'].forEach((id) => {
                const el = document.getElementById(id);
                if (el) {
                  el.style.display = 'inline-flex';
                  el.style.alignItems = 'center';
                  el.style.justifyContent = 'center';
                }
              });
              const hint = document.getElementById('loginWaitHint');
              if (hint) hint.style.display = 'block';
              const phase = document.getElementById('runPhaseLabel');
              if (phase) {
                phase.textContent = '登录中';
                phase.style.color = '#b45309';
              }
              const sum = document.getElementById('runLoginSummary');
              if (sum) sum.textContent = '已验证平台：广材网 · 领材网（演示）';
              const scopeSum = document.getElementById('scopeSummary');
              if (scopeSum) scopeSum.textContent = '将询价：全部材料（演示 4 条）';
              const msg = document.getElementById('msg4');
              if (msg) {
                msg.textContent = '⏳ 等待 [lingcai] 领材网 登录：请在弹出浏览器完成登录后，点「我已登录，继续」';
                msg.className = 'msg';
              }
              // 演示：AI 状态条改为通用文案（不带本机模型名）
              const aiRun = document.getElementById('aiStatusRun');
              if (aiRun) {
                aiRun.className = 'ai-status off';
                aiRun.textContent = 'AI：规则模式（演示截图 · 未调用大模型）';
              }
              // 演示用量数字
              const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
              set('uItems', '2');
              set('uTokens', '1260');
              set('uTokIn', '980');
              set('uTokOut', '280');
              set('uElapsed', '48s');
              const detail = document.getElementById('uLlmDetail');
              if (detail) detail.textContent = 'AI：规则模式（演示截图）；可随时在上方开关大模型';
              const llmToggle = document.getElementById('llmRuntimeToggle');
              if (llmToggle) llmToggle.checked = false;
              const stFull = document.getElementById('stFull');
              if (stFull) stFull.textContent = '1';
              const stReview = document.getElementById('stReview');
              if (stReview) stReview.textContent = '1';
              const stCur = document.getElementById('stCur');
              if (stCur) stCur.textContent = '2/4';
            }"""
        )
        page.wait_for_timeout(400)
        shot(page, "05-step5-run")
        # 特写：强制可见后再截（避免父级 display 导致 not visible）
        page.evaluate(
            """() => {
              const tools = document.getElementById('runTools');
              if (tools) {
                tools.style.display = 'block';
                tools.style.visibility = 'visible';
              }
              const up = document.getElementById('usagePanel');
              if (up) {
                up.style.display = 'block';
                up.style.visibility = 'visible';
              }
              const sc = document.getElementById('scopeBox');
              if (sc) {
                sc.style.display = 'block';
                sc.style.visibility = 'visible';
              }
            }"""
        )
        page.wait_for_timeout(200)
        def _clip_shot(selector: str, out_name: str) -> None:
            """用 boundingClientRect 裁剪截图，绕过 Playwright isVisible 误判。"""
            box = page.evaluate(
                """(sel) => {
                  const el = document.querySelector(sel);
                  if (!el) return null;
                  el.scrollIntoView({block: 'center', inline: 'nearest'});
                  const r = el.getBoundingClientRect();
                  if (r.width < 4 || r.height < 4) return null;
                  return {
                    x: Math.max(0, r.x),
                    y: Math.max(0, r.y),
                    width: Math.min(r.width, window.innerWidth),
                    height: Math.min(r.height, window.innerHeight * 2),
                  };
                }""",
                selector,
            )
            path = OUT / out_name
            if not box:
                print(f"{out_name} skip: no box for {selector}")
                return
            # device_scale_factor=2 时 clip 用 CSS 像素
            page.screenshot(path=str(path), clip=box, full_page=False)
            print("saved", f"docs/images/{out_name}", path.stat().st_size)

        _clip_shot("#scopeBox", "05b-scope-box.png")
        # 优先 usagePanel；不可见则整块 runTools
        box_up = page.evaluate(
            """() => {
              const el = document.querySelector('#usagePanel');
              if (!el) return null;
              const r = el.getBoundingClientRect();
              return r.width > 4 && r.height > 4 ? true : null;
            }"""
        )
        if box_up:
            _clip_shot("#usagePanel", "05c-usage-panel.png")
        else:
            _clip_shot("#runTools", "05c-usage-panel.png")

        # ⑥ 结果：默认保留仓库内手工/真实界面截图，避免演示页覆盖
        # 需要演示页时：MPA_SHOT_STEP6=1 python scripts/capture_screenshots.py
        import os

        if os.environ.get("MPA_SHOT_STEP6", "").strip() in ("1", "true", "yes"):
            show_step(page, 6)
            page.evaluate(
                """(items) => {
                  const tb = document.getElementById('resultBody');
                  const dm = document.getElementById('doneMsg');
                  if (dm) dm.textContent = '演示结果：绿合格 / 黄候选待核 / 灰没查到（样例数据）';
                  document.querySelectorAll('.ai-status, #aiStatusDone, #aiStatusRun').forEach((el) => {
                    el.textContent = 'AI：演示模式（未调用）';
                    el.className = (el.className || '').replace(/\\bon\\b/, 'off') + ' off';
                  });
                  const label = (s) => ({
                    full_k: '绿·已凑满',
                    partial: '绿·部分',
                    need_review: '黄·候选待核',
                    no_match: '灰·没查到',
                  }[s] || s || '—');
                  if (tb) {
                    tb.innerHTML = items.map((r) => {
                      const price = r.price != null ? r.price : '—';
                      return `<tr>
                        <td>${r.row ?? ''}</td>
                        <td><strong>${(r.name || '').slice(0, 48)}</strong>
                          <div class="result-detail">${(r.spec || '').slice(0, 80)}</div></td>
                        <td>${label(r.status)}</td>
                        <td class="price-cell">${price}</td>
                        <td><div class="result-detail">${(r.message || '').slice(0, 120)}</div></td>
                      </tr>`;
                    }).join('');
                  }
                }""",
                _DEMO_RESULTS,
            )
            page.wait_for_timeout(400)
            shot(page, "06-step6-results")
        else:
            print("skip 06-step6-results.png（保留现有截图；覆盖请设 MPA_SHOT_STEP6=1）")

        browser.close()
    print("OK →", OUT)


if __name__ == "__main__":
    main()
