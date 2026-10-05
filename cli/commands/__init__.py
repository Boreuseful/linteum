"""``cli/commands/*`` —— 各子命令的**实现**（handler）。

由 ``main._forward_command`` 在调用时惰性 import（``importlib.import_module``），因此：

- 启动时不加载这些模块（不构造 AgentRunner / 不 import 重依赖）；
- ``patch("<module>.<attr>")`` 仍然能拦截真实 handler。

每个模块导出一个 ``run_<group>(args) -> int``：返回 ``int`` 时由 ``main()`` 作为进程退出码。
"""

from __future__ import annotations
