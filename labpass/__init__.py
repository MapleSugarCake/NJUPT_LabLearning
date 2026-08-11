"""LabPass package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("labpass")
except PackageNotFoundError:  # Running directly from an unpacked source tree.
    __version__ = "1.0.0"

__all__ = ["__version__"]
