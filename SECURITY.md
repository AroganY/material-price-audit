# Security Policy

## 敏感数据

以下内容只能保存在本机，**禁止**提交 Issue、PR、公开日志或文档截图：

| 类型 | 位置 |
|------|------|
| 登录 Cookie / 会话 | `.browser-profile/` |
| 真实询价表 | `data/input/*`、任意 `*.xlsx` |
| 结果与证据 | `data/output/*`（仅保留 `.gitkeep`） |
| 用户设置 / API Key | `data/user/settings.json` 等 |
| 本地配置 | `config.yaml`（用 `config.example.yaml` 作模板） |
| 映射缓存 | `data/mapping-cache/` |
| 调试截屏 | `**/huixun_debug/`、临时 `*_debug/` |

仓库 `.gitignore` 已默认排除上述路径。发版前请执行：

```bash
git status   # 确认无 xlsx / settings / browser-profile
rm -rf data/output/* data/user/* .browser-profile
touch data/output/.gitkeep
```

`docs/images/` 若含真实工程材料名，请用演示数据重跑  
`python scripts/capture_screenshots.py` 后再提交。

若敏感数据被误提交，应立即撤销密钥/会话，并从 Git 历史中清除。

LLM 功能默认关闭。启用后，陌生表头预览以及语义待核所需的材料名称、规格和候选证据会发送到 `api_base` 指向的服务。保密项目应保持关闭，或只使用经过授权的私有接口。

向导默认只监听 `127.0.0.1`。不要在不受信任的网络上把 `--host` 改为公网地址；本工具没有面向公网部署所需的用户认证。

## 报告漏洞

请使用仓库托管平台的私密安全报告功能联系维护者，不要在公开 Issue 中披露可复现的凭据泄漏、任意文件读取或远程执行问题。

## 使用边界

本项目不会也不接受绕过验证码、付费会员权限或平台访问控制的功能。使用者必须拥有相关账号与数据的合法访问权。
