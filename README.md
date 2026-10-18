# Linteum

Linteum is an AI Agent tool built around an agent loop as its core architecture.

Language: [中文](README_zh_CN.md)

## Getting Started

The project currently targets Linux; macOS and Windows have not been tested yet.

```bash
bash install.sh            # editable install; add --web for `linteum serve --ws`
linteum --version
```

### Basic Usage

```bash
linteum setup                              # interactive provider/model/API key
linteum config list                        # inspect the effective config
linteum config edit                        # edit YAML with $EDITOR and validate

linteum chat -w /path/to/project           # REPL
linteum chat -w /path/to/project -m "Summarize README.md"   # one-shot
linteum sessions                           # list sessions

linteum serve --stdio                      # JSONL gateway over stdin/stdout
linteum serve --ws                         # WebSocket gateway on 127.0.0.1:8765
```

`workspace` is a per-session option (`-w` / `session.create`), never a global
config key.

### Configuration

The config file is `~/.linteum/config.yaml` (override with `$LINTEUM_CONFIG`).
`linteum setup` or `linteum config init` writes the commented template:

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

Useful commands:

```bash
linteum config path
linteum config get agent.llm.model
linteum config set agent.llm.model gpt-4o-mini
linteum config set agent.llm.api_key '${OPENAI_API_KEY}'
linteum config set agent.profiles '{"fast":{"provider":"deepseek","model":"deepseek-chat"}}'
linteum config validate --effective
linteum config list --show-origin --section agent
```

Supported providers: OpenAI (Responses / Chat Completions), OpenAI-compatible
endpoints (`base_url`), DeepSeek and Anthropic. Named profiles and a global
fallback chain are available via `agent.profiles` / `agent.fallbacks`.

## Uninstall

```bash
bash uninstall.sh            # remove the command; keep ~/.linteum
bash uninstall.sh --purge    # also remove ~/.linteum (config, state, artifacts)
```
