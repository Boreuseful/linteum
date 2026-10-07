# Linteum

Linteum 是一个以 agent loop 为核心架构的 AI Agent 工具。

语言：[English](README.md)

## 开始

项目目前适用于 linux 环境，暂未测试 mac 和 window 环境。

```bash
bash install.sh
linteum --version
# linteum 0.1.0-rc.1
```

### 基本使用命令

```bash
linteum setup
# 生成配置文件模板
linteum chat -p "实现numpy矩阵运算" --bash -y
```

配置文件模板：

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
  reasoning_effort: high    # deepseek 模型参数
  thinking: true            # deepseek 模型参数
```

目前适配 openai 模型和 deepseek 模型。

## 卸载

```bash
bash uninstall.sh
```
