"""Initialize one process-wide Manager before loading any business models."""

import threading
from typing import Any

from .exceptions import ConnectionClosedError, ManagerNotInitializedError
from .manager import Manager

_condition = threading.Condition(threading.RLock())
_manager: Manager | None = None
_configuration: tuple[str, str, bool, dict[str, Any]] | None = None
_shutting_down = False


def init(
    uri: str = "mongodb://localhost:27017",
    database: str = "pyorm",
    *,
    check_connection: bool = True,
    **options,
) -> Manager:
    """Application startup: initialize and verify the default Manager once."""
    global _manager, _configuration
    configuration = (uri, database, check_connection, dict(options))
    with _condition:
        if _shutting_down:
            raise ConnectionClosedError("Default Manager is shutting down")
        if _manager is not None:
            _manager._check_open()
            if configuration != _configuration:
                raise RuntimeError("Default Manager already initialized with different settings")
            return _manager
        manager = Manager(uri, database, **options)
        try:
            if check_connection:
                manager._orm.ping()
        except BaseException:
            manager.close()
            raise
        _manager = manager
        _configuration = configuration
        return manager


def get_manager() -> Manager:
    """Return the pre-initialized Manager for application monitoring/lifecycle code."""
    with _condition:
        if _shutting_down:
            raise ConnectionClosedError("Default Manager is shutting down")
        manager = _manager
        if manager is None:
            raise ManagerNotInitializedError("Call pyorm.init(...) during application startup")
    manager._check_open()
    return manager


def shutdown(timeout: float | None = 10.0) -> None:
    """Application shutdown: persist remaining cached changes, then release the Manager."""
    global _manager, _configuration, _shutting_down
    with _condition:
        if _manager is not None and (
            _manager._orm._executor.in_worker or threading.current_thread() is _manager._thread
        ):
            raise RuntimeError("A Manager worker cannot shut down its own application Manager")
        _condition.wait_for(lambda: not _shutting_down)
        if _manager is None:
            return
        manager = _manager
        _shutting_down = True
    # Do not hold the runtime lock while draining: worker callbacks may inspect the runtime.
    try:
        manager.close(timeout)
    except BaseException:
        with _condition:
            _shutting_down = False
            _condition.notify_all()
        raise
    with _condition:
        _manager = None
        _configuration = None
        _shutting_down = False
        _condition.notify_all()
