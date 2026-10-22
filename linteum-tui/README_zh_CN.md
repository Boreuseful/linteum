# linteum-tui

[English](README.md) | 简体中文

`linteum_agent` 的独立 Rust 终端客户端。它通过 stdio 或 WebSocket 与 gateway
通信，不链接、也不内嵌 Python 运行时。

## 环境要求

- Rust stable（固定工具链见 `rust-toolchain.toml`）
- 默认的 spawn 模式需要 `PATH` 上有 Python `linteum` CLI

## 构建

```bash
cargo build --release
```

二进制输出到 `target/release/linteum-tui`。

## 运行

通过 stdio 拉起一个私有 gateway（默认模式）：

```bash
cargo run --release -- --spawn

# 或者使用构建好的二进制
./target/release/linteum-tui --spawn
```

连接一个已经在运行的 gateway：

```bash
# 终端 1：启动 gateway
linteum serve --ws

# 终端 2：连接 TUI
./target/release/linteum-tui --connect ws://127.0.0.1:8765/
```

完整选项见 `linteum-tui --help`。

## 会话与 workspace

会话选择器（picker）按 **workspace** 过滤会话目录，并且对保存的 workspace
字符串做精确匹配。`--spawn` 模式下过滤器默认取启动 TUI 时所在的目录（或显式
传入的 `--workspace`）；`--connect` 模式默认不加过滤器，除非传了
`--workspace`。因此，为 `/path/to/project` 创建的会话，在另一个目录启动
`linteum-tui --spawn` 时不会显示。

如果过滤后的列表为空，TUI 会跳过 picker，直接打开新建会话对话框——**即使没有
传 `--new` 也会这样**。这不是 `--new` 的效果，而是空结果导致的：启动时的
`session.list` 请求带着当前目录过滤器，返回 0 条，view model 就把覆盖层从
picker 切换成新建会话表单。

要查看全部会话，在 picker 中按 `a`（all workspaces）或 `r`（刷新）。如果已经
停在新建会话表单上，先按 `Esc`，再按 `F2` 重新打开 picker，然后按 `a`。也可以
用 `--session <ID>` 直接连接；`linteum sessions` 会打印带 id 和 workspace 的
会话目录。

`--continue` 已被解析但尚未实现：它不会连接到最近更新的会话，而是停在 picker。

## 命令行参数

| 参数 | 说明 |
|---|---|
| `--spawn` | 拉起 `linteum serve --stdio` 子进程（默认模式）。与 `--connect` 互斥。 |
| `--connect <URL>` | 连接已运行的 gateway，例如 `ws://127.0.0.1:8765/`。与 `--spawn` 互斥。 |
| `--linteum-bin <PATH>` | spawn 模式使用的 `linteum` 可执行文件。 |
| `--config <PATH>` | 传给 `linteum serve --config` 的 Python 配置（仅 spawn 模式）。 |
| `--no-lease` | 给拉起的 gateway 传 `--no-lease`（仅 spawn 模式）。 |
| `--workspace <DIR>` | 新会话的 workspace。spawn 模式下它同时作为 picker 的过滤器。 |
| `--session <ID>` | 连接已有会话。与 `--continue`、`--new` 互斥。 |
| `--continue` | 保留参数，尚未实现（会停在 picker）。与 `--session`、`--new` 互斥。 |
| `--new` | 启动时打开新建会话对话框。与 `--session`、`--continue` 互斥。 |
| `--profile <NAME>` | 传给 `session.create` 的 profile。 |
| `--tui-config <PATH>` | 显式指定 TUI 偏好文件（TOML）。 |
| `--theme <dark\|light\|mono>` | 启动主题。 |
| `--show-reasoning` | 启动时展开 reasoning 区块。 |
| `--no-color` | 强制单色主题。 |
| `--debug` | 输出详细元数据日志。 |
| `--debug-content` | 日志中包含消息内容。 |
| `-h, --help` | 打印选项列表。 |
| `-V, --version` | 打印版本。 |

TUI 的配置优先级为：命令行参数 > 环境变量 > TUI TOML > Python
`config.yaml` > 默认值。

| 环境变量 | 含义 |
|---|---|
| `LINTEUM_HOME` | 状态目录（默认 `~/.linteum`）：`config.yaml`、`state.db`、`artifacts/`、`logs/`。 |
| `LINTEUM_CONFIG` | Python 配置文件路径（默认 `$LINTEUM_HOME/config.yaml`）。 |
| `LINTEUM_BIN` | spawn 模式使用的 `linteum` CLI 路径。 |
| `LINTEUM_TUI_CONFIG` | TUI 偏好 TOML（默认 `$XDG_CONFIG_HOME/linteum-tui/config.toml`）。 |
| `LINTEUM_TUI_MODE` | 启动模式：`spawn`，或 `connect`/`attach`。 |
| `LINTEUM_TUI_URL` | attach 模式的 gateway URL。 |
| `LINTEUM_TUI_THEME` | `dark`、`light`、`mono` 或 `auto`。 |
| `LINTEUM_TUI_SHOW_REASONING` | `1`/`true`/`yes`/`on` 时展开 reasoning 区块。 |
| `NO_COLOR` | 强制单色主题。 |

## 按键

### 全局

| 按键 | 动作 |
|---|---|
| `Enter` | 发送消息（composer 获得焦点时）。 |
| `Ctrl+J` | 在 composer 中插入换行。 |
| `Ctrl+C` | 中断正在运行的 agent；空闲时清空 composer。 |
| `Ctrl+N` | 打开新建会话对话框。 |
| `Ctrl+P` | 打开命令面板。 |
| `Ctrl+R` | 展开/收起 reasoning 区块。 |
| `Ctrl+G` | 恢复处于 paused 状态的 agent。 |
| `Ctrl+Q` | 退出 TUI。 |
| `F1` / `Alt+1` | 帮助。 |
| `F2` / `Alt+2` | 会话选择器。 |
| `F3` / `Alt+3` | 活动日志。 |
| `F4` / `Alt+4` | 审批。 |
| `F5` / `Alt+5` | 立即重连。 |
| `F6` / `Alt+6` | 子代理。 |
| `Esc` | 关闭当前覆盖层。 |
| `Tab` / `Shift+Tab` | 在 composer 和侧边栏之间切换焦点。 |
| `Left` / `Right` / `Up` / `Down` | 移动光标（composer）或选择项（列表）。 |
| `Home` / `End` | composer 当前行的行首/行尾。 |
| `Ctrl+Home` / `Ctrl+End` | 滚动到 transcript 顶部/底部。 |
| `PageUp` / `PageDown` | transcript 翻一页。 |
| `Backspace` / `Delete` | 向前/向后删除。 |

### 会话选择器（`F2`）

| 按键 | 动作 |
|---|---|
| `Up` / `Down` | 移动选择。 |
| `Enter` | 连接到选中的会话。 |
| `n` | 打开新建会话对话框。 |
| `r` | 刷新列表。 |
| `a` | 切换是否显示全部 workspace（清除或重新应用 workspace 过滤）。 |
| `m` | 加载下一页。 |
| `Esc` | 关闭 picker。 |

### 新建会话对话框

| 按键 | 动作 |
|---|---|
| `Enter` | 创建会话。 |
| `Tab` / `Shift+Tab` / `Up` / `Down` | 在 workspace 和标题字段之间切换。 |
| `Left` / `Right` / `Home` / `End` | 移动光标。 |
| `Backspace` / `Delete` | 编辑字段。 |
| 可打印字符 | 输入到当前字段。 |
| `Esc` | 关闭对话框。 |

### 审批对话框

| 按键 | 动作 |
|---|---|
| `y` | 批准待处理的工具调用。 |
| `n` | 拒绝待处理的工具调用。 |
| `Esc` | 关闭对话框。 |

### 命令面板（`Ctrl+P`）

| 按键 | 动作 |
|---|---|
| `Up` / `Down` | 移动选择。 |
| `Enter` | 执行选中的动作。 |
| 可打印字符 | 过滤动作列表。 |
| `Left` / `Right` / `Home` / `End` | 移动过滤光标。 |
| `Backspace` / `Delete` | 编辑过滤文本。 |
| `Esc` | 关闭命令面板。 |

面板动作：退出、切换 reasoning、会话、活动、审批、侧边栏子代理、子代理、帮助、
新建会话、重连、恢复、复制最后一条助手消息、打开最后一个 artifact、展开/收起最后
一个工具调用。

### 子代理（`F6`）

| 按键 | 动作 |
|---|---|
| `Up` / `Down` | 移动选择。 |
| `Enter` | 打开选中子代理的 context。 |
| `Esc` | 关闭。 |

子代理 context 视图：`Up`/`Down` 滚动一行，`PageUp`/`PageDown` 滚动十行，`r`
重新加载 context，`Esc` 返回子代理列表。

### 工作区侧边栏

侧边栏仅在可见且获得焦点时处理这些按键。

| 按键 | 动作 |
|---|---|
| `Up` / `Down` | 上一个/下一个条目。 |
| `Home` / `End` | 第一个/最后一个条目。 |
| `Left` / `Right` | 收起/展开选中的目录。 |
| `Enter` / `Space` | 展开/收起选中的目录。 |
| `/` | 焦点移到 composer 并开始输入斜杠命令。 |
| `Tab` / `Shift+Tab` / `Esc` | 焦点移回 composer。 |

### 斜杠命令

在 composer 中输入 `/` 打开命令菜单。`Up`/`Down` 选择命令，`Enter` 执行，
`Tab` 补全，`Esc` 关闭菜单。

| 命令 | 动作 |
|---|---|
| `/files` | 显示工作区目录树。 |
| `/sidebar` | 显示或隐藏侧边栏。 |
| `/merge` | 把侧边栏合并为一个区块。 |
| `/split` | 把侧边栏拆分为文件、预览和子代理。 |
| `/collapse` | 收起选中的目录。 |
| `/expand` | 展开选中的目录。 |
| `/preview` | 显示预览面板。 |
| `/info` | 显示信息面板。 |
| `/subagents` | 在侧边栏显示子代理状态。 |
| `/refresh` | 重新加载工作区目录树。 |
| `/exit` | 离开当前会话。 |
| `/quit` | 退出 `linteum-tui`。 |

## 测试

```bash
cargo test
```

需要真实 gateway 的集成测试默认被忽略：

```bash
cargo test --test integration_stdio -- --ignored
cargo test --test integration_ws -- --ignored
```

`scripts/check.sh` 会运行格式化、clippy、单元测试和上述被忽略的集成测试
（后者需要 Python `linteum` CLI）。
