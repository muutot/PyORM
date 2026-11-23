"""Errors raised by the document mapper; driver errors retain their original types."""


class ORMError(Exception):
    pass


class ValidationError(ORMError, ValueError):
    pass


class ConcurrentModificationError(ORMError):
    """The stored version changed or the document was deleted since loading."""


class ConnectionClosedError(ORMError, RuntimeError):
    pass


class QueueFullError(ORMError):
    pass


class PersistenceError(ORMError):
    """A Manager could not persist a cached document; the cache remains intact."""


class ManagerNotInitializedError(ORMError, RuntimeError):
    pass
