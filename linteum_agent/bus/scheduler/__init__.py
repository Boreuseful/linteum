"""Scheduler package (design doc §16).

* :mod:`linteum_agent.bus.scheduler.pool` — the bounded :class:`WorkerPool` the
  bus (and, later, the tool runtime) spawn handlers on (partition concurrency).
* :mod:`linteum_agent.bus.scheduler.cancel` — :class:`CancelToken` /
  :class:`CancelRegistry` and the cooperative :class:`Cancelled` signal (§16.3).

Priority arbitration and the full timeout matrix (§16.2/§16.4) are layered on
top of the same pool without changing these interfaces.
"""

from .cancel import CancelRegistry, CancelToken, Cancelled
from .pool import WorkerPool

__all__ = ["WorkerPool", "CancelToken", "CancelRegistry", "Cancelled"]
