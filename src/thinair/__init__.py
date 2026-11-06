"""Thinair: run coding agents in sandboxes and find the minimum resources a project needs."""

from importlib.metadata import PackageNotFoundError, version

try:  # one source of truth: the version in pyproject.toml
    __version__ = version("thinair")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+unknown"
