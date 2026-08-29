"""Fail-fast Python network guard for deterministic Brain reducer subprocesses.

Importing this module replaces common socket entry points with functions that raise.
It is a cooperative guard for Python reducers and tests, not a security sandbox. CI and
production builders should additionally disable networking at the container/runner layer.
"""
from __future__ import annotations

import socket


class NetworkDisabledError(RuntimeError):
    """Raised when an offline reducer attempts to access the network."""


def _blocked(*_args: object, **_kwargs: object) -> None:
    raise NetworkDisabledError(
        "network access is disabled for deterministic Brain reduction"
    )


class _BlockedSocket:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        _blocked()


socket.socket = _BlockedSocket  # type: ignore[assignment]
socket.create_connection = _blocked  # type: ignore[assignment]
socket.getaddrinfo = _blocked  # type: ignore[assignment]
socket.gethostbyname = _blocked  # type: ignore[assignment]
socket.gethostbyname_ex = _blocked  # type: ignore[assignment]
socket.gethostbyaddr = _blocked  # type: ignore[assignment]
