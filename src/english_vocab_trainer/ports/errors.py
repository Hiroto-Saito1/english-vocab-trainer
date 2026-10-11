class MissingError(RuntimeError):
    pass


class EventConflictError(RuntimeError):
    pass


class ConcurrentUpdateError(RuntimeError):
    pass
