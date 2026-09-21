"""Authentication failures containing only safe, user-facing descriptions."""


class AuthError(Exception):
    """Authentication could not be completed."""


class AuthProtocolError(AuthError):
    """An authentication response or destination is unexpected."""


class InvalidCredentialsError(AuthError):
    """The identity provider explicitly rejected the credentials."""


class InteractionRequiredError(AuthError):
    """A user must complete a challenge in the browser."""


class AuthUnavailableError(AuthError):
    """The authentication service cannot be reached."""


class AuthOutcomeUncertainError(AuthError):
    """A request may have been processed; it must not be replayed."""


class AuthExpiredError(AuthError):
    """The resource no longer accepts the authentication context."""


class BrowserUnavailableError(AuthError):
    """The optional browser dependency or installed Edge is unavailable."""
