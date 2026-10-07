# Linteum

Linteum is an AI Agent tool built around an agent loop as its core architecture.

Language: [中文](README_zh_CN.md)

## Getting Started

The project currently targets the linux environment; mac and window have not been tested yet.

```bash
bash install.sh
linteum --version
# linteum 0.1.0-rc.1
```

### Basic Usage

```bash
linteum setup
# generate the config file template
linteum chat -p "implement numpy matrix operations" --bash -y
```

Config file template:

```yaml
version: 1
llm:
  provider: openai
  model: gpt-6-sol
  openai_mode: responses
  api_key: sk-
  base_url: https://example.com
  timeout: 60.0
  temperature: 0.1
  max_tokens: 384000
  top_p: null
  reasoning_effort: high    # deepseek model parameter
  thinking: true            # deepseek model parameter
```

Currently supports the openai and deepseek models.

## Uninstall

```bash
bash uninstall.sh
```
