# 项目开发约定

- 用户指定本项目只在 `/ssd1/code/yqq/Git_for_ai` 目录下开发。源码、测试脚本与文档的修改均在此目录进行，不另建 Git worktree、checkout 或其他开发副本。
- `/ssd1/code/yqq/ContextHub_next_20261010` 是此前的历史开发工作树，后续不再用于开发。本目录已包含其 0.8.0 成果。
- 此目录同时是 `contexthub.service` 用户服务的工作目录。更新运行版本时，保留 `.context-data` 与现有服务、隧道配置，避免旧后端与新静态资源混用。
