# Contributing

感谢改进材料询价助手。提交 PR 前请先确认改动不破坏“宁可留空，也不写错价”的核心原则。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
pytest
```

## 提交要求

- 一个 PR 聚焦一个问题或一个平台适配。
- 修复解析和匹配问题时必须增加最小回归测试。
- 平台 HTML 样本必须脱敏，不包含账号、Cookie、真实工程名称或客户数据。
- 不降低严格匹配标准来提高表面命中率。
- 新平台先实现异常状态，再实现候选抽取：登录、会员权限、验证码、限流和空结果必须可区分。
- README 与平台文档只描述已经生效且测试覆盖的能力。

## 测试层次

1. 编码、价格和 DOM 行解析的纯函数测试。
2. 名称、型号、数值与单位冲突测试。
3. Excel 识表、结果列和超链接测试。
4. 使用个人授权账号进行的本地真实站点验证；不要把会话或结果提交仓库。

提交前至少运行：

```bash
pytest
python -m compileall -q material_price_audit
python -m material_price_audit --help
```
