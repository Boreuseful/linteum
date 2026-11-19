# linteum-web

[English](README.md) | 简体中文

`linteum_agent` 的独立浏览器客户端。它通过 WebSocket 使用 Gateway Protocol v1，
不导入、也不内嵌 Python 运行时。CLI、TUI 与 Web 只在传输层和渲染层不同。

## 环境要求

- Node.js **22 LTS（>= 22.12）** 或 20.19+；npm >= 10。
- 一个运行中的 gateway：
  ```bash
  uv run linteum serve --ws        # ws://127.0.0.1:8765/
  ```
- gateway 环境需要 `websockets` 包（`linteum[web]`）。

## 构建

```bash
npm install
npm run build
```

产物输出到 `dist/`；`npm run preview` 可在本地预览。构建会先执行
`tsc --noEmit`，再由 Vite 打包。

## 运行

```bash
# 终端 1 — gateway
uv run linteum serve --ws

# 终端 2 — Web 客户端
npm run dev                        # http://127.0.0.1:5173/
```

默认连接 `ws://127.0.0.1:8765/`。可用 `VITE_GATEWAY_URL` 覆盖，或在连接界面
修改地址；最近一次地址保存在 `localStorage`。

| 环境变量 | 含义 |
|---|---|
| `VITE_GATEWAY_URL` | Gateway WebSocket 地址（默认 `ws://127.0.0.1:8765/`）。 |

## 会话与 workspace

新建会话对话框要求填写 workspace，默认使用本浏览器上一次的 workspace，否则为
`/workspace`。每个会话保留自己的 workspace 和 profile；会话列表展示 gateway 的
会话目录。

当 gateway 声明 `config.profiles` 时，对话框可以选择命名 profile，默认使用
`agent.default_profile`。重连时从最后一个持久化的 `seq` 重新订阅，并回放该会话
的历史。

## 设置

设置页是 gateway 级的，不需要先打开会话。侧栏按归属分组：

- **客户端** — 通用（语言）、外观（主题）、连接。
- **Agent** — LLM、插件，以及 `agent.*` 的 schema 分组（上下文、工具、后台任务、
  Skills、子代理、轮次引导、预算、会话默认值、模式、插件设置）。
- **网关 / 存储 / 日志** — 其余 schema 分组。

行为说明：

- **LLM** 通过 `config.profile.*` / `config.fallback.*` 编辑命名 profile、默认
  profile 和 fallback 链。需要 `config` feature（命名 profile 还需要
  `config.profiles`）以及 `gateway.config.manage=true`；只有本地传输可以写入。
  密钥始终遮罩，未改动的遮罩密钥不会被回写。
- **插件** 列出运行时插件目录并切换启用/禁用，需要
  `gateway.plugins.manage=true`。有 `plugins.runtime` 时，应用修改后的 allow
  list（`plugin.set_allowed`）会就地重新装配；否则重启 gateway 后生效。运行时
  应用命令仅限本地传输；`plugin.enable` / `plugin.disable` 只持久化 allow list。
- **Schema 分组** 需要 `config.settings`。支持全局搜索、来源筛选、恢复默认值
  （分组与单字段）以及 `persist=false` 的差异预览。标量和标量列表可编辑；map、
  object、list-of-object 只读；`${VAR}` 引用通过 `raw_value` 保留；被环境变量
  接管的字段只读。
- **配置推送**（`config.push`）：同一进程内写入配置后，gateway 发送 ephemeral
  的 `config.changed`，仅包含新 revision 和变更的 `{key, scope}`（不含值）。
  空闲客户端会自动刷新已加载的分组；有未保存草稿的客户端会显示 stale banner
  并禁用保存，直到显式重新加载。`logging.*` 立即生效，无需重启 gateway。

## 工作区面板（demo 桥）

浏览器无法读取 gateway 主机的文件系统，Gateway Protocol v1 也没有
`workspace.*` 命令。因此本 demo 在 `vite.config.ts` 中提供了一个**仅用于
demo** 的只读桥：

- `GET /demo-api/workspace/tree?root=...&path=...`
- `GET /demo-api/workspace/files?root=...`
- `GET /demo-api/workspace/file?root=...&path=...`
- `GET /demo-api/workspace/raw?root=...&path=...`（原始字节，支持 HTTP
  byte-range/`206`，供浏览器原生 PDF 查看器使用；上限 32 MB，而文本上限为
  256 KB）

它只在 `apply: "serve"` 时启用，每个文本文件最多读取 256 KB，解析 `realpath`
并拒绝 session workspace 之外的路径。未来有真正的 `workspace.tree` /
`workspace.read` 协议时，替换 `src/workspace/index.ts` 中的 provider 即可，
面板本身无需改动。

## 键盘快捷键

UI 以鼠标操作为主；这些快捷键只是加速方式，不是唯一路径。

| 按键 | 动作 |
|---|---|
| `Enter` | 发送消息 |
| `Shift+Enter` | 换行 |
| `/` | 打开斜杠命令菜单 |
| `Ctrl/Cmd+K` | 命令面板 |
| `Ctrl/Cmd+N` | 新建会话 |
| `F1` | 帮助 |
| `F2` | 会话选择器 |
| `y` / `n` | 批准 / 拒绝待审批的工具调用 |
| `Esc` | 关闭当前对话框；在设置页返回聊天 |

在斜杠菜单中，`Up`/`Down` 移动选择，`Enter`/`Tab` 执行命令，`Esc` 关闭菜单。
在 SVG 预览中，`+`/`-`/`0` 缩放，拖动平移。

## 斜杠命令

在输入框输入 `/` 打开命令菜单。

| 命令 | 动作 |
|---|---|
| `/new` | 新建会话 |
| `/files` | 打开工作区面板 |
| `/sidebar` | 显示/隐藏右侧栏 |
| `/preview` | 工作区预览 |
| `/info` | 信息面板 |
| `/subagents` | 子代理面板 |
| `/plugins` | 插件面板 |
| `/refresh` | 刷新会话列表 |
| `/merge` | 堆叠面板 |
| `/split` | 分屏为标签页 |
| `/collapse` | 折叠当前面板 |
| `/expand` | 展开当前面板 |
| `/stop` | 停止正在运行的 agent |
| `/leave` | 离开当前会话 |
| `/help` | 键盘快捷键帮助 |
| `/quit` | 断开 gateway 连接 |

