# 平台适配说明

本项目只把有专用处理器并纳入回归测试的平台列为“内置维护平台”。网站随时可能改版，适配状态表示代码路径受维护，不代表平台永久可访问或用户拥有会员权限。

| ID | 登录入口 | 搜索方式 | 关键保护 |
|---|---|---|---|
| `guangcai` | `https://www.gldjc.com/login` | `/scj/so.html?keyword=...` | 解析同一厂家报价行及 SSR 数据 |
| `lingcai` | `https://www.hylcw.cn/userInfo/index.html` | `/marketPrice/so.html?...&gjz=...` | 搜索词双重 UTF-8 编码；`.list-item` 同行抽价 |
| `huixun` | `https://services.iccchina.com/login` | 产品库 SPA 搜索框 | 直接填入 Unicode，不拼错误 URL |
| `yize` | `https://www.easybii.com/` | 顶栏「产品信息/信息价」搜索；信息价页名称+规格表单 | 未登录/服务到期独立状态；同行表格抽价，不跨行串价 |
| `zaojiatong` | 可选（仅看数字价时）`member…/login.html?url=…` | **专用适配器** `adapters/zaojiatong.py`：纯 `request` 抓 SSR，**禁止 page.goto 搜价/详情** | R1–R7：永不因 SPA 踢登录循环；无价则「没查到」；互踢弹窗仅登录阶段处理 |
| `jd` | `https://www.jd.com/` | 京东搜索页 | 检测“访问频繁”并停止当前会话；**价仅市场参考** |
| `1688` | 淘宝统一登录入口 | 1688 搜索页 | GBK 查询编码；验证码可等人继续；**价仅市场参考** |

> 电商与造价站分流策略见 [ECOMMERCE_POLICY.md](./ECOMMERCE_POLICY.md)。

## 状态约定

平台处理器不会把异常统一伪装为“没查到”：

| 状态 | 上层行为 |
|---|---|
| `ok` | 继续严格匹配候选 |
| `empty_page` | 当前搜索词无结果，可尝试下一个搜索词 |
| `need_login` | 回到独立登录面板 |
| `no_membership` | 当前站停止并换下一站 |
| `captcha` | 当前会话停止访问 1688 |
| `rate_limited` | 当前会话停止访问京东 |
| `error:*` | 记录错误并换站，不连续刷新 |

## 自定义平台

`config.yaml` 的 `platforms.definitions` 可注册使用通用 DOM 解析器的网站：

```yaml
platforms:
  definitions:
    example:
      name: 示例平台
      login_url: https://example.com/login
      search_url: https://example.com/search?q={query}
      handler: generic
      item_link_contains: example.com/product
      item_link_selector: 'a[href*="/product/"]'
      detail_price_selectors: ['.price']
```

通用适配器只适合结构简单的网站。准备贡献正式内置平台时，应提供专用处理器、登录状态判断、风控状态以及不串价的同行证据测试。
