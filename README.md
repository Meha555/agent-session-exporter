# Agent 会话导出工具

这个项目提供一个 Python 脚本，用于把本地 agent 会话记录导出为便于阅读和归档的格式。目前支持：

- `opencode`：从 `opencode.db` 中读取会话和消息。
- `codex`：从 Codex 的 `.jsonl` 会话文件夹中读取会话。

导出结果主要是 HTML，也支持只输出聊天摘要 JSON。HTML 会渲染 Markdown 和 Mermaid 图表，并展示 Human 和 AI 的对话、AI 思考过程、工具调用、文件 diff、会话分页、右侧大纲、浅色/深色模式、返回顶部和滚到底部按钮。

## 数据安全

脚本不会直接修改原始数据源。

- 导出 `opencode` 时，会先复制指定的 `opencode.db`，以及同名的 `-wal`、`-shm` 文件，然后只读取临时副本。
- 导出 `codex` 时，会先复制指定的 sessions 目录，然后只读取临时副本。
- SQLite 连接使用只读模式，并设置 `PRAGMA query_only=ON`。

临时副本会在导出结束后自动清理。

## 环境

项目使用 `uv` 管理依赖和运行脚本。

```powershell
uv sync
```

查看命令行参数：

```powershell
uv run python agent_session_exporter.py --help
```

## 导出 opencode 会话

导出全部 opencode 会话：

```powershell
uv run python agent_session_exporter.py `
  --source opencode `
  --db /path/to/opencode.db `
  --output /path/to/save
```

导出指定会话：

```powershell
uv run python agent_session_exporter.py `
  --source opencode `
  --db /path/to/opencode.db `
  --session-id ses_10df512a0ffeiAwU78zKjLNNGq `
  --output /path/to/save
```

导出为单个 HTML 文件，不分页：

```powershell
uv run python agent_session_exporter.py `
  --source opencode `
  --db /path/to/opencode.db `
  --session-id ses_10df512a0ffeiAwU78zKjLNNGq `
  --single-file `
  --output /path/to/save
```

## 导出 Codex 会话

Codex 会话通常按日期保存在类似下面的目录中：

```text
/path/to/.codex/sessions/2026/07/01/*.jsonl
```

导出全部 Codex 会话：

```powershell
uv run python agent_session_exporter.py `
  --source codex `
  --sessions-dir /path/to/.codex/sessions `
  --output /path/to/save
```

导出指定 Codex 会话：

```powershell
uv run python agent_session_exporter.py `
  --source codex `
  --sessions-dir /path/to/.codex/sessions `
  --session-id 019f1d3e-ec6b-7961-b0df-53704e1bb94b `
  --output /path/to/save
```

如果不知道完整会话 id，也可以先导出摘要或导出全部，再从 `index.html` 中查看对应会话。

## 只查看会话摘要

使用 `--summary-only` 时不会导出 HTML，而是输出 JSON 摘要。每轮对话格式如下：

```json
[
  {
    "human": "Human 请求摘要",
    "ai": "AI 响应摘要"
  }
]
```

查看指定 opencode 会话摘要，并写入 `summary.json`：

```powershell
uv run python agent_session_exporter.py `
  --source opencode `
  --db /path/to/opencode.db `
  --session-id ses_10df512a0ffeiAwU78zKjLNNGq `
  --summary-only `
  --output /path/to/save
```

查看指定 Codex 会话摘要：

```powershell
uv run python agent_session_exporter.py `
  --source codex `
  --sessions-dir /path/to/.codex/sessions `
  --session-id 019f1d3e-ec6b-7961-b0df-53704e1bb94b `
  --summary-only `
  --output /path/to/_export\codex_summary.json
```

调整摘要长度：

```powershell
uv run python agent_session_exporter.py `
  --source opencode `
  --db /path/to/opencode.db `
  --summary-only `
  --summary-chars 120
```

## HTML 阅读体验

HTML 导出会生成一个 `index.html`，并为每个会话生成对应页面。

页面支持：

- Markdown 渲染。
- Mermaid 图表渲染。
- Human 和 AI 头像颜色区分。
- AI 思考过程折叠展示。
- 工具调用折叠展示。
- 文件 diff 折叠展示。
- 会话压缩事件以分隔符展示。
- 右侧 Outline，可折叠，便于在小屏幕查看。
- 浅色和深色模式切换。
- 右下角返回顶部、滚到底部按钮。
- 大会话分页，分页入口只显示在页面底部。

## 性能相关参数

大会话导出时建议使用分页，避免单个 HTML 太大导致浏览器加载缓慢。

```powershell
uv run python agent_session_exporter.py `
  --source opencode `
  --db /path/to/opencode.db `
  --output /path/to/save `
  --page-message-count 80 `
  --jobs 4
```

常用参数：

- `--page-message-count 120`：每个 HTML 页最多包含多少条可见消息，默认 `120`。设置为 `0` 可关闭分页。
- `--single-file`：强制每个会话只导出一个 HTML 文件。
- `--jobs 4`：并发渲染会话数量。
- `--diff-highlight-lines 2000`：diff 行数不超过该值时使用逐行高亮，超过后使用更轻量的纯文本块。
- `--include-synthetic`：默认隐藏 synthetic、model-switched、agent-switched 等事件；启用后会在 HTML 中展示。

## 输出结构

分页导出时，输出目录大致如下：

```text
output\
  index.html
  ses_xxx-title-p001.html
  ses_xxx-title-p002.html
  ses_xxx-title-diffs.html
```

未分页导出时，输出目录大致如下：

```text
output\
  index.html
  ses_xxx-title.html
```

如果 `--summary-only` 的 `--output` 是目录，则会写入：

```text
output\
  summary.json
```

如果 `--summary-only` 的 `--output` 是 `.json` 文件，则直接写入该文件。
