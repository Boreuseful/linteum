# Linteum

Linteum 是一个用 Python 编写的事件驱动 AI Agent runtime。核心是建立于不可变
事件之上的纯 reducer：由 WAL 支撑的 bus 负责排序并持久化每个事实，LLM
provider 和工具作为叶子接入，从不触碰 reducer 的状态。项目提供两个命令：

- `linteum` — CLI：交互式与一次性对话、会话目录、配置管理，以及基于 stdio 或
  WebSocket 的 gateway。
- `linteum-tui` — 独立的 Rust 终端客户端，可连接该 gateway（或自行拉起一个
  私有 gateway）。

每个会话都有自己的 workspace、事件日志和带检查点的状态，因此对话可以在之后
恢复。

语言：[English](README.md)

## 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Linux | 当前测试目标；macOS 和 Windows 尚未测试。 |
| Python | 3.12+ | `uv` 可自动准备合适的解释器。 |
| uv | 任意较新版本 | 缺失时由 `install.sh` 自动安装。 |
| curl | 任意较新版本 | 仅在 `install.sh` 需要安装 uv 或 Rust 时使用。 |
| Rust | stable 工具链 | 仅用于 `linteum-tui`；`cargo` 缺失时由 `install.sh` 通过 rustup 安装。工具链配置见 `linteum-tui/rust-toolchain.toml`。 |
| websockets | `>=13` | 可选；`linteum serve --ws` 需要。用 `./install.sh --web` 安装。 |

## 安装

```bash
git clone https://github.com/Boreuseful/linteum.git linteum
cd linteum

./install.sh                 # 可编辑安装 + 编译并安装 linteum-tui
./install.sh --web           # 同时安装 websockets，供 `linteum serve --ws` 使用
./install.sh --no-tui        # 跳过 Rust 和 TUI
./install.sh --no-editable   # 安装当前源码的快照，而不是跟踪源码树
```

`install.sh` 会在缺失时安装 `uv` 和 Rust，将 Python 项目安装为 uv tool，使用
`cargo build --release` 编译 TUI，并把两个命令放入 uv tool 的 bin 目录。该目录
也会被加入你的 shell 配置。

验证安装：

```bash
linteum --version
linteum-tui --version
```

如果当前 shell 中仍找不到命令，请打开新 shell 或重新加载 shell 配置文件。

## 快速开始

```bash
linteum setup                              # 交互式选择 provider/model/API key
linteum chat -w /path/to/project           # 交互式 REPL
linteum chat -w /path/to/project -m "总结 README.md"   # 一次性对话
linteum sessions                           # 列出已保存的会话
```

终端客户端：

```bash
linteum-tui --spawn                        # 启动私有的 stdio gateway（默认）
linteum-tui --connect ws://127.0.0.1:8765/ # 连接已运行的 gateway
```

默认的 `--spawn` 模式会拉起 `linteum serve --stdio` 子进程，只需要 Python CLI。
`--connect` 模式需要一个 WebSocket gateway：

```bash
./install.sh --web                         # 一次性安装 websockets
linteum serve --ws                         # ws://127.0.0.1:8765
```

运行 `linteum-tui --help` 查看客户端完整选项。

## 配置

半永久配置默认位于 `~/.linteum/config.yaml`。`LINTEUM_HOME` 会移动整个状态目录
（`config.yaml`、`state.db`、`artifacts/`、`logs/`）；`LINTEUM_CONFIG` 只覆盖
配置文件路径。运行 `linteum config path` 可打印实际生效的位置。

`linteum setup` 或 `linteum config init` 会写入带注释的模板。文件保存 provider
凭据、模型 profile、context/工具策略、gateway 限制、存储路径、日志和 UI 偏好。
会话的 `workspace` **不**在这里配置——它按每次对话提供（`-w` /
`session.create`）。

常用字段示例（未列出的字段使用默认值；完整模板请运行
`linteum config init`）：

```yaml
version: 1

agent:
  # `default` profile，未选择 profile 时使用。
  llm:
    provider: openai              # openai | openai_compatible | anthropic | deepseek | moonshot | qwen | zhipu | siliconflow
    model: gpt-4o-mini
    openai_mode: responses        # responses | chat_completions；null = provider 默认
    api_key: ${OPENAI_API_KEY}    # ${ENV_VAR} 引用，或字面量 key（文件会被设为 0600）
    base_url: null                # openai_compatible 和自定义端点必须填写
    timeout: 60.0
    temperature: 0.7
    max_tokens: 4096
    top_p: null
    reasoning_effort: null        # 例如 low | medium | high
    thinking: null                # provider 专用开关

  default_profile: default

  # 命名 profile，用 `linteum chat -p <name>` 选择。
  profiles:
    fast:
      provider: deepseek
      model: deepseek-chat
      api_key: ${DEEPSEEK_API_KEY}
    claude:
      provider: anthropic
      model: claude-sonnet-5-5
      api_key: ${ANTHROPIC_API_KEY}

  # 选中的 profile 失败后按顺序尝试。
  fallbacks:
    - provider: openai
      model: gpt-4o-mini
      api_key: ${OPENAI_API_KEY}

  context:
    policy: passthrough           # passthrough | compacting
    max_tokens: 120000            # policy=compacting 时使用
    compact_to_tokens: 80000
    keep_last_messages: 8
    max_summary_tokens: 4000

  tools:
    approval: ask                 # ask | allow | deny
    bash_timeout: 30.0

  skills:
    enabled: true
    project_dir: .linteum/skills  # 相对于会话 workspace

  subagents:
    enabled: false                # 需显式开启；开启后根会话才看到 tool_name
    tool_name: spawn_subagent     # 保留名，不会注册进子会话的工具表
    max_depth: 1                  # 子会话不能再派生 subagent
    max_concurrent_per_session: 4
    max_queued_per_session: 32
    default_timeout_ms: 1800000   # 30 分钟
    max_timeout_ms: 3600000       # 60 分钟；超出的请求会被截断
    cancel_grace_ms: 5000
    max_prompt_bytes: 16384       # spawn 提示词的 UTF-8 总字节数
    max_summary_bytes: 7168       # 返回父会话的摘要上限
    progress_interval_ms: 1000
    default_tools: []             # 为空 = 父会话全部工具（tool_name 除外）
    allowed_tools: []             # 为空 = 以父会话当前工具表为准
    default_model_profile: default
    allowed_model_profiles: []    # 为空 = 只允许 default_model_profile
    approval_policy: allow        # allow | deny | inherit（继承 agent.tools.approval）
    inherit_skills: true
    max_tokens: 0                 # 每个子会话的预算；0 = 不限制
    max_usd: 0.0
    max_rounds: 0
    max_wall_ms: 0

  budget:                         # 0 = 不限制
    tokens: 0
    usd: 0.0
    wall_ms: 0
    rounds: 0

  session:
    input_policy: queue           # queue | interrupt | reject
    idle_ttl_ms: 1800000

gateway:
  transport: auto                 # auto | inproc | stdio | websocket
  websocket:
    host: 127.0.0.1               # 非回环地址需要 --allow-remote
    port: 8765

storage:
  state_db: ~/.linteum/state.db
  artifacts: ~/.linteum/artifacts
  checkpoints:
    keep: 3
    every_n_events: 500

logging:
  dir: ~/.linteum/logs
  file: run.log
  level: info                     # debug | info | warning | error
  format: text                    # text | json
  include_content: false          # 写入完整 payload（默认只记录元数据）
  console: false

ui:
  language: zh-CN
  theme: auto                     # auto | light | dark
  show_reasoning: true
```

Subagent 默认关闭。设置 `agent.subagents.enabled: true` 后，根会话的工具表会
加入保留的 `spawn_subagent`；每次派生在独立的子会话中运行，只把一份有界摘要
交回父会话。模型只能收窄 `tools`、`model_profile` 和 `timeout_ms`：
`allowed_tools` 为空时取父会话当前工具表，`allowed_model_profiles` 为空时只允许
`default_model_profile`。子会话不能再派生，默认不出现在 `session.list` 中
（需 `include_subagents=true`），并与根会话共用租约。

其他配置段与限制（`agent.skills` 的各项上限、`gateway.limits`、
`gateway.delta`、`gateway.history`、`gateway.backpressure`、
`storage.checkpoints`、`logging` 轮转）会在生成的模板中说明。

内置 provider：`openai`、`openai_compatible`、`anthropic`、`deepseek`、
`moonshot`、`qwen`、`zhipu`、`siliconflow`。每个 provider 都有默认端点和
API key 环境变量（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、
`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY`、`DASHSCOPE_API_KEY`、
`ZHIPU_API_KEY`、`SILICONFLOW_API_KEY`）；`openai_compatible` 和未知
provider 必须提供 `base_url`。共享同一 provider 的 profile 必须在
`base_url`、`api_key` 和 `openai_mode` 上保持一致。

常用配置命令：

```bash
linteum config path
linteum config list --show-origin --section agent
linteum config get agent.llm.model
linteum config set agent.llm.model gpt-4o-mini
linteum config set agent.llm.api_key '${OPENAI_API_KEY}'
linteum config set agent.profiles '{"fast":{"provider":"deepseek","model":"deepseek-chat"}}'
linteum config edit               # 用 $VISUAL/$EDITOR 编辑，保存时校验
linteum config validate --effective
```

## 卸载

```bash
./uninstall.sh            # 移除 linteum 和 linteum-tui，保留 ~/.linteum
./uninstall.sh --purge    # 同时删除 ~/.linteum（配置、状态、artifact）
```

通过 rustup 安装的 Rust，以及 `install.sh` 添加的 shell PATH 配置会保留。
