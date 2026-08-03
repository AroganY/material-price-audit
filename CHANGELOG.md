# 更新日志

## v0.3.2 — 2026-08-03

首个面向公众的可下载 Release 包。

### 功能

- 本地 Web 向导询价（广材 / 慧讯 / 领材 / 易择 / 造价通 / 京东 / 1688）
- 实用 / 严格 / 宽松匹配；候选待核工作台
- 多站 Worker 调度、登录会话复检、询价中「我已登录，继续」
- 可选大模型辅助检索（不编造价格）；结果须自行核对
- 导出 result / RFQ / evidence；任务历史

### 安装包

| 文件 | 说明 |
|------|------|
| `material-price-audit-0.3.2-portable.zip` | 便携包：解压后 `./install.sh` → `./start.sh` |
| `material_price_audit-0.3.2-py3-none-any.whl` | pip 安装用 wheel |
| `material_price_audit-0.3.2.tar.gz` | 源码包 sdist |

### 文档

- README：使用说明、若亘造价助手小程序、OA 官网 https://www.rogan.asia/
- INSTALL.zh.md：中文安装步骤
