"""Public authentication result and explicit network environment."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Self

import requests

if TYPE_CHECKING:
    from .transport import ResourceSessionFactory


class NetworkEnvironment(StrEnum):
    INTRANET = "intranet"
    EXTRANET = "extranet"


@dataclass(slots=True)
class AuthenticationResult:
    """Hand over a real Session plus a factory for independent course Sessions."""

    session: requests.Session = field(repr=False)
    api_base_url: str
    session_factory: "ResourceSessionFactory" = field(repr=False)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Call after course threads stop; also supports use without a client."""
        self.session.close()
        self.session_factory.close()
