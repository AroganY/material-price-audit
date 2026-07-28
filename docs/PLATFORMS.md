# 平台官网对照表（用户核实 + 实测标题）

> 登录哪个站，浏览器就必须打开哪个站。  
> **禁止**把领材/慧讯指到广材。

---

## 造价信息站（已按用户提供 + 实测）

| ID | 名称 | 登录 / 首页 | 搜索入口 | 实测标题 |
|----|------|-------------|---------|----------|
| **guangcai** | **广材网** | https://www.gldjc.com/login | https://www.gldjc.com/scj/so.html?l=1&keyword={query} | 登录-**广材网**-… / 广材网-建筑工程造价… |
| **huixun** | **慧讯网** | https://services.iccchina.com/apply_trial | https://services.iccchina.com/iccHome | **慧讯网**-RCC瑞达恒旗下_建筑行业价格信息查询平台 |
| **lingcai** | **领材网** | https://www.hylcw.cn/lcIndex.html | https://www.hylcw.cn/lcIndex.html?keyword={query} | **领材网**-首页 |
| gldjc_hangqing | 广材行情 | https://hangqing.gldjc.com/ | 同域名 | 钢材行情-广材网 |
| gldjc_xunjia | 广材询价 | https://xunjia.gldjc.com/ | 同域名 | 广材人工询价 |
| jcnet | 建材在线 | https://www.jc.net.cn/ | 同域名 | 建材在线-建材信息价格服务 |

### 说明

- **慧讯网** 是 RCC 瑞达恒旗下 `iccchina.com`，**不是** 广材网 `gldjc.com`。  
- **领材网** 是 `hylcw.cn`，**不是** 广材网。  
- 登录时若两站 URL 不同，会分别打开；同 URL 才去重。

---

## 电商补充

| ID | 登录 |
|----|------|
| jd | https://www.jd.com/ |
| 1688 | https://www.1688.com/ |
| taobao / tmall / zkh / suning / mysteel | 见 platforms.py |

---

## 默认启用

```text
guangcai, huixun, lingcai, jd, 1688
```

```bash
python -m material_price_audit login \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai
```

应依次看到：

1. 广材网登录页  
2. 慧讯网（iccchina）试用/登录页  
3. 领材网首页（hylcw.cn）  
