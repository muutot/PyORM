"""Native containers that notify their owning field after in-place mutations."""

from functools import wraps

_SCALAR_TYPES = frozenset((str, int, float, bool, bytes, type(None)))


def mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            self._changed.before()
            try:
                return method(self, *args, **kwargs)
            finally:
                # extend/update/sort can mutate part of a container before raising.
                self._changed()

    return wrapped


def track(value, changed, lock):
    if type(value) in _SCALAR_TYPES:
        return value
    if (
        isinstance(value, (TrackedDict, TrackedList, TrackedSet))
        and value._changed == changed
        and value._lock is lock
    ):
        # Augmented field assignment (items += ..., tags |= ...) writes the
        # container back through its descriptor. Keep existing references valid.
        return value
    if hasattr(value, "_bind_changes"):
        value._bind_changes(changed, lock)
        return value
    if isinstance(value, dict):
        return TrackedDict(value, changed, lock)
    if isinstance(value, list):
        return TrackedList(value, changed, lock)
    if isinstance(value, set):
        return TrackedSet(value, changed, lock)
    if isinstance(value, tuple):
        return tuple(track(item, changed, lock) for item in value)
    return value


class TrackedDict(dict):
    def __init__(self, value, changed, lock):
        self._changed, self._lock = changed, lock
        super().__init__((key, track(item, changed, lock)) for key, item in value.items())

    @mutation
    def __setitem__(self, key, value):
        return super().__setitem__(key, track(value, self._changed, self._lock))

    @mutation
    def __delitem__(self, key):
        return super().__delitem__(key)

    @mutation
    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            dict.__setitem__(self, key, track(value, self._changed, self._lock))

    @mutation
    def setdefault(self, key, default=None):
        if key not in self:
            dict.__setitem__(self, key, track(default, self._changed, self._lock))
        return self[key]

    @mutation
    def pop(self, *args):
        return super().pop(*args)

    @mutation
    def popitem(self):
        return super().popitem()

    @mutation
    def clear(self):
        return super().clear()

    def __ior__(self, other):
        self.update(other)
        return self


class TrackedList(list):
    def __init__(self, value, changed, lock):
        self._changed, self._lock = changed, lock
        super().__init__(track(item, changed, lock) for item in value)

    @mutation
    def __setitem__(self, key, value):
        if isinstance(key, slice):
            value = [track(item, self._changed, self._lock) for item in value]
        else:
            value = track(value, self._changed, self._lock)
        return super().__setitem__(key, value)

    @mutation
    def __delitem__(self, key):
        return super().__delitem__(key)

    @mutation
    def append(self, value):
        return super().append(track(value, self._changed, self._lock))

    @mutation
    def extend(self, value):
        return super().extend(track(item, self._changed, self._lock) for item in value)

    @mutation
    def insert(self, index, value):
        return super().insert(index, track(value, self._changed, self._lock))

    @mutation
    def pop(self, *args):
        return super().pop(*args)

    @mutation
    def remove(self, value):
        return super().remove(value)

    @mutation
    def clear(self):
        return super().clear()

    @mutation
    def reverse(self):
        return super().reverse()

    @mutation
    def sort(self, *args, **kwargs):
        return super().sort(*args, **kwargs)

    def __iadd__(self, value):
        self.extend(value)
        return self

    @mutation
    def __imul__(self, count):
        super().__imul__(count)
        return self


class TrackedSet(set):
    def __init__(self, value, changed, lock):
        self._changed, self._lock = changed, lock
        super().__init__(value)

    @mutation
    def add(self, value):
        return super().add(value)

    @mutation
    def discard(self, value):
        return super().discard(value)

    @mutation
    def remove(self, value):
        return super().remove(value)

    @mutation
    def pop(self):
        return super().pop()

    @mutation
    def clear(self):
        return super().clear()

    @mutation
    def update(self, *args):
        return super().update(*args)

    @mutation
    def difference_update(self, *args):
        return super().difference_update(*args)

    @mutation
    def intersection_update(self, *args):
        return super().intersection_update(*args)

    @mutation
    def symmetric_difference_update(self, other):
        return super().symmetric_difference_update(other)

    def __ior__(self, other):
        self.update(other)
        return self

    def __iand__(self, other):
        self.intersection_update(other)
        return self

    def __isub__(self, other):
        self.difference_update(other)
        return self

    def __ixor__(self, other):
        self.symmetric_difference_update(other)
        return self
