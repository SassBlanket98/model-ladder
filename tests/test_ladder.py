"""Tests for ladder.py. No real model CLI, no network, and no real models.json.

Every launch goes through `FakeLaunch`, quota goes through `FakeQuota`, and the settle
pause is a no-op. Everything lives in pytest's tmp_path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import ladder
from ladder import ComboScore, Launched, ModelCfg, Workspace

# Reset times are epoch ms, as quota reports them.
R1 = 1791954011000
R2 = 1792558811000

# Same shape as the real models.json: models is a list, subscriptions carry windows/lastResort.
MODELS = {
    "version": 1,
    "stopPct": 90,
    "tiers": ["small", "mid", "frontier"],
    "subscriptions": {
        "subA": {"account": "a:test", "windows": ["five", "week"]},
        "subB": {"account": "b:test"},
        "dead": {"account": "dead:test", "lastResort": True, "windows": ["five", "week"]},
    },
    "models": [
        {
            "id": "luna",
            "cli": "codex",
            "cliModel": "gpt-6-luna",
            "subscription": "subA",
            "tier": "small",
            "efforts": ["low", "medium", "high"],
            "defaultEffort": "medium",
        },
        {"id": "haiku", "cli": "claude", "cliModel": "haiku-cli", "subscription": "subA"},
        {
            "id": "deepseek",
            "cli": "opencode",
            "cliModel": "opencode-go/deepseek-v4.1-flash",
            "subscription": "subB",
            "tier": "small",
            "efforts": ["default"],
            "defaultEffort": "default",
            "neverJobs": ["review", "plan-small", "plan", "judgement", "lead"],
        },
        {"id": "sonnet", "cli": "claude", "subscription": "subA"},
        {"id": "sol", "cli": "codex", "subscription": "subB"},
        {"id": "composer", "cli": "cursor", "subscription": "subB", "neverJobs": ["review"]},
        {"id": "opus", "cli": "claude", "subscription": "dead"},
    ],
}
JOBS = {
    "review": {"passPct": 80, "maxFalseAlarms": 1},
    "judgement": {"passPct": 50, "maxFalseAlarms": None},
}


def default_respond(argv: list[str], cwd: Path) -> Launched:
    return Launched(0, "ANSWER", "", False)


class FakeLaunch:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, float]] = []
        self.respond = default_respond

    def __call__(self, argv: list[str], cwd: Path, timeout: float) -> Launched:
        self.calls.append((argv, cwd, timeout))
        return self.respond(argv, cwd)


class FakeQuota:
    """Returns queued quota reads in order; the last one repeats."""

    def __init__(self) -> None:
        self.reads: list[dict | None] = [None]

    def __call__(self) -> dict | None:
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]


def qstatus(sub: str = "subA", **windows: tuple[float, int]) -> dict:
    """qstatus(five=(90.0, R1)) -> quota status with one limit per keyword, in the real shape."""
    return {
        "generatedAt": 1791466175743,
        "subscriptions": [
            {
                "id": sub,
                "account": f"{sub}:test",
                "lastResort": False,
                "stale": False,
                "limits": [
                    {
                        "window": name,
                        "name": name,
                        "usedPct": used,
                        "pacePct": 0.0,
                        "projectedPct": used,
                        "runsOutAt": None,
                        "resetsAt": resets,
                        "stale": False,
                    }
                    for name, (used, resets) in windows.items()
                ],
            }
        ],
    }


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ladder, "call_quota", lambda: None)
    monkeypatch.setattr(ladder, "pause", lambda seconds: None)


@pytest.fixture
def fake_launch(monkeypatch) -> FakeLaunch:
    fake = FakeLaunch()
    monkeypatch.setattr(ladder, "launch", fake)
    return fake


@pytest.fixture
def quota(monkeypatch) -> FakeQuota:
    fake = FakeQuota()
    monkeypatch.setattr(ladder, "call_quota", fake)
    return fake


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps(MODELS), encoding="utf-8")
    root = tmp_path / "root"
    (root / "tasks").mkdir(parents=True)
    workspace = Workspace(root, models_path)
    (root / "tasks" / "jobs.json").write_text(json.dumps(JOBS), encoding="utf-8")
    return workspace


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_task(
    ws: Workspace,
    tid: str,
    *,
    job: str = "review",
    mode: str = "read",
    check: str | None = None,
    inputs: dict[str, str] | None = None,
    overlay: dict[str, str] | None = None,
    items: tuple[str, ...] = ("a",),
    critical: tuple[str, ...] = (),
    links: dict[str, str] | None = None,
    timeout: int = 30,
) -> str:
    d = ws.tasks_dir / tid
    spec: dict = {"job": job, "mode": mode, "timeoutSec": timeout}
    if check:
        spec["check"] = check
    if links:
        spec["links"] = links
    write_file(d / "task.json", json.dumps(spec))
    write_file(d / "prompt.md", "Review the code.\n")
    key = {"items": [{"id": i, "text": f"item {i}", "critical": i in critical} for i in items]}
    write_file(d / "key.json", json.dumps(key))
    for rel, text in (inputs or {}).items():
        write_file(d / "input" / rel, text)
    for rel, text in (overlay or {}).items():
        write_file(d / "overlay" / rel, text)
    return tid


def record(
    ws: Workspace,
    task: str,
    model: str,
    effort: str,
    *,
    n: int = 1,
    job: str = "review",
    status: str = "ok",
    rc: int | None = 0,
    check_rc: int | None = None,
    usage: float | None = None,
    tokens_total: int | None = None,
) -> str:
    run_id = f"{task}__{model}__{effort}__{n}"
    ws._append_row(
        ladder.RunRow(
            run_id=run_id,
            task=task,
            job=job,
            model=model,
            effort=effort,
            n=n,
            status=status,
            rc=rc,
            seconds=1.0,
            check_rc=check_rc,
            usage_delta=usage,
            date="2026-10-08",
            tokens_total=tokens_total,
        )
    )
    return run_id


def grade(
    ws: Workspace,
    run_id: str,
    *,
    met: tuple[str, ...],
    fa: int = 0,
    items: tuple[str, ...] = ("a",),
) -> None:
    ws.grades_dir.mkdir(exist_ok=True)
    data = {
        "items": [{"id": i, "met": i in met, "note": ""} for i in items],
        "falseAlarms": fa,
        "grader": "test",
    }
    (ws.grades_dir / f"{run_id}.json").write_text(json.dumps(data), encoding="utf-8")


def score(
    *,
    pct: float | None = 100.0,
    passed: bool = True,
    complete: bool = True,
    todo: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> ComboScore:
    return ComboScore(
        pct=pct,
        passed=passed,
        complete=complete,
        graded=1,
        total=1,
        seconds=None,
        usage=None,
        false_alarms=0,
        date="2026-10-08",
        todo=todo or (() if complete else ("run t1",)),
        notes=notes,
        has_rows=True,
    )


def incomplete() -> ComboScore:
    return score(pct=None, passed=False, complete=False)


def table_scorer(table: dict[str, ComboScore]):
    calls: list[str] = []

    def score_of(effort: str) -> ComboScore:
        calls.append(effort)
        return table.get(effort, incomplete())

    return score_of, calls


def model_cfg(cli: str, efforts: tuple[str, ...] = ("low", "medium", "high")) -> ModelCfg:
    return ModelCfg(
        id="m",
        cli=cli,
        cli_model="model-x",
        subscription="subA",
        efforts=efforts,
        last_resort=False,
        windows=None,
        never_jobs=frozenset(),
        only_jobs=None,
    )


# --- command construction -------------------------------------------------------------


def test_codex_read_and_write_with_effort(tmp_path):
    out = tmp_path / "out.md"
    cfg = model_cfg("codex")
    read = ladder.build_command(cfg, "high", "read", "P", tmp_path, out)
    assert read == [
        "codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--json",
        "-m", "model-x", "-c", "model_reasoning_effort=high", "-o", str(out), "P",
    ]  # fmt: skip
    write = ladder.build_command(cfg, "low", "write", "P", tmp_path, out)
    assert write[:4] == ["codex", "exec", "-s", "workspace-write"]
    assert "model_reasoning_effort=low" in write


def test_codex_default_effort_has_no_effort_flag(tmp_path):
    argv = ladder.build_command(
        model_cfg("codex", ("default",)), "default", "read", "P", tmp_path, tmp_path / "o"
    )
    assert "-c" not in argv
    assert argv[-1] == "P"


def test_claude_read_write_and_default(tmp_path):
    cfg = model_cfg("claude")
    read = ladder.build_command(cfg, "medium", "read", "P", tmp_path, tmp_path / "o")
    assert read == [
        "env", "-u", "CLAUDE_CONFIG_DIR", "claude", "-p", "--model", "model-x",
        "--output-format", "json", "--effort", "medium", "--allowedTools=Read,Grep,Glob", "--", "P",
    ]  # fmt: skip
    write = ladder.build_command(cfg, "medium", "write", "P", tmp_path, tmp_path / "o")
    assert "--permission-mode" in write
    assert write[write.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools=Read,Grep,Glob,Edit,Write" in write
    default = ladder.build_command(cfg, "default", "read", "P", tmp_path, tmp_path / "o")
    assert "--effort" not in default


def test_cursor_read_write_and_default(tmp_path):
    cfg = model_cfg("cursor", ("default",))
    read = ladder.build_command(cfg, "default", "read", "P", tmp_path, tmp_path / "o")
    assert read == [
        "cursor-agent", "-p", "--trust", "--mode", "ask", "--model", "model-x",
        "--workspace", str(tmp_path), "P",
    ]  # fmt: skip
    write = ladder.build_command(cfg, "default", "write", "P", tmp_path, tmp_path / "o")
    assert "--force" in write and "--mode" not in write


def test_opencode_command_and_default(tmp_path):
    argv = ladder.build_command(
        model_cfg("opencode", ("default",)), "default", "read", "P", tmp_path, tmp_path / "o"
    )
    assert argv == ["opencode", "run", "--standalone", "--format", "json", "-m", "model-x", "P"]


def test_build_prompt_adds_fixed_final_line():
    prompt = ladder.build_prompt("Do it.\n\n")
    assert prompt == f"Do it.\n\n{ladder.FINAL_LINE}\n"


def test_launch_uses_devnull_stdin_timeout_and_no_shell(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    monkeypatch.setattr(ladder.subprocess, "run", fake_run)
    result = ladder.launch(["claude", "-p", "x"], tmp_path, 42)
    assert result == Launched(0, "out", "err", False)
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeout"] == 42
    assert seen["cwd"] == tmp_path
    assert "shell" not in seen or seen["shell"] is False


def test_opencode_last_text_event():
    stream = "\n".join(
        [
            '{"type": "step_start", "part": {}}',
            '{"type": "text", "part": {"text": "first"}}',
            "not json at all",
            '{"type": "tool_use", "part": {"text": "ignored"}}',
            '{"type": "text", "part": {"text": "final answer"}}',
            '{"type": "step_finish"}',
        ]
    )
    assert ladder.last_text_event(stream) == "final answer"
    assert ladder.last_text_event('{"type": "step_start"}') == ""


def test_collect_answer_per_cli(tmp_path):
    out = tmp_path / "out.md"
    out.write_text("from file", encoding="utf-8")
    launched = Launched(0, '{"type": "text", "text": "from stream"}', "", False)
    assert ladder.collect_answer("codex", launched, out) == "from file"
    assert ladder.collect_answer("opencode", launched, out) == "from stream"
    assert ladder.collect_answer("claude", launched, out) == launched.stdout


def test_token_usage_parsers_handle_cli_json_shapes():
    opencode = json.dumps(
        {
            "type": "step_finish",
            "part": {"tokens": {"input": 12, "output": 5, "reasoning": 3, "cache": {"read": 7}}},
        }
    )
    assert ladder.token_usage("opencode", opencode) == {
        "tokens_in": 12,
        "tokens_out": 5,
        "tokens_reasoning": 3,
        "tokens_cached": 7,
        "tokens_total": 17,
    }
    codex = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 5,
                "reasoning_output_tokens": 3,
                "cached_input_tokens": 7,
            },
        }
    )
    assert ladder.token_usage("codex", codex)["tokens_total"] == 17
    claude = (
        '{"result":"ok","usage":{"input_tokens":12,"output_tokens":5,"cache_read_input_tokens":7}}'
    )
    assert ladder.token_usage("claude", claude)["tokens_cached"] == 7
    assert ladder.collect_answer("claude", Launched(0, claude, "", False), Path("missing")) == "ok"


def test_token_usage_opencode_sums_step_finish_events_and_ignores_missing():
    stream = "\n".join(
        [
            '{"type":"step_finish","part":{"tokens":{"input":2,"output":1,"reasoning":0,"cache":{"read":1}}}}',
            '{"type":"step_finish","part":{"tokens":{"input":3,"output":4,"reasoning":2,"cache":{"read":0}}}}',
            '{"type":"step_finish","part":{"tokens":{"input":null}}}',
        ]
    )
    assert ladder.token_usage("opencode", stream)["tokens_total"] == 10
    partial = '{"type":"step_finish","part":{"tokens":{"input":4,"output":2}}}'
    assert ladder.token_usage("opencode", partial)["tokens_total"] == 6


def test_unknown_token_counts_are_null():
    assert all(value is None for value in ladder.token_usage("cursor", "").values())


# --- refusals, budget gate and ledger -------------------------------------------------


def test_refuses_last_resort_model(ws, fake_launch):
    make_task(ws, "t1")
    with pytest.raises(ladder.Refused, match="lastResort"):
        ws.run_task("t1", "opus", "low")
    assert fake_launch.calls == []


def test_refuses_never_job(ws, fake_launch):
    make_task(ws, "t1", job="review")
    with pytest.raises(ladder.Refused, match="neverJobs"):
        ws.run_task("t1", "composer", None)
    assert fake_launch.calls == []


def test_refuses_when_window_at_limit(ws, fake_launch, quota):
    make_task(ws, "t1")
    quota.reads = [qstatus(five=(90.0, R1))]
    with pytest.raises(ladder.Refused, match="at 90%"):
        ws.run_task("t1", "luna", "medium")
    assert fake_launch.calls == []


@pytest.mark.parametrize("job", ["read", "triage"])
def test_hot_window_allows_read_and_triage_jobs(ws, fake_launch, quota, job):
    make_task(ws, "t1", job=job)
    quota.reads = [qstatus(five=(95.0, R1)), qstatus(five=(95.0, R1))]
    assert ws.run_task("t1", "luna", "medium").status == "ok"


def test_refuses_when_ledger_at_cap(ws, fake_launch, quota):
    make_task(ws, "t1")
    quota.reads = [qstatus(five=(10.0, R1))]
    ledger = ladder.Ledger.load(ws.ledger_path)
    ledger.add("subA", "five", R1, 5.0)
    ledger.save()
    with pytest.raises(ladder.Refused, match="test usage"):
        ws.run_task("t1", "luna", "medium")
    assert fake_launch.calls == []


def test_ledger_reset_lets_the_run_through(ws, fake_launch, quota):
    make_task(ws, "t1")
    quota.reads = [qstatus(five=(10.0, R2))]
    ledger = ladder.Ledger.load(ws.ledger_path)
    ledger.add("subA", "five", R1, 5.0)
    ledger.save()
    ws.run_task("t1", "luna", "medium")
    assert len(fake_launch.calls) == 1


def test_effort_required_for_multi_effort_model(ws, fake_launch):
    make_task(ws, "t1")
    with pytest.raises(ladder.LadderError, match="needs an effort"):
        ws.run_task("t1", "haiku", None)


def test_ledger_accumulates_and_ignores_negative_deltas(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ladder.Ledger.load(path)
    ledger.add("subA", "five", R1, 2.0)
    ledger.add("subA", "five", R1, 1.5)
    ledger.add("subA", "five", R1, -4.0)
    ledger.save()
    reloaded = ladder.Ledger.load(path)
    assert reloaded.total("subA", "five", R1) == pytest.approx(3.5)


def test_ledger_resets_when_resets_at_changes(tmp_path):
    ledger = ladder.Ledger.load(tmp_path / "ledger.json")
    ledger.add("subA", "five", R1, 4.0)
    ledger.add("subA", "five", R2, 1.0)
    assert ledger.total("subA", "five", R2) == pytest.approx(1.0)
    assert ledger.total("subA", "five", R1) == 0.0


def test_budget_reason_both_conditions(tmp_path):
    ledger = ladder.Ledger.load(tmp_path / "ledger.json")
    assert ladder.budget_reason("subA", None, ledger) is None
    ledger.add("subA", "week", R1, 5.0)
    assert "test usage" in ladder.budget_reason("subA", None, ledger)
    windows = {"five": ladder.Window(90.0, R1)}
    assert "at 90%" in ladder.budget_reason("subA", windows, ledger)
    under = {"five": ladder.Window(89.9, R1)}
    assert ladder.budget_reason("subA", under, ladder.Ledger.load(tmp_path / "empty.json")) is None


def test_usage_is_measured_and_ledgered(ws, fake_launch, quota):
    make_task(ws, "t1")
    quota.reads = [qstatus(five=(10.0, R1)), qstatus(five=(12.5, R1))]
    row = ws.run_task("t1", "luna", "medium")
    assert row.usage_delta == pytest.approx(2.5)
    saved = json.loads(ws.ledger_path.read_text(encoding="utf-8"))
    assert saved["subA"]["five"] == {"resetsAt": R1, "delta": 2.5}


# --- run flow: overlay, check, errors, write-mode diff ---------------------------------


@pytest.mark.parametrize("with_overlay", [True, False])
def test_overlay_is_applied_before_check(ws, fake_launch, with_overlay):
    overlay = {"hidden.txt": "secret"} if with_overlay else None
    make_task(ws, "t1", check="test -f hidden.txt", overlay=overlay)
    row = ws.run_task("t1", "luna", "medium")
    assert row.check_rc == (0 if with_overlay else 1)


def test_write_mode_diff_excludes_overlay(ws, fake_launch):
    make_task(
        ws,
        "t1",
        mode="write",
        inputs={"a.txt": "old\n"},
        overlay={"hidden.txt": "secret\n"},
    )

    def edit(argv, cwd):
        (cwd / "a.txt").write_text("new\n", encoding="utf-8")
        return Launched(0, "done", "", False)

    fake_launch.respond = edit
    row = ws.run_task("t1", "luna", "medium")
    diff = (ws.runs_dir / row.run_id / "changes.diff").read_text(encoding="utf-8")
    assert "-old" in diff and "+new" in diff
    assert "hidden" not in diff and "secret" not in diff


def test_run_writes_work_out_log_and_links(ws, fake_launch, tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    make_task(ws, "t1", inputs={"src.py": "x = 1\n"}, links={"ref": str(target)})
    row = ws.run_task("t1", "haiku", "medium")  # claude answers on stdout; codex uses a file
    run_dir = ws.runs_dir / row.run_id
    assert (run_dir / "work" / "src.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (run_dir / "work" / "ref").is_symlink()
    assert (run_dir / "out.md").read_text(encoding="utf-8") == "ANSWER"
    assert (run_dir / "log.txt").exists()
    argv, cwd, timeout = fake_launch.calls[0]
    assert cwd == run_dir / "work"
    assert timeout == 30


def test_run_records_tokens_in_meta_and_csv(ws, fake_launch):
    make_task(ws, "t1")
    payload = json.dumps(
        {
            "result": "ANSWER",
            "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3},
        }
    )
    fake_launch.respond = lambda argv, cwd: Launched(0, payload, "", False)
    row = ws.run_task("t1", "haiku", "medium")
    meta = json.loads((ws.runs_dir / row.run_id / "meta.json").read_text())
    assert (meta["tokens_in"], meta["tokens_out"], meta["tokens_cached"], meta["tokens_total"]) == (
        10,
        4,
        3,
        14,
    )
    assert ws.rows()[0].tokens_total == 14


def test_append_row_upgrades_legacy_csv_without_losing_rows(ws):
    ws.results_path.write_text(
        "run_id,task,job,model,effort,n,status,rc,seconds,check_rc,usage_delta,date\n"
        "old,t1,review,luna,low,1,ok,0,2.0,,0.25,2026-10-07\n",
        encoding="utf-8",
    )
    record(ws, "t1", "luna", "medium", tokens_total=21)
    rows = ws.rows()
    assert [(row.run_id, row.tokens_total) for row in rows] == [
        ("old", None),
        ("t1__luna__medium__1", 21),
    ]


def test_nonzero_exit_with_empty_answer_is_error(ws, fake_launch):
    make_task(ws, "t1")
    fake_launch.respond = lambda argv, cwd: Launched(1, "", "boom", False)
    assert ws.run_task("t1", "luna", "medium").status == "error"


def test_timeout_is_error(ws, fake_launch):
    make_task(ws, "t1")
    fake_launch.respond = lambda argv, cwd: Launched(None, "partial", "", True)
    assert ws.run_task("t1", "luna", "medium").status == "error"


def test_nonzero_exit_with_answer_is_ok_not_error(ws, fake_launch):
    make_task(ws, "t1")
    fake_launch.respond = lambda argv, cwd: Launched(1, "a real answer", "", False)
    assert ws.run_task("t1", "haiku", "medium").status == "ok"


def test_run_numbers_increase(ws, fake_launch):
    make_task(ws, "t1")
    first = ws.run_task("t1", "luna", "medium")
    second = ws.run_task("t1", "luna", "medium")
    assert first.run_id.endswith("__1")
    assert second.run_id.endswith("__2")


# --- grading --------------------------------------------------------------------------


def _graded_run(ws: Workspace, fake_launch, items=("a", "b")) -> str:
    make_task(ws, "t1", items=items)
    row = ws.run_task("t1", "luna", "medium")
    return row.run_id


OK_A = {"id": "a", "met": True, "note": ""}


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n{}\n```",
        '{"items": [], "falseAlarms": 0, "extra": 1}',
        '{"items": [{"id": "a", "met": true}], "falseAlarms": 0}',
        '{"items": [{"id": "a", "met": "yes", "note": ""}], "falseAlarms": 0}',
        '{"items": [{"id": "a", "met": true, "note": ""}], "falseAlarms": -1}',
        '{"items": [{"id": "a", "met": true, "note": ""}], "falseAlarms": true}',
        json.dumps({"items": [OK_A, OK_A], "falseAlarms": 0}),
        '{"items": [{"id": "a", "met": true, "note": ""}], "falseAlarms": 0}',
        json.dumps(
            {
                "items": [{**OK_A, "id": "x"}, {"id": "b", "met": False, "note": ""}],
                "falseAlarms": 0,
            }
        ),
    ],
)
def test_parse_grade_rejects_bad_json(raw):
    with pytest.raises(ladder.GradeFormatError):
        ladder.parse_grade(raw, ["a", "b"])


def test_parse_grade_accepts_and_orders_by_key():
    items = [{"id": "b", "met": False, "note": "no"}, {"id": "a", "met": True, "note": "yes"}]
    raw = json.dumps({"items": items, "falseAlarms": 2})
    parsed = ladder.parse_grade(raw, ["a", "b"])
    assert [i["id"] for i in parsed["items"]] == ["a", "b"]
    assert parsed["falseAlarms"] == 2


def test_grade_retries_once_then_succeeds(ws, fake_launch):
    run_id = _graded_run(ws, fake_launch, items=("a", "b"))
    good = json.dumps(
        {
            "items": [{"id": "a", "met": True, "note": ""}, {"id": "b", "met": False, "note": ""}],
            "falseAlarms": 0,
        }
    )
    replies = iter(["not json", good])
    fake_launch.respond = lambda argv, cwd: Launched(0, next(replies), "", False)
    path = ws.grade_run(run_id, "sonnet")
    assert len(fake_launch.calls) == 3  # the run, then two grader attempts
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["grader"] == "sonnet@medium"
    assert data["falseAlarms"] == 0


def test_grade_fails_after_two_bad_replies(ws, fake_launch):
    run_id = _graded_run(ws, fake_launch, items=("a",))
    calls_before = len(fake_launch.calls)
    fake_launch.respond = lambda argv, cwd: Launched(0, "still not json", "", False)
    with pytest.raises(ladder.GradeFailed):
        ws.grade_run(run_id, "sonnet")
    assert len(fake_launch.calls) - calls_before == 2
    assert not (ws.grades_dir / f"{run_id}.json").exists()
    assert (ws.runs_dir / run_id / "grade_raw.txt").read_text(encoding="utf-8") == "still not json"


def test_regrade_moves_previous_file_then_grades_again(ws, monkeypatch):
    run_id = "t1__luna__low__1"
    ws.grades_dir.mkdir()
    current = ws.grades_dir / f"{run_id}.json"
    current.write_text("old grade", encoding="utf-8")
    replacement = {"items": [], "falseAlarms": 0}

    def fake_grade(target, grader_id="sol"):
        current.write_text(json.dumps(replacement), encoding="utf-8")
        return current

    monkeypatch.setattr(ws, "grade_run", fake_grade)
    assert ws.regrade_run(run_id) == current
    assert (ws.grades_dir / f"{run_id}.prev.json").read_text(encoding="utf-8") == "old grade"
    assert json.loads(current.read_text(encoding="utf-8")) == replacement


def test_grade_refuses_error_run(ws, fake_launch):
    make_task(ws, "t1")
    fake_launch.respond = lambda argv, cwd: Launched(1, "", "", False)
    run_id = ws.run_task("t1", "luna", "medium").run_id
    with pytest.raises(ladder.LadderError, match="error run"):
        ws.grade_run(run_id, "sonnet")


# --- score aggregation ----------------------------------------------------------------


def _scored_task(ws: Workspace, *, critical=(), check_rc=None) -> str:
    make_task(ws, "t1", items=("a", "b", "c", "d", "e"), critical=critical)
    return record(ws, "t1", "luna", "medium", check_rc=check_rc)


def test_score_passes_when_pct_and_rules_hold(ws, fake_launch):
    run_id = _scored_task(ws)
    grade(ws, run_id, met=("a", "b", "c", "d"), fa=1, items=("a", "b", "c", "d", "e"))
    s = ws.combo_score("review", "luna", "medium")
    assert s.pct == pytest.approx(80.0)
    assert s.passed is True
    assert s.complete is True


def test_score_fails_below_pass_pct(ws, fake_launch):
    run_id = _scored_task(ws)
    grade(ws, run_id, met=("a", "b", "c"), items=("a", "b", "c", "d", "e"))
    assert ws.combo_score("review", "luna", "medium").passed is False


def test_score_fails_on_unmet_critical_item(ws, fake_launch):
    run_id = _scored_task(ws, critical=("a",))
    grade(ws, run_id, met=("b", "c", "d", "e"), items=("a", "b", "c", "d", "e"))
    s = ws.combo_score("review", "luna", "medium")
    assert s.pct == pytest.approx(80.0)
    assert s.passed is False


def test_score_fails_over_max_false_alarms(ws, fake_launch):
    run_id = _scored_task(ws)
    grade(ws, run_id, met=("a", "b", "c", "d", "e"), fa=2, items=("a", "b", "c", "d", "e"))
    assert ws.combo_score("review", "luna", "medium").passed is False


def test_score_ignores_false_alarm_cap_when_unset(ws, fake_launch):
    make_task(ws, "t1", job="judgement", items=("a",))
    run_id = record(ws, "t1", "luna", "medium", job="judgement")
    grade(ws, run_id, met=("a",), fa=9)
    assert ws.combo_score("judgement", "luna", "medium").passed is True


def test_score_fails_on_failed_check(ws, fake_launch):
    run_id = _scored_task(ws, check_rc=1)
    grade(ws, run_id, met=("a", "b", "c", "d", "e"), items=("a", "b", "c", "d", "e"))
    assert ws.combo_score("review", "luna", "medium").passed is False


def test_score_aggregates_items_across_tasks(ws, fake_launch):
    make_task(ws, "t1", items=("a", "b", "c", "d", "e"))
    make_task(ws, "t2", items=("a", "b", "c", "d", "e"))
    run1 = record(ws, "t1", "luna", "medium")
    run2 = record(ws, "t2", "luna", "medium")
    grade(ws, run1, met=("a", "b", "c", "d"), items=("a", "b", "c", "d", "e"))
    grade(ws, run2, met=("a", "b", "c", "d", "e"), items=("a", "b", "c", "d", "e"))
    s = ws.combo_score("review", "luna", "medium")
    assert s.pct == pytest.approx(90.0)
    assert s.graded == 2


def test_score_table_lists_combos(ws, fake_launch):
    run_id = _scored_task(ws)
    grade(ws, run_id, met=("a", "b", "c", "d", "e"), items=("a", "b", "c", "d", "e"))
    table = ws.score_table("review")
    assert [(j, m, e) for j, m, e, _ in table] == [("review", "luna", "medium")]


def test_score_uses_median_tokens_for_combo(ws, fake_launch):
    make_task(ws, "t1", items=("a",))
    record(ws, "t1", "luna", "medium", n=1, tokens_total=20)
    record(ws, "t1", "luna", "medium", n=2, tokens_total=31)
    grade(ws, "t1__luna__medium__2", met=("a",))
    assert ws.score_table("review")[0][3].tokens == 25.5


# --- error-vs-failed handling in `next` -----------------------------------------------


def test_one_error_offers_retry(ws, fake_launch):
    make_task(ws, "t1")
    record(ws, "t1", "luna", "low", status="error", rc=1)
    plan = ws.next_plan("review")
    assert (plan.model, plan.effort) == ("luna", "low")
    assert plan.combo.todo == ("run t1 (retry after error)",)


def test_two_errors_skip_the_task_with_a_note(ws, fake_launch):
    make_task(ws, "t1")
    record(ws, "t1", "luna", "low", n=1, status="error", rc=1)
    record(ws, "t1", "luna", "low", n=2, status="error", rc=1)
    plan = ws.next_plan("review")
    assert (plan.model, plan.effort) == ("luna", "medium")
    assert any("skipped after 2 error runs" in note for note in plan.notes)


# --- effort search rules --------------------------------------------------------------


def test_default_effort_pass_fail_and_need():
    fn, _ = table_scorer({"default": score(pct=90, passed=True)})
    assert ladder.judge_model(("default",), fn).kind == "best"
    fn, _ = table_scorer({"default": score(pct=40, passed=False)})
    assert ladder.judge_model(("default",), fn).kind == "failed"
    fn, _ = table_scorer({})
    v = ladder.judge_model(("default",), fn)
    assert (v.kind, v.effort) == ("need", "default")


def test_low_missing_needs_low_then_medium_missing_needs_medium():
    fn, _ = table_scorer({})
    assert ladder.judge_model(("low", "medium", "high"), fn).effort == "low"
    fn, _ = table_scorer({"low": score(pct=90, passed=True)})
    v = ladder.judge_model(("low", "medium", "high"), fn)
    assert (v.kind, v.effort) == ("need", "medium")


def test_low_passes_and_medium_close_skips_high():
    fn, calls = table_scorer(
        {"low": score(pct=80, passed=True), "medium": score(pct=90, passed=True)}
    )
    v = ladder.judge_model(("low", "medium", "high"), fn)
    assert (v.kind, v.effort) == ("best", "low")
    assert "high" not in calls


def test_medium_exactly_ten_above_low_still_skips_high():
    fn, calls = table_scorer(
        {"low": score(pct=80, passed=True), "medium": score(pct=90, passed=True)}
    )
    assert ladder.judge_model(("low", "medium", "high"), fn).kind == "best"
    assert "high" not in calls


def test_medium_more_than_ten_above_low_runs_high():
    fn, _ = table_scorer({"low": score(pct=80, passed=True), "medium": score(pct=95, passed=True)})
    v = ladder.judge_model(("low", "medium", "high"), fn)
    assert (v.kind, v.effort) == ("need", "high")


def test_high_needed_then_lowest_passing_within_margin_wins():
    # top = 99; low (80) is outside 10 points of it, medium (95) is inside.
    fn, _ = table_scorer(
        {
            "low": score(pct=80, passed=True),
            "medium": score(pct=95, passed=True),
            "high": score(pct=99, passed=True),
        }
    )
    v = ladder.judge_model(("low", "medium", "high"), fn)
    assert (v.kind, v.effort) == ("best", "medium")


def test_low_failing_medium_passing_high_runs_then_picks_medium_at_margin_edge():
    # top = 95; medium (85) sits exactly 10 points below it, so medium wins.
    fn, _ = table_scorer(
        {
            "low": score(pct=70, passed=False),
            "medium": score(pct=85, passed=True),
            "high": score(pct=95, passed=True),
        }
    )
    v = ladder.judge_model(("low", "medium", "high"), fn)
    assert (v.kind, v.effort) == ("best", "medium")


def test_all_efforts_failing_is_failed():
    fn, _ = table_scorer(
        {
            "low": score(pct=30, passed=False),
            "medium": score(pct=40, passed=False),
            "high": score(pct=50, passed=False),
        }
    )
    assert ladder.judge_model(("low", "medium", "high"), fn).kind == "failed"


def test_high_incomplete_when_needed_is_need():
    fn, _ = table_scorer(
        {"low": score(pct=70, passed=False), "medium": score(pct=75, passed=False)}
    )
    v = ladder.judge_model(("low", "medium", "high"), fn)
    assert (v.kind, v.effort) == ("need", "high")


# --- climb rule -----------------------------------------------------------------------

DEFAULT_CANDIDATES = [("luna", ("default",)), ("haiku", ("default",)), ("deepseek", ("default",)),
                      ("sonnet", ("default",)), ("sol", ("default",))]  # fmt: skip


def _plan(job: str, table: dict[str, ComboScore], candidates=DEFAULT_CANDIDATES):
    return ladder.plan_next(job, candidates, lambda m, e: table.get(m, incomplete()))


def test_climb_runs_one_cross_check_model_then_done():
    assert _plan("docs", {"luna": score(passed=True), "haiku": score(passed=True)}).model is None
    plan = _plan("docs", {"luna": score(passed=True)})
    assert (plan.model, plan.effort) == ("haiku", "default")


def test_cross_check_is_the_next_model_after_the_first_best():
    plan = _plan("docs", {"luna": score(passed=False), "haiku": score(passed=True)})
    assert (plan.model, plan.effort) == ("deepseek", "default")
    done = _plan(
        "docs",
        {
            "luna": score(passed=False),
            "haiku": score(passed=True),
            "deepseek": score(passed=False),
        },
    )
    assert done.model is None


def test_climb_stops_when_cross_check_fails():
    plan = _plan(
        "docs",
        {"luna": score(passed=True), "haiku": score(passed=False), "deepseek": score(passed=False)},
    )
    assert plan.model is None


def test_frontier_reached_only_when_cheaper_models_fail():
    table = {
        "luna": score(passed=False),
        "haiku": score(passed=False),
        "deepseek": score(passed=False),
        "sonnet": score(passed=False),
        "sol": score(passed=True),
    }
    assert _plan("docs", table).model is None
    plan = _plan("docs", {"luna": score(passed=False)})
    assert plan.model == "haiku"


@pytest.mark.parametrize("job", ["review", "plan-small", "plan", "judgement"])
def test_sonnet_always_runs_for_its_jobs_after_done(job):
    table = {"luna": score(passed=True), "haiku": score(passed=True)}
    plan = _plan(job, table)
    assert (plan.model, plan.effort) == ("sonnet", "default")


def test_sonnet_not_forced_for_other_jobs():
    table = {"luna": score(passed=True), "haiku": score(passed=True)}
    assert _plan("docs", table).model is None


def test_sonnet_already_complete_means_done():
    table = {"luna": score(passed=True), "haiku": score(passed=True), "sonnet": score(passed=False)}
    assert _plan("review", table).model is None


def test_rung_order_and_candidate_filter(ws):
    # composer and deepseek are neverJobs for review and must be left out; opus is lastResort.
    names = [
        mid
        for mid in ladder.RUNG_ORDER
        if mid in ws.models() and ws.models()[mid].refusal("review") is None
    ]
    assert names == ["luna", "haiku", "sonnet", "sol"]


# --- propose and budget ---------------------------------------------------------------


def test_propose_lists_passed_and_failed_efforts(ws, fake_launch):
    make_task(ws, "t1", items=("a",))
    luna_low = record(ws, "t1", "luna", "low")
    grade(ws, luna_low, met=("a",))
    luna_med = record(ws, "t1", "luna", "medium")
    grade(ws, luna_med, met=("a",))
    haiku_low = record(ws, "t1", "haiku", "low")
    grade(ws, haiku_low, met=())
    haiku_med = record(ws, "t1", "haiku", "medium")
    grade(ws, haiku_med, met=("a",))
    haiku_high = record(ws, "t1", "haiku", "high")
    grade(ws, haiku_high, met=("a",))
    entries = {(e["job"], e["model"]): e for e in ws.propose()}

    assert entries[("review", "luna")]["passed"] == {
        "effort": "low",
        "date": "2026-10-08",
        "note": "ladder 100%; tokens n/a",
    }
    assert entries[("review", "luna")]["failed"] == []
    haiku = entries[("review", "haiku")]
    assert haiku["passed"]["effort"] == "medium"
    assert haiku["failed"] == [
        {"effort": "low", "date": "2026-10-08", "note": "ladder 0%; tokens n/a"}
    ]


def test_propose_skips_models_still_being_searched(ws, fake_launch):
    make_task(ws, "t1")
    record(ws, "t1", "haiku", "low")  # medium and high are still missing
    assert ws.propose() == []


def test_budget_table_and_cli_budget(ws, tmp_path, monkeypatch, capsys):
    ledger = ladder.Ledger.load(ws.ledger_path)
    ledger.add("subA", "five", R1, 1.25)
    ledger.save()
    assert ws.budget_table() == [("subA", "five", 1.25)]
    monkeypatch.setattr(ladder, "ROOT", ws.root)
    monkeypatch.setenv("LADDER_MODELS_FILE", str(ws.models_path))
    assert ladder.main(["budget"]) == 0
    assert "subA" in capsys.readouterr().out


def test_cli_run_refusal_exit_code(ws, tmp_path, monkeypatch, capsys):
    make_task(ws, "t1")
    monkeypatch.setattr(ladder, "ROOT", ws.root)
    monkeypatch.setenv("LADDER_MODELS_FILE", str(ws.models_path))
    assert ladder.main(["run", "t1", "opus", "low"]) == 3
    assert "lastResort" in capsys.readouterr().err


# --- models.json shape, quota shape, grader effort ------------------------------------


def test_load_models_reads_the_real_shape(ws):
    luna = ws.model("luna")
    assert (luna.cli, luna.cli_model, luna.subscription) == ("codex", "gpt-6-luna", "subA")
    assert luna.efforts == ("low", "medium", "high")
    assert luna.windows == frozenset({"five", "week"})
    assert ws.model("deepseek").windows is None
    assert "review" in ws.model("deepseek").never_jobs
    assert ws.model("opus").last_resort is True
    assert ws.model("opus").windows == frozenset({"five", "week"})


def test_snapshot_reads_limits_of_the_named_subscription_only(monkeypatch):
    status = {
        "subscriptions": [
            {
                "id": "subA",
                "limits": [
                    {"window": "five", "usedPct": 10, "resetsAt": R1},
                    {"window": "month", "usedPct": 99, "resetsAt": R2},
                ],
            },
            {"id": "subB", "limits": [{"window": "five", "usedPct": 50, "resetsAt": R1}]},
        ]
    }
    monkeypatch.setattr(ladder, "call_quota", lambda: status)
    assert ladder.snapshot("subA", frozenset({"five"})) == {"five": ladder.Window(10.0, R1)}
    assert set(ladder.snapshot("subA", None)) == {"five", "month"}
    assert ladder.snapshot("missing", None) is None


def test_grader_effort_is_medium_when_listed_else_the_only_effort():
    assert ladder.grader_effort(model_cfg("codex")) == "medium"
    assert ladder.grader_effort(model_cfg("codex", ("default",))) == "default"


# --- dry run ----------------------------------------------------------------------------


def test_dry_run_run_prints_argv_and_cwd_and_touches_nothing(ws, fake_launch, monkeypatch, capsys):
    make_task(ws, "t1")
    monkeypatch.setattr(ladder, "ROOT", ws.root)
    monkeypatch.setenv("LADDER_MODELS_FILE", str(ws.models_path))
    assert ladder.main(["run", "t1", "luna", "medium", "--dry-run"]) == 0
    cwd_line, argv_line = capsys.readouterr().out.splitlines()
    assert cwd_line == f"cwd: {ws.runs_dir / 't1__luna__medium__1' / 'work'}"
    argv = json.loads(argv_line.removeprefix("argv: "))
    assert argv[:4] == ["codex", "exec", "-s", "read-only"]
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=medium"
    assert fake_launch.calls == []
    assert not ws.runs_dir.exists()
    assert not ws.ledger_path.exists()
    assert not ws.results_path.exists()


def test_dry_run_grade_uses_grader_medium_and_touches_nothing(ws, fake_launch, monkeypatch, capsys):
    run_id = _graded_run(ws, fake_launch)
    ledger_before = ws.ledger_path.read_bytes()
    calls_before = len(fake_launch.calls)
    monkeypatch.setattr(ladder, "ROOT", ws.root)
    monkeypatch.setenv("LADDER_MODELS_FILE", str(ws.models_path))
    assert ladder.main(["grade", run_id, "--grader", "sonnet", "--dry-run"]) == 0
    cwd_line, argv_line = capsys.readouterr().out.splitlines()
    assert cwd_line == f"cwd: {ladder.GRADE_TMP_PLACEHOLDER}"
    argv = json.loads(argv_line.removeprefix("argv: "))
    assert argv[argv.index("--effort") + 1] == "medium"
    assert len(fake_launch.calls) == calls_before
    assert ws.ledger_path.read_bytes() == ledger_before
    assert not (ws.grades_dir / f"{run_id}.json").exists()
