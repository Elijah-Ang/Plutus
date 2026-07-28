from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY_ROOT / "scripts"

APP_IMPORTING_SCRIPTS = (
    "activate_phase3.py",
    "activate_phase4.py",
    "adaptive_sizing_evidence.py",
    "check_phase0_integrity.py",
    "check_release_eligibility.py",
    "migrate_runtime_db.py",
    "phase0_migration_proof.py",
    "phase1_evidence.py",
    "repair_exit_blocker.py",
    "replay_adaptive_conviction.py",
    "telegram_get_updates.py",
    "telegram_test.py",
    "test_fake_paper_proposal.py",
    "test_paper_order_proposal.py",
    "test_paper_sell_proposal.py",
)

HELP_ENTRYPOINTS = (
    "activate_phase3.py",
    "activate_phase4.py",
    "adaptive_sizing_evidence.py",
    "check_phase0_integrity.py",
    "replay_adaptive_conviction.py",
    "telegram_get_updates.py",
)


def test_app_importing_script_inventory_is_complete() -> None:
    discovered = set()
    for script in SCRIPTS.glob("*.py"):
        module = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        if any(
            isinstance(node, ast.ImportFrom)
            and (node.module == "app" or str(node.module).startswith("app."))
            or isinstance(node, ast.Import)
            and any(
                alias.name == "app" or alias.name.startswith("app.")
                for alias in node.names
            )
            for node in module.body
        ):
            discovered.add(script.name)

    assert discovered == set(APP_IMPORTING_SCRIPTS)


@pytest.mark.parametrize("script_name", APP_IMPORTING_SCRIPTS)
def test_script_imports_app_from_an_isolated_working_directory(
    script_name: str,
    tmp_path: Path,
) -> None:
    script = SCRIPTS / script_name
    probe = (
        "import runpy; "
        f"runpy.run_path({str(script)!r}, run_name='operational_entrypoint_import_probe')"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script_name", HELP_ENTRYPOINTS)
def test_safe_cli_help_works_outside_repository(
    script_name: str, tmp_path: Path
) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script_name), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
