# README 演示截图

这些截图来自 ContextHub 的真实网页界面。TaskFlow、账号 `demo`、对话和代码均为专门编写的演示数据；截图不包含真实用户内容。

- `conversation.png`：按日期查看开发对话，以及消息关联的代码。
- `code.png`：打开对应代码文件，再返回来源消息。
- `codex-import.png`：选择本地目录，复制命令到本机 Codex 继续。
- `upload-preview.png`：上传前检查对话包内容，并选择公开或私有（截取弹窗中的文件选择与检查摘要区域）。
- `version-compare.png`：比较 v1 与 v2，查看新增对话和空标题检查的代码改动。
- `fork.png`：从选中的 v2 创建独立副本，选择副本可见性。

截图中的 `127.0.0.1` 地址属于一次性演示服务，不是可访问的项目链接。

重新生成（需要 Python、Playwright 和 Chromium）：

```bash
python -m pip install playwright
python -m playwright install chromium
python scripts/capture_readme_screenshots.py
```

脚本会创建临时 Hub 和演示账号，通过正常上传、导入下一版和比较版本操作生成截图，完成后自动清理；不会连接线上服务或读写本机 Codex 会话。Fork 图展示确认前的弹窗，没有实际发布副本。
