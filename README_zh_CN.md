# Linteum

Linteum 是一个以 agent loop 为核心架构的 AI Agent 工具。

语言：[English](README.md)

## 开始

项目目前适用于 Linux 环境，暂未测试 macOS 和 Windows 环境。

```bash
bash install.sh            # 可编辑安装；需要 `linteum serve --ws` 时加 --web
linteum --version
```

### 基本使用

```bash
linteum setup                              # 交互式配置 provider/model/API key
linteum config list                        # 查看有效配置
linteum config edit                        # 用 $EDITOR 编辑并校验

linteum chat -w /path/to/project           # REPL
linteum chat -w /path/to/project -m "总结 README.md"   # one-shot
linteum sessions                           # 列出会话

linteum serve --stdio                      # stdin/stdout JSONL 网关
linteum serve --ws                         # 127.0.0.1:8765 的 WebSocket 网关
```

`workspace` 是会话级参数（`-w` / `session.create`），不是全局配置项。

### 配置

配置文件默认是 `~/.linteum/config.yaml`（可用 `$LINTEUM_CONFIG` 覆盖）。
`linteum setup` 或 `linteum config init` 会写入带注释的模板：

```yaml
version: 1
agent:
  llm:
    provider: openai
    model: gpt-4o-mini
    openai_mode: responses
    api_key: ${OPENAI_API_KEY}
    base_url: null
    timeout: 60.0
    temperature: 0.7
    max_tokens: 4096
```

常用命令：

```bash
linteum config path
linteum config get agent.llm.model
linteum config set agent.llm.model gpt-4o-mini
linteum config set agent.llm.api_key '${OPENAI_API_KEY}'
linteum config set agent.profiles '{"fast":{"provider":"deepseek","model":"deepseek-chat"}}'
linteum config validate --effective
linteum config list --show-origin --section agent
```

支持的 provider：OpenAI（Responses / Chat Completions）、OpenAI 兼容端点
（`base_url`）、DeepSeek、Anthropic。可通过 `agent.profiles` 配置命名 profile，
通过 `agent.fallbacks` 配置全局降级链。

## 卸载

```bash
bash uninstall.sh            # 只移除命令，保留 ~/.linteum
bash uninstall.sh --purge    # 同时删除 ~/.linteum（配置、状态、artifact）
```
