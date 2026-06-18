from __future__ import annotations

import socket


def internet_available(host: str = "api.alpaca.markets", port: int = 443, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
