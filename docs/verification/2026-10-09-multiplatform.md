# 多平台导入、分类与原文存档验收

日期：2026-10-09（Asia/Shanghai）。应用版本：0.4.0。已部署至 <https://contexthub.reflectstars.dev/>。

## 交付范围

- Codex、ChatGPT、Claude、DeepSeek、其他五类来源；Claude Code 归 Claude，DeepSeek Harness/dsh 归 DeepSeek，具体适配器随来源保留。混合库支持多个平台标签。
- 支持现有 contextpack、Claude 对话 JSON、ChatGPT 当前分支 JSON、通用/DeepSeek 消息与文本片段、Claude Code JSONL、受支持 dsh 事件日志、安全 ZIP 中的唯一会话文件，以及 ContextHub 规范化 JSON 的再次导入。
- 无法识别的 UTF-8 JSON/JSONL/TXT/Markdown 可显式按原文存档，保留 BOM 与 CRLF。未知格式不自动归档，二进制原件托管不在本次范围。
- 上传预览显示格式、来源、遗漏及兼容报告，需要确认的转换或存档只有在用户确认后才能保存。私有为默认，下载和所有写操作继续要求登录。
- 新增 CLI `convert`；`inspect`、`upload` 可接受原生导出文件、来源声明与存档模式。转换与上传使用同一适配器。
- 分类索引随创建、版本、Fork 和贡献合并原子更新；查询先执行 ACL、搜索和分类过滤，再限制 200 条结果。旧库自动回填索引，已有包字节及版本 ID 不变。

完整格式边界、导出入口和使用方法见[多平台导入指南](../import-formats.md)。没有宣称原平台账户回导、原生 UI/运行状态恢复或模型续聊已经验证。

## 自动化验证

最终 `python3 -m unittest discover -s tests -q`：**212 项通过，69.401 秒**。

新增覆盖包括：

- 34 项导入适配测试：中文/代码、结构识别、来源冲突、Claude 空文本块回退、ChatGPT 分支、dsh 版本、日志角色冲突、隐藏/推理/工具定向消息、规范化 JSON 保真及原包字节不变。
- JSON 的重复键、非有限数、过深或过多节点；ZIP 的路径穿越、符号链接、重复条目、歧义会话文件、解压体积与条目数限制。导出 ZIP 不落地解压，不收集账号文件或附件。
- 9 项平台分类测试：来源别名、混合库、私有隔离、历史版/Fork、旧库幂等迁移、超过 200 条时先筛选、贡献合并、容量失败回滚、并发快照一致性。
- 6 项 HTTP/CLI 集成测试：登录/CSRF、预览不落库、告警必须确认、明确原文存档、平台过滤、完整下载、CLI 转换上传，以及原文存档不启动 Codex 会话恢复。

独立审查复现并修复两项导入问题：隐藏分析文本可能混入 ChatGPT 正文；通用分支 JSON 可能被串平。修复后明确隐藏、非可见通道和工具定向消息会省略并报告；未支持的通用分支结构拒绝转换。原生过滤不改变已有 contextpack 或 canonical JSON。

## 浏览器验证

五套脚本通过，均使用临时数据库与合成对话：

- `verify_web.py`
- `verify_versions_web.py`
- `verify_contributions_web.py`
- `verify_public_access_web.py`
- `verify_multiplatform_web.py`

新脚本验证来源筛选、私有库隔离、历史版标签、JSON 导出、中文遗漏提示、未勾确认不能提交、原文存档字节保真、匿名下载/来源复制登录后继续原快照、换账号后的私有 ACL，以及切换文件/参数时旧预览不能覆盖新选择。移动视口无横向溢出，无 JavaScript 错误。最终适配器修复及提示翻译后再次通过多平台浏览器验收。

为避免生产静态文件与旧服务短暂不兼容，先在 `/tmp/contexthub-multiplatform-ui/` 完成前端并通过测试时的 STATIC 路径替换验收；部署时停服统一替换三份静态文件，再启动新版后端。

## 生产更新与验收

17:18:40 完成 0.4.0 启动。停服时完整备份原数据至 `.artifacts/backups/20261009-171839-before-multiplatform/`，未向生产添加验收账号或合成对话，未人工读取生产私有正文。数据库迁移由应用执行。

公网浏览器实际验证：

- 主页和配置返回 200、版本 0.4.0，显示全部/Codex/ChatGPT/Claude/DeepSeek/其他选择。
- Claude、DeepSeek 分类列表返回 200，非法分类返回 422。
- 匿名完整下载、原生文件预览和上传返回 401；点击上传会打开登录对话框。
- 检查的读取响应为 `Cache-Control: no-store` 与 Cloudflare DYNAMIC，没有私有条目泄露。
- 桌面、移动页面加载正常，无 JavaScript 错误。
- Hub 与隧道均保持 enabled、active/running；更新后的自动重启次数为 0。

机器证据与截图位于忽略目录 `.artifacts/`：`multiplatform-web-verification.json`、`multiplatform-deployment.json`、`multiplatform-live-verification.json`、`multiplatform-live-desktop.png`、`multiplatform-live-mobile.png`。未调用模型或读取真实 Codex 历史。
