from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts.verify_alpaca_crypto_sdk import required_alpaca_version


def test_hash_verified_lock_has_exact_current_alpaca_sdk_pin(tmp_path) -> None:
    lock = tmp_path / "requirements-hashes.lock"
    lock.write_text(
        f"alpaca-py==0.43.4 --hash=sha256:{'a' * 64}\n"
        f"requests==2.0 --hash=sha256:{'b' * 64}\n",
        encoding="utf-8",
    )
    assert required_alpaca_version(lock) == "0.43.4"


@pytest.mark.parametrize(
    "contents",
    [
        f"requests==2.0 --hash=sha256:{'a' * 64}\n",
        f"alpaca-py==0.43.4 --hash=sha256:{'a' * 64}\n"
        f"alpaca-py==0.43.5 --hash=sha256:{'b' * 64}\n",
        "alpaca-py>=0.43.4\n",
        "alpaca-py==0.43.4\n",
    ],
)
def test_missing_duplicate_or_unpinned_sdk_lock_fails_closed(tmp_path, contents) -> None:
    lock = tmp_path / "requirements-hashes.lock"
    lock.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one hash-verified alpaca-py pin"):
        required_alpaca_version(lock)


def test_installed_sdk_matches_offline_crypto_contract() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_alpaca_crypto_sdk.py"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["verified"] is True
    assert evidence["network_io"] is False
    assert evidence["installed_version"] == evidence["locked_version"] == "0.43.4"
    assert evidence["crypto_feed"] == "us"
    assert evidence["asset_exchange"] == "CRYPTO"
    assert len(evidence["official_contract_fingerprint"]) == 64
    assert evidence["supported_order_types"] == ["limit", "market", "stop_limit"]
    assert evidence["supported_time_in_force"] == ["gtc", "ioc"]
    assert "pending_review" in evidence["order_statuses"]
    assert evidence["verified_signature_count"] == 20
