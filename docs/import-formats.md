# 多平台对话导入与分类

ContextHub 保存的是可阅读、可版本管理的对话资料。来源分类为 Codex、ChatGPT、Claude、DeepSeek、其他；Claude Code 归 Claude，DeepSeek Harness（dsh）归 DeepSeek，具体适配器保留在来源记录。一个库可以包含多个平台，筛选任意一个来源都能找到它。分类是来源声明，不证明模型或内容的真实性。

## 上传流程

1. 登录后点击「上传对话包」，选择 `.contextpack`、导出 ZIP、JSON 或 JSONL。
2. 默认自动识别结构；通用 `messages` JSON 不能可靠推断平台时，选择来源分类。
3. 展开预览检查消息、来源和兼容提示。遇到附件、工具、分支等遗漏，需要勾选已阅读提示后再提交。
4. 默认保存为私有库；明确选择公开后，任何人可阅读，下载和后续操作仍须登录。

原生导出转换为现有 `.contextpack` 格式，沿用阅读、搜索、下载、Fork、版本与贡献功能。旧包保持原始字节与版本 ID，旧库自动建立分类索引。平台筛选在权限检查内进行，不会显示其他人的私有库。

## 格式与边界

| 输入 | 处理方式 | 边界 |
| --- | --- | --- |
| `.contextpack` | 校验后原样保存 | 格式仍为 0.1；不重新打包或覆盖原版本 |
| Claude 对话 JSON | `chat_messages` 中的 human/assistant 文本及文本块 | 图片、附件、思考和工具状态不作为对话正文导入 |
| Claude Code JSONL | 提取受支持 user/assistant 记录的文本 | 是按记录顺序的文本摘录，不重放工具、运行环境或原生会话状态 |
| ChatGPT JSON | 沿 `current_node` 的 parent 链选取当前分支 | 不混合不同回答分支；分支歧义、缺失节点或循环会拒绝转换 |
| DeepSeek 通用对话 JSON | 识别 role/content、受支持的 RESPONSE 文本片段 | 通用结构需手选 DeepSeek；不凭文件名推断；不导入思考片段 |
| DeepSeek Harness（dsh）JSONL | 从受支持日志事件提取用户与助手可见文本 | 历史文本摘录，可能包含被替换的历史记录；不声称恢复最终活跃分支或运行状态 |
| 通用 messages / ContextHub 对话 JSON | 保存可识别的角色正文与允许的来源字段 | 没有可靠来源时归「其他」，可以明确声明分类 |
| 受支持的导出 ZIP | 读取唯一明确的会话文件 | 不导入账号文件、附件和子代理资料；不解压到服务器文件系统 |
| 未识别的 UTF-8 JSON/JSONL/TXT/Markdown | 手动选择「原文存档」 | 原文保留为一个资料条目，不自动推断角色、不执行文件 |

适配器接受有明确结构的文件，不保证任意版本或第三方插件的同名 JSON 都能解析。未识别结构不会悄悄截断或自动降级；可改为原文存档。dsh 带头部日志支持 v2–v4；旧 packed 编码、zstd 与未来版本不直接解析，应先取得支持的 JSONL 或解压后的 UTF-8 原文再存档。无头部但结构明确的事件日志也只按历史文本摘录。

原生导入省略明确隐藏的消息、分析/推理通道、用户系统指令及工具定向消息，并提示确认。通用 `messages` 中出现不支持的分支字段时拒绝转换，避免把不同回答串成一段；可选择原文存档。已规范化的 ContextHub JSON 和 `.contextpack` 不重新应用这些原生过滤规则。

原生转换保留受支持正文，不保留整个账号导出原件；请自行保存本地原件。若希望保留完整文本文件，主动使用原文存档模式。二进制附件与任意 ZIP 的原件托管不在本次范围内。

## 原文存档

原文存档支持 UTF-8 `.json`、`.jsonl`、`.txt`、`.md`，保留初次上传文本的 BOM 与换行。不会自动脱敏，以免改变原文，因此可能包含账号信息、路径或令牌；必须在预览中确认内容后才能保存或公开。下载的名称只保留安全文件名，不带原机器路径。

存档仍可按来源分类、搜索、设为私有或公开、下载完整包。阅读页提供「下载存档文本」；登录后下载当前选中版本的文本。如果编辑了存档内容，下载的是该版本修改后的文本，不再是初次上传原件。未解析的存档不能通过本地连接器当作 Codex 会话恢复。

## 命令行

```bash
# 检查文件和转换报告，不保存到 Hub
python3 -m contexthub inspect conversations.json

# 转换为通用包；有遗漏提示时先检查，再明确接受
python3 -m contexthub convert conversations.json -o conversations.contextpack
python3 -m contexthub convert session.jsonl --dry-run
python3 -m contexthub convert session.jsonl --confirm-import -o session.contextpack

# 通用消息 JSON 手选来源
python3 -m contexthub convert deepseek.json --platform deepseek -o deepseek.contextpack

# 未知结构原文归档
python3 -m contexthub inspect unknown.json --platform deepseek --mode archive
python3 -m contexthub convert unknown.json --platform deepseek --mode archive \
  --confirm-import -o archive.contextpack

# 直接上传平台导出文件，默认私有；密码在终端交互输入
python3 -m contexthub upload conversations.json \
  --url https://contexthub.reflectstars.dev --username alice
# 若报告要求确认，检查后添加 --confirm-import
```

原生输入最多 16 MiB，转换后仍受每包 200 段对话、每段 2,000 条消息、每个对话 JSON 5 MiB、总解压内容 100 MiB 的限制。大文件应先拆分；不会通过静默删减内容凑到上限。公开实例还有独立请求和存储预算。

## 官方导出入口与证据

- [Claude 导出说明](https://support.claude.com/en/articles/9450526-export-your-claude-data)：在网页或桌面应用的隐私设置导出数据；官方没有承诺可将导出迁移回另一私人 Claude 账号。
- [Claude Code 命令说明](https://code.claude.com/docs/zh-CN/commands)：`/export` 输出文本，不能把它与 Claude 网页 JSON 或原生 JSONL 会话记录视为同一种格式；文本可用原文存档。
- [DeepSeek 官方隐私政策](https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html)：账号区域的设置 → 数据管理提供历史对话导出。该说明不承诺固定 JSON 字段结构。
- [DeepSeek Harness 导出说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session-query/session-log-export/README.md)、[事件类型](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/session/src/types.ts)与[v2 记录编码](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-format-v1-to-v2/src/codec.ts)：dsh 会话事件与聊天网页导出不同，运行状态和工具数据不属于通用正文。
- [ChatGPT 导出说明](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data)：通过数据控制导出账户历史。账号导出 ZIP 中会话以外的文件不会上传到对话库。

这些入口可导出内容，不代表原产品提供任意对话回导接口。ContextHub 的格式转换验证也不代表原生界面恢复、工具重放或模型继续调用已经验证。
