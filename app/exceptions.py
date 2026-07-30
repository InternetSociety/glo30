class ApplicationError(Exception):
    """Base class for errors that can be translated into an API response."""


class DuplicateUserError(ApplicationError):
    pass


class UserNotFoundError(ApplicationError):
    pass


class InvalidUserOperationError(ApplicationError):
    pass


class RadiusLimitError(ApplicationError):
    pass


class DemCoverageError(ApplicationError):
    pass


class TileDownloadError(ApplicationError):
    pass


class ViewshedProcessingError(ApplicationError):
    pass
