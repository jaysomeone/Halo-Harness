"""governor — a machine-global, cross-process adaptive rate limiter for shared API
gateways (Windows + POSIX). See README.md for the full guide."""
from .governor import (
    DEGRADED,
    GovernorError,
    Handle,
    acquire,
    governed,
    health,
    inspect,
    inspect_all,
    is_degraded,
    is_overload_status,
    key_for,
    params_for,
    pid_alive,
    priority_for,
    recent_calls,
    report,
    state_dir,
)
from . import routing

__all__ = [
    "DEGRADED", "GovernorError", "Handle", "acquire", "governed", "health",
    "inspect", "inspect_all", "is_degraded", "is_overload_status", "key_for",
    "params_for", "pid_alive", "priority_for", "recent_calls", "report",
    "state_dir", "routing",
]
