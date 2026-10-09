# 参与 ContextHub

当前优先级是可验证的 Codex 导出、上传、下载与导入。

## 本地开发

需要 Python 3.12+ 和 Git。运行产品与单元测试不需要额外 Python 依赖。

```bash
python3 -m unittest discover -v
python3 -m contexthub serve --data .context-data --port 8765
python3 -m contexthub connect --port 8766
```

请使用 `python3 -m contexthub demo` 的合成数据测试，不将私人对话、凭证、本机 Codex 数据库或迁移包提交进源码库。

## 模块边界

- `package.py`：迁移格式、校验、阅读版、辅助脱敏。
- `codex.py`：本地 JSON-RPC 协议与显式转换；导入不发起模型生成。
- `store.py`：账号、会话、上传包、访问控制与公开 Git 快照。
- `server.py`：Hub 和本地连接器两种隔离服务模式。
- `__main__.py`：终端命令。
- `static/`：不依赖构建工具的浏览器界面。

改动迁移格式时增加往返和损坏输入测试；改动 Codex 适配时同时报告版本、历史模式、API 回读、持久化、原生 UI 和实际续聊的验证范围。不要把请求成功等同于全部兼容。

提交说明应包含具体问题、结果、测试命令及已知限制。平台软件采用 MIT 许可；用户上传的对话内容不因此自动获得 MIT 许可。
