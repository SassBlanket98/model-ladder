"""The bundled example task must fail on its input and pass with its reference solution."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent / "tasks" / "build-slugify"


def run_check(tmp_path: Path, *, with_solution: bool) -> int:
    work = tmp_path / "work"
    shutil.copytree(TASK / "input", work)
    if with_solution:
        shutil.copytree(TASK / "solution", work, dirs_exist_ok=True)
    shutil.copytree(TASK / "overlay", work, dirs_exist_ok=True)
    check = json.loads((TASK / "task.json").read_text(encoding="utf-8"))["check"]
    return subprocess.run(shlex.split(check), cwd=work, capture_output=True).returncode


def test_check_fails_on_untouched_input(tmp_path: Path) -> None:
    assert run_check(tmp_path, with_solution=False) != 0


def test_check_passes_with_reference_solution(tmp_path: Path) -> None:
    assert run_check(tmp_path, with_solution=True) == 0


def test_visible_tests_pass_before_and_after(tmp_path: Path) -> None:
    for name, with_solution in (("before", False), ("after", True)):
        work = tmp_path / name
        shutil.copytree(TASK / "input", work)
        if with_solution:
            shutil.copytree(TASK / "solution", work, dirs_exist_ok=True)
        done = subprocess.run(
            ["python3", "-m", "unittest", "-q", "test_textutil"], cwd=work, capture_output=True
        )
        assert done.returncode == 0
