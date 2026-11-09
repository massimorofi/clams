"""Clams — Claudio Multi-Agent System.

Standalone orchestrator wrapping decoupled Claudio Planner/Executor instances:
GitHub work queue, handoffs, leases, outbox, and a zero-LLM watchdog.
"""
__version__ = "0.1.0"

from .config import (  # noqa: F401
    Settings,
    GitHubSettings,
    RedisSettings,
    PlannerSettings,
    ExecutorSettings,
    StrixperSettings,
    Limits,
    PathsSettings,
    WatchdogSettings,
    get_settings,
)
from .db import (  # noqa: F401
    Database,
    Store,
    WorkItem,
    WorkItemRepo,
    TaskClaimRepo,
    AlertRepo,
    OutboxRepo,
    open_store,
)
