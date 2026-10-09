# ContextHub：GitHub for AI Conversations

> 一个面向 AI 对话的开源版本控制平台，让任何人的高质量 AI 对话都可以被保存、共享、复用、Fork 和持续演化。

目前提供可运行的迁移 MVP：**本机 Codex → 批量导出 → ContextHub 上传与分享 → 他人下载 → 导入对方 Codex 的上下文**。平台管理对话库，不提供 Agent 市场或模型托管。

## 快速启动

需要 Python 3.12+、Git；只有本地 Codex 连接器需要安装 Codex CLI。产品运行不依赖第三方 Python 包。

```bash
cd /ssd1/code/yqq/Git_for_ai
python3 -m contexthub serve --port 8765
```

打开 <http://127.0.0.1:8765>，注册自己的账号即可使用。默认数据存放在 `.context-data/`。用户名为 3–32 位字母、数字、下划线或连字符；密码为 10–128 个字符。

另开一个终端启动本地连接器：

```bash
python3 -m contexthub connect --port 8766 --open
```

使用终端输出的完整地址打开连接器，地址中包含本次启动的令牌。Hub 和连接器是两个独立进程；连接器只监听本机，Hub 不读取服务器上的 Codex 会话。

## 把对话交给另一个人

1. 在自己的本地连接器中勾选 Codex 会话，预览正文、编辑或移除不想分享的消息，再下载 `.contextpack`。
2. 在 Hub 登录，选择迁移包并查看预览，再决定“仅自己”或“公开分享”。新上传默认私有。
3. 对方在 Hub 阅读并下载完整对话包。公开包可匿名下载；私有包仅上传者登录后可见。
4. 对方在自己的电脑上启动连接器，选择下载的包、检查内容，填写已存在的项目目录，确认导入。
5. 按导入回执中的新会话 ID 使用 `codex resume <会话ID>`。继续对话时由对方自己的 Codex 配置和模型服务处理。

**Codex 兼容性边界：**当前实现通过 `thread/start` 与 `thread/inject_items` 新建上下文会话，不自动调用模型。本机测试版本为 `codex-cli 0.153.4`。注入内容能在隔离存储中持久化，但公开会话读取接口不会将它重建为原生聊天时间线。请勿将“注入成功”理解为原生界面历史完整恢复；实际测试证据见[迁移验证记录](docs/verification/2026-10-09-transfer.md)。

当前导出公开时间线中已完成的用户/助手文本和代码块，遇到未完成或失败的轮次时停止并提示；保留可用的来源标记。重新导出曾导入的会话，可能只获得后来新增的可见消息，请保留原始对话包。图片、附件、工具执行状态和其他不支持的事件会生成兼容警告。迁移包不携带账号凭据、源机器权限、系统指令或项目文件；继续代码任务前需自行取得对应项目文件。自动脱敏只是辅助，公开前应检查正文。

## 命令行使用

```bash
# 列出本机 Codex 会话；可另加 --archived、--subagents
python3 -m contexthub list

# 一次导出多段对话，默认辅助脱敏
python3 -m contexthub export \
  --thread THREAD_ID_1 --thread THREAD_ID_2 \
  --title '我的对话库' -o my-library.contextpack

# 只检查，不写入 Codex
python3 -m contexthub inspect my-library.contextpack
python3 -m contexthub import my-library.contextpack --dry-run

# 导入为多个新会话，目标目录必须已存在
python3 -m contexthub import my-library.contextpack --cwd /absolute/path/to/project

# 上传；密码交互输入，省略 visibility 时为私有
python3 -m contexthub upload my-library.contextpack \
  --url http://127.0.0.1:8765 --username alice --visibility public

# 下载公开包；私有包另加 --username alice
python3 -m contexthub download \
  http://127.0.0.1:8765/api/packages/PACKAGE_ID/download -o downloaded.contextpack

# 生成两段合成对话，可用来体验上传下载
python3 -m contexthub demo -o demo.contextpack
```

现有文件默认不覆盖。批量导入逐条返回回执，部分失败时命令返回非零状态；已成功创建的会话不会回滚，重试前请查看回执。

## 开放格式与存储

`.contextpack` 是可直接解压的 ZIP：包含清单、对话库元数据、规范化 JSON、可阅读 Markdown 和兼容报告。每个文件都有 SHA-256 校验，JSON 是内容主记录；导入时验证 Markdown 与 JSON 一致。格式版本为 `0.1`。

单包最多 200 段对话、每段最多 2,000 条消息、每段 JSON 不超过 5 MiB，总解压内容不超过 100 MiB。未知格式版本、路径穿越、重复条目、哈希不符和不支持的消息角色会被拒绝。

Hub 使用 SQLite 保存账号和索引，按内容哈希保存原始上传包。下载保留上传包的字节内容；同一用户以相同可见性重复上传同一包会返回已有记录。公开上传同时创建内部 Git 快照，私有包不进入 Git。

“删除”会撤销站点访问，底层包文件和内部 Git 快照暂时保留，没有实现物理清除或备份清理。数据目录应按私人应用数据管理，不要作为静态文件目录公开。

## 自部署与当前范围

```bash
python3 -m contexthub serve --host 0.0.0.0 --port 8765 --data /srv/contexthub-data
```

当前版本适合本地或受控环境试用。公网部署仍需 HTTPS 反向代理、请求与存储配额、备份及运维配置。备份时停止服务，保存整个数据目录，再以同一 `--data` 路径启动即可恢复。不要把本地连接器代理到公网。

已实现：账号、公开/私有包、正文搜索、批量 Codex 迁移、上传预览、阅读、JSON/Markdown 导出、完整包下载、校验、去重和持久化。

后续平台工作：粘贴和其他平台导入、库内版本浏览与差异比较、Fork 及来源谱系、贡献请求、讨论与发现。当前内部 Git 快照尚未提供这些协作操作。

## 开发与验证

```bash
python3 -m unittest discover -v
python3 scripts/verify_codex_transfer.py
```

第二条使用隔离临时存储与合成内容，需要 Linux、`bwrap` 和 Codex CLI。不会批量读取真实用户对话，也不会向真实模型发送请求。

浏览器验收脚本使用可选的 Playwright 开发依赖，产品运行不需要它：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
python3 scripts/verify_web.py
```

更多细节：[贡献指南](CONTRIBUTING.md)、[实现计划](docs/superpowers/plans/2026-10-09-codex-transfer-mvp.md)、[测试记录](docs/verification/2026-10-09-transfer.md)。平台源码采用 [MIT](LICENSE)；该许可不自动适用于用户上传的对话内容。

## 产品文档

- [当前产品与架构设计](docs/superpowers/specs/2026-10-08-contexthub-conversations-design.md)
- [Codex 导入导出核查](docs/research/2026-10-08-codex-portability.md)
- [早期 Agent 构想](ContextHub_Git_for_AI_Agents_Idea.md)：历史记录，当前定位以对话库设计为准。
