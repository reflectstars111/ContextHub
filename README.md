# ContextHub：GitHub for AI Conversations

> 一个面向 AI 对话的开源版本控制平台，让任何人的高质量 AI 对话都可以被保存、共享、复用、Fork 和持续演化。

当前版本 **0.3.0**：支持 Codex 导入导出、分享、版本历史、Fork、网页编辑和差异比较，并可通过贡献请求将改进送回上游，由上游拥有者审阅后合并。

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

## Fork、编辑与版本管理

1. 打开对话库，用“版本历史”选择要复用的版本，点击“Fork 到我的对话库”。默认创建私有副本，也可明确选择公开。
2. 在自己的最新版本点击“编辑副本”，修改标题、背景和消息，或新增、移除、移动消息。
3. 如果已经在 Codex 中继续对话并导出新的 `.contextpack`，选择“导入对话包作为下一版”，检查内容后用它替换整份草稿。平台不会自动推测不同线程之间的消息对应关系。
4. 填写保存说明，点击“预览变化”，检查后保存。公开库的按钮为“发布新版本”，保存后该版本即可被访问者阅读和下载。
5. 通过“比较版本”查看增删修改及顺序变化；选择历史版本可阅读、下载或复制固定版本链接。新版本不会覆盖原始文件，Fork 的修改不会改变上游。

草稿只保存在当前浏览器页面，尚无服务器自动保存；离开或刷新会提示丢弃。两个页面同时修改时，较晚提交会收到冲突提示并保留当前草稿，不会覆盖已保存的新版本。

当前每个库的公开/私有设置在创建时固定，不支持原地转换。私有来源只能由所有者 Fork 为私有副本。Fork 复制所选快照并记录来源版本，不复制全部历史；上游删除后，该独立副本仍保留，来源信息会隐藏。

差异摘要统计全部变化；正文详情最多显示 200 条消息，每段最多 4,000 字符，超出时明确提示下载完整版本。服务端保留已有来源并添加人工编辑记录，但来源声明不等于模型真实性认证。

## 把改进贡献回上游

1. 从公开库的固定版本创建公开 Fork，在自己的 Fork 中编辑并保存新版本。
2. 选择要贡献的版本，点击“贡献给上游”，填写标题和说明。提案固定这个源版本及原始 Fork 起点；之后继续编辑 Fork 不会自动改变已提交的内容。
3. 上游拥有者打开贡献请求，查看相对 Fork 起点的变化，然后点击“检查合并”。平台对共同起点、上游当前版本和固定源版本进行三方比较，生成候选或列出冲突。
4. 若可合并，点击“查看本次合并变化”，核对将发布的准确内容；需要完整内容时下载候选包。确认后点击“合并并发布新版本”，上游得到一个新版本，原有历史与 Fork 保持可访问。

当前只支持**公开的直接 Fork 向它的公开上游**贡献，私有库不参与。源库拥有者可提交或“更新提案版本”；上游拥有者才可检查和合并。双方均可评论、关闭或重新打开请求，其他人可匿名阅读提案和讨论。每条请求最多 200 条评论，每条最多 4,000 字符；已合并的请求不可重开。

更换提案版本或关闭后重开都会清除旧检查。上游在检查后发布了新版，则候选失效，必须重新检查和审阅；源库发布未纳入提案的其他版本不会使检查失效。合并直接采用已检查候选的包字节与 Git 提交，保留上游和源版本两个父提交；重复确认不会重复创建版本。

合并按稳定 ID 和字段进行：独立修改可组合，双方对同一字段的不同修改、删除与编辑或无法确定的顺序变化会阻止合并。当前没有图形冲突解决器；双方需讨论后在 Fork 中编辑、保存新版本，再显式更新提案并重新检查。必要时需基于最新上游重新 Fork 并提交新提案。任何依赖库被删除后，相关贡献、候选下载和合并操作都会被阻断。

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

Hub 使用 SQLite 保存账号、索引、不可变版本、来源关系、贡献请求及评论，按内容哈希保存原始上传包与合并候选。下载选定版本保留该版的字节内容；同一用户以相同可见性重复上传当前已有包会返回已有记录。公开版本建立带父提交的内部 Git 历史，公开 Fork 连接来源的公开提交，贡献合并保留两个父提交；私有包及私有版本不进入 Git。数据库事务决定新版本是否发布，Git refs 可由数据库恢复。

升级现有 0.1 或 0.2 实例前先停止服务并备份数据目录，然后使用原来的 `--data` 路径启动。0.1 的已有上传记录会自动迁移为 v1，保留库 ID、原包字节、创建时间和 Git 提交；0.3 会增加贡献与评论表。迁移可重复运行，不会再次生成初始版本。

“删除”会撤销站点访问，底层包文件和内部 Git 快照暂时保留，没有实现物理清除或备份清理。数据目录应按私人应用数据管理，不要作为静态文件目录公开。

## 自部署与当前范围

```bash
python3 -m contexthub serve --host 0.0.0.0 --port 8765 --data /srv/contexthub-data
```

当前版本适合本地或受控环境试用。公网部署仍需 HTTPS 反向代理、请求与存储配额、备份及运维配置。备份时停止服务，保存整个数据目录，再以同一 `--data` 路径启动即可恢复。不要把本地连接器代理到公网。

已实现：账号、公开/私有库、正文搜索、批量 Codex 迁移、上传预览、阅读、JSON/Markdown 导出、完整包下载、校验、去重、不可变版本历史、固定版本链接、Fork、网页编辑、整包更新、消息差异比较，以及公开贡献请求、双方评论、三方检查与候选审阅合并。

后续平台工作：服务器草稿、图形冲突解决、许可与发布管理、粘贴和其他平台导入、站内通知与发现。当前还没有原生 Git push/clone 服务。

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
python3 scripts/verify_versions_web.py
python3 scripts/verify_contributions_web.py
```

更多细节：[开发贡献指南](CONTRIBUTING.md)、[贡献回流实现计划](docs/superpowers/plans/2026-10-09-contributions.md)、[贡献回流验证记录](docs/verification/2026-10-09-contributions.md)、[版本管理验证记录](docs/verification/2026-10-09-versions.md)、[Codex 迁移证据](docs/verification/2026-10-09-transfer.md)。平台源码采用 [MIT](LICENSE)；该许可不自动适用于用户上传的对话内容。

## 产品文档

- [当前产品与架构设计](docs/superpowers/specs/2026-10-08-contexthub-conversations-design.md)
- [Codex 导入导出核查](docs/research/2026-10-08-codex-portability.md)
- [早期 Agent 构想](ContextHub_Git_for_AI_Agents_Idea.md)：历史记录，当前定位以对话库设计为准。
