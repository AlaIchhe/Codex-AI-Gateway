# Codex AI Gateway 使用指南

本文说明如何运行、配置、部署和验证 Codex AI Gateway。当前网关是模型中心
架构：上游自动探测协议，模型聚合为可路由目录，数据面使用唯一全局网关
token，本机 Codex 配置和 `model_catalog.json` 由网关自动接管。

## 1. 本地启动

```bash
uv sync --extra dev
uv run uvicorn codex_ai_gateway.app:app --host 127.0.0.1 --port 8787
```

如需本地构建前端管理界面：

```bash
bun install
bun run build
```

浏览器打开 <http://127.0.0.1:8787> 进入管理界面。管理面按规格使用无登录
模式；生产部署时应只将管理端口暴露给可信管理网络或本机。

## 2. 管理上游

在“上游”页选择创建方式。

### 预设 Provider

选择内置预设后只填写 API Key。名称、Base URL、官方文档地址和提取器由服务端
从只读预设目录填充。创建或重新探测时，网关通过 HTTP 获取官方文档页并解析模型
列表；预设不会请求上游 `/models`，也不会把 API Key 发送到文档页面。网页获取或
解析失败时，当前预设模型会被清空并停止路由，历史快照和失败原因仍可在详情中查询；
下一次网页探测成功后才恢复，不提供手工模型列表修正。

### 自定义 Provider

自定义 Provider 继续填写名称、Base URL 和 API 凭据。Base URL 必须是 API 根，例如：

```text
https://provider.example.com/v1
```

保存后网关自动完成：

- custom Provider 的 `/models` 账号与模型发现检查；
- `/responses` 与 `/chat/completions` 空请求探测；
- OpenRouter 元数据维护与目录发布；
- 本机 Codex 自动调和。

探测不会发送计费生成请求。上游编辑后，原协议确认会重新探测；预设 Provider
同时重新获取官方文档模型列表。

## 3. 模型与路由

“模型”页展示所有匹配成功的规范模型。每个模型可以设置上游优先级；未设置
覆盖的模型继承全局默认顺序。

数据面请求的 `model` 字段接受规范模型的 `slug` 或 `openrouter_model_id`。
网关只路由到已启用、协议确认且与规范模型匹配的 offering，并在可重试上游
错误上按优先级切换备用上游。

## 4. 网关 token

数据面 `/v1/responses` 必须携带全局网关 token：

```http
Authorization: Bearer <gateway-token>
```

完整 token 只在生成或轮换响应中出现一次；之后管理 API 只显示前缀、后四位
与最后使用时间。token 与上游凭据都写入系统 Secret 后端，生产环境禁止
memory 后端，也不允许明文文件后端。

## 5. Codex 自动集成

网关会自动发现本机 `~/.codex/config.toml` 并维护两类受管对象：

- `config.toml`：`codex-ai-gateway` provider 块、`model_provider`、
  `model_catalog_json` 和官方 `experimental_bearer_token`；
- `model_catalog.json`：按官方 `ModelInfo` / `ModelsResponse` schema 独占
  整体覆盖，非网关条目会被移除。

`config.toml` 中网关未管理的键、注释与排序保持原样。每次应用都会创建恢复
点，并在 schema、连通性、启动配置检查失败时自动回滚。目录发布变更会触发
preview → apply → 校验，无需人工确认。

## 6. 用量与脱敏

“用量”页展示 attempt 级结果、上游归属、协议、token 类别与成本口径。
provider 上报用量优先于本地估算；没有 provider 用量的记录标记为
`estimated`。导出与明细不包含 prompt、response 正文或 bearer token 明文。

## 7. 部署（使用 Release 构建产物）

正式部署请使用 GitHub Releases 提供的构建产物，不要在目标机器上现场构建：

1. 在仓库的 [Releases](../../releases) 页面下载 `codex-ai-gateway-<版本>.zip`。
2. 解压后得到：
   - `dist/`：前端管理界面构建产物；
   - `wheels/`：后端 Python wheel；
   - `README.md`：本说明文档。
3. 在解压目录内安装并启动：

   ```bash
   cd codex-ai-gateway-<版本>
   uv venv
   uv pip install wheels/*.whl
   uv run uvicorn codex_ai_gateway.app:app --host 127.0.0.1 --port 8787
   ```

网关启动时会自动加载解压目录下的 `dist/` 前端产物；如需放在其他位置，可
通过环境变量 `CODEX_AI_GATEWAY_FRONTEND_DIST` 指定路径。

发布新版本时推送 `v*` 格式的 tag（例如 `git tag v0.1.0 && git push origin v0.1.0`），
GitHub Actions 会自动完成前端与后端构建，并把产物压缩包附加到对应 Release。
