from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping


_SECRET_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class SecretValue:
    """A secret whose normal string/repr representations cannot expose its value."""

    _value: str
    source: str

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "[REDACTED SECRET]"

    def __repr__(self) -> str:
        return "SecretValue([REDACTED], source=%r)" % self.source

    def reveal(self) -> str:
        """Explicit boundary used only when constructing an authenticated client."""
        return self._value


KeychainReader = Callable[[str], str | None]


class SecretStore:
    """Environment-first secret provider with an injectable Keychain boundary."""

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        keychain_reader: KeychainReader | None = None,
        *,
        allow_keychain: bool = True,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._keychain_reader = keychain_reader or self._read_macos_keychain
        self._allow_keychain = allow_keychain

    def get(self, name: str) -> SecretValue | None:
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError("invalid secret name")
        value = self._environment.get(name)
        if self._usable(value):
            return SecretValue(str(value), "environment")
        if not self._allow_keychain or self._testing():
            return None
        value = self._keychain_reader(name)
        return SecretValue(value, "keychain") if self._usable(value) else None

    def get_plaintext(self, name: str) -> str | None:
        secret = self.get(name)
        return secret.reveal() if secret else None

    def _testing(self) -> bool:
        return self._environment.get("TRADING_AGENT_TESTING") == "1"

    @staticmethod
    def _usable(value: str | None) -> bool:
        return bool(value and not value.startswith("replace_with_"))

    def _read_macos_keychain(self, name: str) -> str | None:
        service_name = name if name.startswith("TradingAgent.") else f"TradingAgent.{name}"
        try:
            result = subprocess.run(
                [
                    "/usr/bin/security", "find-generic-password", "-a",
                    self._environment.get("USER", ""), "-s", service_name, "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def default_secret_store() -> SecretStore:
    # Construct on demand so tests can establish a synthetic environment before use.
    return SecretStore()
