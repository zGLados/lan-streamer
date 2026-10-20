"""lan-streamer: self-hosted modular LAN streaming platform."""

from importlib import resources

__version__ = "0.0.1"


def _build() -> str:
    """The installer's build version (e.g. 0.0.1-test.23), written by build.ps1."""
    try:
        return resources.files(__name__).joinpath("BUILD").read_text().strip()
    except OSError:
        return f"{__version__}-dev"


BUILD = _build()
