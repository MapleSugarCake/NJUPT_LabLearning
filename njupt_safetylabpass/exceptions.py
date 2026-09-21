"""Business failures with safe, user-facing descriptions."""


class LabPassError(Exception):
    """Base class for recoverable business errors."""


class AuthenticationExpiredError(LabPassError):
    """Global resource authentication failure."""


class RunCancelledError(LabPassError):
    """The run was cancelled before this request could start."""


class NetworkError(LabPassError):
    """A read request failed."""


class ApiError(LabPassError):
    """The API rejected a request."""


class LockConflictError(ApiError):
    """The server could not acquire its write lock."""


class ResponseFormatError(ApiError):
    """The response does not match the expected business schema."""


class SubmissionUncertainError(ApiError):
    """A write may have reached the server and must not be retried."""
