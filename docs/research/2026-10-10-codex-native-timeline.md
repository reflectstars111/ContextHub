# Codex 原生文本时间线迁移验证

验证日期：2026-10-10（Asia/Shanghai）。验证对象：本机 Codex CLI **0.153.4**、Python 3.12、Linux。

此前 `thread/inject_items` 只注入模型上下文，不能在公开时间线读回。新增默认 `timeline` 模式，以明确版本化的本地 rollout 适配器重建用户与助手文本消息；每次导入通过独立进程 `thread/read` 和 `thread/resume` 验证后返回成功。真实 Codex 的 `thread/list` 发现和再导出另由隔离验收脚本验证。保留显式 `--mode context` 兼容路径，不在失败时悄悄降级。

```sh
ctx codex import library.contextpack --cwd /path/to/project
ctx codex import library.contextpack --cwd /path/to/project --mode context
```

成功回执包括新 `thread_id`、`resume_command`、实际验证状态和限制。支持版本暂精确锁定为 `0.153.4`；其他版本在写入前拒绝并提示选择上下文模式。适配器使用 `initialize.codexHome` 定位接收端，不猜测或扫描用户历史。

## 实现与权限边界

- 每次生成新 UUID，以排他创建方式写入新的 `sessions/YYYY/MM/DD/rollout-*.jsonl`，不覆盖现有文件；写入失败只清理本次新文件。
- 父目录使用 `safe_fs` 锚定，拒绝符号链接、Windows junction/reparse point 和不支持的本地路径。
- 仅重建明确的 user/assistant 文本、显示事件和完成回合；省略工具参考消息。角色校验、2,000 消息和 5 MiB 限额在变更前执行。
- 模型提供方取自接收端 `config/read`，工作目录取自用户明确选择；不复制源机器配置、权限、项目指令、工具状态、凭据或系统/开发者消息。
- 调用新进程读回磁盘历史并恢复同一 ID，没有调用 `turn/start`。恢复成功后退出；实际模型调用留给用户后续主动发起。
- 若文件已创建而读回失败，回执标记 `failed` 并保留新路径供排查；既有会话不变，绝不假报完整恢复。

排他创建可使并发 Codex 扫描短暂看到尚未写完的新文件；产品自身等待完整写入、文件 fsync 和关闭后才执行读回及恢复。实现不修改现有 rollout，也不直接修改 Codex SQLite 数据库。

## 验证结果

以下命令仅使用合成数据：

```sh
python3 -m unittest tests.test_codex tests.test_codex_timeline tests.test_cli tests.test_import_api tests.test_windows_codex -q
python3 scripts/verify_codex_timeline.py --output /tmp/contexthub-native-timeline-verification.json
```

第一条命令通过 39 项测试。第二条将整个导入器和 Codex 进程放入 bwrap 无网络命名空间，并将空临时目录覆盖挂载到 Codex 存储路径；真实目录只作为挂载目标，不读取其内容。配置指向无监听端口，**模型调用次数为 0**。

实测通过：中文、换行、代码块、连续助手消息、尾部用户消息逐条一致；新进程磁盘读回；新进程恢复；会话列表可见；重新导出与输入文本一致；接收端仍为 `readOnly` / `on-request`；已有合成文件和接收端配置字节不变。接口测试另覆盖显式上下文模式、模式拒绝、兼容错误可见、上游原始错误不回显。

## 仍需明确的限制

这会重建**可恢复的原生文本时间线**，并非完整复制原始原生会话状态。新会话/消息/回合 ID 与导入时间会重新生成；原始回合划分和时间戳不保证保留，工具执行、附件、隐藏推理、权限及机器文件不迁移。CLI/桌面 GUI 实际渲染未单独验收，真实模型续聊未调用，因此回执仍将 `ui_history_verified`、`model_context_verified` 标为 false。真实 Windows Codex 0.153.4 实机尚未测试；Windows 文件安全适配器由独立测试覆盖。

## 协议依据

先读取本机 CLI 生成的 `InitializeResponse`、`ThreadResumeParams` 和 `MessagePhase` 类型，再校对固定版本的 [OpenAI Codex 协议源码](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/protocol/src/protocol.rs)。`thread/resume.history` 在生成接口中明确仅供 Codex Cloud 使用，因此产品未采用该接口。当前本地 rollout 不是稳定公开导入协议；版本门控和每次独立进程读回是必要的兼容检查，后续版本必须单独验证后才能加入允许列表。
