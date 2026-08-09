class RepositoryError(RuntimeError):
    pass


class MissingError(RepositoryError):
    pass


class EventConflictError(RepositoryError):
    pass


class ConcurrentUpdateError(RepositoryError):
    pass
