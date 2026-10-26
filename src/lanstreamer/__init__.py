"""lan-streamer: self-hosted modular LAN streaming platform."""

from importlib import resources

__version__ = "0.1.1"  # the version in development; a release tag builds exactly this


def _build() -> str:
    """The installer's build version (e.g. 0.1.0-test.23 or 0.1.0), written by build.ps1."""
    try:
        return resources.files(__name__).joinpath("BUILD").read_text().strip()
    except OSError:
        return f"{__version__}-dev"


BUILD = _build()
