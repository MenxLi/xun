# 更新日志

## 1.1.1

### 容器与多用户

- 修复 `xunc <目录>` 挂载后工作区不符的问题。
- 新增 `xunx pause <user>` / `xunx resume <user>`：暂停或恢复某个用户的容器而不丢弃其状态。
- 修复令牌以 `-` 开头时被 `xuns` 当作选项解析的问题，容器启动改用 `--token=<token>` 传参。

### Web 服务与前端

- 优化ws消息发送：消息与命令带上 `client_id`，收到服务端 `accepted` 回执后才清空输入框与附件；重连后自动重发，服务端按 `client_id` 去重以避免重复执行。
- 文件信息中的体积改为按量级展示（B / KiB / MiB ...）。

### 扩展系统

- 启动时不再打印 `Loaded extensions:` 提示。

## 1.1.0

### 扩展系统（新增）

- `$XUN_HOME/extensions/` 下的源文件在每个智能体初始化时自动加载，支持包形式 `{name}/setup_extension.py` 与扁平形式 `{name}.py`；入口 `setup_extension(ctx)` 。
- 导入或初始化失败只告警，不阻断启动；配置项 `enable_extensions` 可关闭。
- 新增显示层钩子 `before_display_info` / `before_display_warning` / `before_display_error`，可拦截智能体输出；新增 `/extensions` 命令与仓库内示例。

### 核心与工具

- 会话压缩分级：先回收旧工具结果，仍超标才转摘要压缩，摘要后仍超标可继续再压；摘要正文不再混入 info 消息。
- 取消事件改为链式结构，父智能体取消级联到子智能体；Web 端等待输入时同样响应取消。
- 生命周期状态独立为 `agent_state.py`；显示方法抽为 `AgentDisplayMixin` 并纳入类型状态约束。
- 子智能体 getter 参数改为 dataclass `AgentGetterParam`。
- `shell` 工具更名为 `bash`；`copy` 覆盖已存在目标需要确认。
- `get_choice` / `get_confirm` 返回 `ChoiceOutcome`（选择值 + 来源）；自动确认不再写入命令与路径允许列表。
- 修复工具调用参数被 JSON 修复后与历史不同步的问题。

### 配置与提示词

- `auto_confirm` 支持环境变量 `XUN_AUTO_CONFIRM`。
- 系统提示词要求先查看并遵循工作目录下的 `AGENTS.md`。

### Web 服务与前端

- 文件浏览：PDF 与全屏预览、文本预览语法高亮、行内编辑；列表与信息分列，扫描更快；窄屏下面板互斥。
- 上传支持整个目录，结果通知列出全部文件并自动消失。
- 新增静态目录托管 `/srv/`，可直接访问工作目录里的站点（默认 1 小时过期）。
- 事件列表加载与传输优化；输入内容跨会话保持；界面文案中/英双语。

### 容器与多用户

- 容器启动时把宿主 xun home 复制进 `/.xun`（只取 `config.json` 与 `extensions`）。
- `xunc --env` / `xunx serve --env` 支持 `NAME=VALUE` 赋值与通配转发宿主变量。
- `xunx` 容器持久化：`serve` 重启后重新接管已有容器并保留会话，已删除用户的容器在对账时清理。
- 新增 `xunx upgrade`，由运行中的 `serve` 在下一次对账时重建容器。

### 文档

- 新增中英双语文档（MkDocs），Web 服务在 `/docs` 提供无需登录的访问。

## 1.0 — 2026-09-15

首个正式版本：执行循环与结构化输出、函数即工具与 8 组内置工具、显示层（控制台 / Web / 空）与 Web 前端、子智能体生成与会话压缩、会话命令与策略确认、`xun` / `xuns` / `xunc` 三种入口与 `xunx` 多用户复用服务。
