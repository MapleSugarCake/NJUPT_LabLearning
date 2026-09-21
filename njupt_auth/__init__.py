"""NJUPT authentication, handing off verified requests.Session instances."""

from .auth import authenticate, check_access, import_token
from .browser import authenticate_in_browser
from .errors import AuthError, AuthExpiredError
from .models import AuthenticationResult, NetworkEnvironment

__all__ = [
    "AuthError",
    "AuthExpiredError",
    "AuthenticationResult",
    "NetworkEnvironment",
    "authenticate",
    "authenticate_in_browser",
    "check_access",
    "import_token",
]
