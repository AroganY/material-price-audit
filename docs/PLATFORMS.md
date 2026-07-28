# 平台官网对照表（实测 / 不瞎指）

> 原则：**登录哪个站，浏览器就必须打开那个站的官网。**  
> 禁止把「领材网」指到广材网登录页。

---

## 已核实可用

| ID | 名称 | 登录页 | 搜索/首页 | 依据 |
|----|------|--------|-----------|------|
| **guangcai** | **广材网** | https://www.gldjc.com/login | https://www.gldjc.com/scj/so.html?l=1&keyword={query} | 实测标题：`广材网-建筑工程造价行业材料价格查询平台` |
| **gldjc_hangqing** | 广材行情 | https://hangqing.gldjc.com/ | 同上域名 | 实测可打开，钢材行情 |
| **gldjc_xunjia** | 广材询价 | https://xunjia.gldjc.com/ | 同上域名 | 实测可打开，人工询价 |
| **jcnet** | 建材在线 | https://www.jc.net.cn/ | https://www.jc.net.cn/ | 实测标题含「建材在线-建材信息价格服务」 |
| **jd** | 京东 | https://www.jd.com/ | search.jd.com | 公开电商 |
| **1688** | 1688 | https://www.1688.com/ | s.1688.com | 公开批发 |
| **taobao / tmall / zkh / suning / mysteel** | 见 platforms.py | 各自官网 | 各自搜索 | 公开站点 |

---

## 慧讯网说明（重要）

| 项目 | 说明 |
|------|------|
| 历史名称 | 广联达「慧讯网」材料价格服务 |
| **当前公开官网** | **已统一为广材网 https://www.gldjc.com/** |
| 登录页标题 | `登录-广材网-建筑工程造价行业材料价格查询平台` |
| 工具行为 | ID `huixun` 与 `guangcai` **同一登录 URL**，登录时**自动去重只弹一次**，不会当成两个不同网站乱跳 |

若你们单位仍使用**旧的独立慧讯域名**，请在 `config.yaml` 覆盖：

```yaml
platforms:
  definitions:
    huixun:
      name: 慧讯网
      login_url: "https://【你们的慧讯真实登录地址】"
      search_url: "https://【搜索页】?keyword={query}"
      handler: generic
      item_link_contains: "【域名】"
```

---

## 领材网说明（重要）

| 项目 | 说明 |
|------|------|
| 问题 | 公开互联网上**未能核实**到与「领材网」标题一致的独立官网（禁止再默认跳广材） |
| 工具行为 | ID `lingcai` 标记为 **requires_config**，**不配置则跳过，绝不打开广材网** |
| 你要做的 | 把你们浏览器地址栏里**真实的领材网登录地址、搜索地址**填进配置 |

```yaml
platforms:
  enabled:
    - guangcai
    - lingcai
    - jd
  definitions:
    lingcai:
      name: 领材网
      login_url: "https://【领材网真实登录页，从浏览器复制】"
      search_url: "https://【领材网搜索页】?q={query}"   # 必须含 {query}
      handler: generic
      item_link_contains: "【领材网域名】"
      item_link_selector: 'a[href*="【域名】"]'
      detail_price_selectors:
        - ".price"
        - "[class*='price']"
```

配置后验证：

```bash
python -m material_price_audit platforms
python -m material_price_audit login --profile .browser-profile --platforms lingcai
```

**登录弹窗标题必须是领材网自己的，不能是广材网。**

---

## 登录去重

同一 `login_url` 只打开一次。  
例如同时选 `guangcai` + `huixun`（同为 gldjc.com/login）→ 只弹一次广材登录。

---

## 如何自己核对「对不对」

1. 浏览器无痕窗口打开 `login_url`  
2. 看标题栏 / 页头品牌名  
3. 与平台「名称」一致才算正确  
4. 把地址发给信息同事写进 `config.yaml`  

---

## 默认启用

```text
guangcai, jd, 1688
```

不含未配置的 `lingcai`，避免误跳广材。
