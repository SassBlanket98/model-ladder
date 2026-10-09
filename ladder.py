#!/usr/bin/env python3
"""model-ladder: find the cheapest model and effort that can do each job type.

Stdlib only. Run with `uv run ladder.py <command>` or `python3 ladder.py <command>`.
The design is in SPEC.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_MODELS = Path.home() / ".agents" / "picker" / "models.json"
DEFAULT_QUOTA = Path.home() / ".agents" / "bin" / "quota"

RUNG_ORDER = [
    "luna",
    "haiku",
    "deepseek",
    "sonnet",
    "composer",
    "grok-medium",
    "sol",
    "opus",
    "grok-high",
]
ALWAYS_SONNET_JOBS = {"review", "plan-small", "plan", "judgement"}
EFFORT_SEARCH = ["low", "medium", "high"]
DEFAULT_EFFORT = "default"
FINAL_LINE = "Write your full answer as your final message."
EFFORT_MARGIN = 10.0
TEST_CAP = 5.0
USED_LIMIT = 90.0
SETTLE_SECONDS = 20
GRADER_PROMPT = (
    "Grade the answer in out.md against key.json as GRADER.md in this directory says. "
    "Judge substance, not wording: an item is met when the answer states the same fact, "
    "even in different words or with a more precise formulation. If the key text itself "
    "says what counts as meeting an item, follow that. "
    "Reply with the JSON object only."
)
GRADE_TMP_PLACEHOLDER = "<ladder-grade-tmp>"  # dry-run stand-in for the grader's mkdtemp dir
GRADER_INSTRUCTIONS = """\
You are grading one answer against a key. Work only from the files in this directory.

Files: prompt.md (the question the model was asked), out.md (its answer), key.json (the key),
changes.diff (only if present: the file changes the model made).

key.json has "items" (things a good answer must state or implement; each has an id and a
critical flag) and optionally "nonIssues" (claims that are NOT real defects or facts).

Rules:
- Give one verdict per key item, using exactly the item ids from key.json. "met" is true only
  if out.md clearly states or implements the item. Otherwise false.
- "falseAlarms" is the number of distinct wrong claims of a defect or fact in out.md. A claim
  that matches an entry in nonIssues counts as a false alarm.
- Do not modify any file. Do not reply with prose or a code fence.

Reply with exactly this JSON object and nothing else:
{"items": [{"id": "<key id>", "met": true, "note": "<short reason>"}], "falseAlarms": 0}
"""
RESULT_FIELDS = [
    "run_id",
    "task",
    "job",
    "model",
    "effort",
    "n",
    "status",
    "rc",
    "seconds",
    "check_rc",
    "usage_delta",
    "tokens_in",
    "tokens_out",
    "tokens_reasoning",
    "tokens_cached",
    "tokens_total",
    "date",
]


class LadderError(Exception):
    exit_code = 2


class Refused(LadderError):
    exit_code = 3


class GradeFailed(LadderError):
    exit_code = 4


class GradeFormatError(ValueError):
    pass


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# --- models ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCfg:
    id: str
    cli: str
    cli_model: str
    subscription: str
    efforts: tuple[str, ...]
    last_resort: bool
    windows: frozenset[str] | None
    never_jobs: frozenset[str]
    only_jobs: frozenset[str] | None

    def refusal(self, job: str) -> str | None:
        """Why this model may not run the job, or None if it may."""
        if self.last_resort:
            return f"{self.id} is on the lastResort subscription {self.subscription}"
        if job in self.never_jobs:
            return f"{self.id} is in neverJobs for {job}"
        if self.only_jobs is not None and job not in self.only_jobs:
            return f"{self.id} is not in onlyJobs for {job}"
        return None


def load_models(path: Path) -> dict[str, ModelCfg]:
    """Read models.json: {"models": [{id, ...}], "subscriptions": {name: {lastResort, windows}}}."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    subs = raw.get("subscriptions", {})
    models: dict[str, ModelCfg] = {}
    for m in raw["models"]:
        mid = m["id"]
        cli = m["cli"]
        efforts = tuple(
            m.get("efforts") or (EFFORT_SEARCH if cli in ("codex", "claude") else [DEFAULT_EFFORT])
        )
        if efforts != (DEFAULT_EFFORT,) and efforts != tuple(EFFORT_SEARCH):
            raise LadderError(f'{mid}: efforts must be ["default"] or [low, medium, high]')
        sub = m["subscription"]
        sub_cfg = subs.get(sub, {})
        windows = sub_cfg.get("windows")
        only = m.get("onlyJobs")
        models[mid] = ModelCfg(
            id=mid,
            cli=cli,
            cli_model=m.get("cliModel", mid),
            subscription=sub,
            efforts=efforts,
            last_resort=bool(sub_cfg.get("lastResort", False)),
            windows=frozenset(windows) if windows is not None else None,
            never_jobs=frozenset(m.get("neverJobs", [])),
            only_jobs=frozenset(only) if only is not None else None,
        )
    return models


def resolve_effort(model: ModelCfg, effort: str | None) -> str:
    if effort is None:
        if model.efforts == (DEFAULT_EFFORT,):
            return DEFAULT_EFFORT
        raise LadderError(f"{model.id} needs an effort: {', '.join(model.efforts)}")
    if effort not in model.efforts:
        raise LadderError(f"{model.id} has no effort {effort!r}")
    return effort


def grader_effort(model: ModelCfg) -> str:
    """Grading runs at medium when the model lists it, else at its last effort."""
    return "medium" if "medium" in model.efforts else model.efforts[-1]


# --- launching (the only places that touch a real CLI or quota) -----------------------


@dataclass(frozen=True)
class Launched:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def launch(argv: list[str], cwd: Path, timeout: float) -> Launched:
    """Run a CLI with its stdin closed and a timeout. Tests replace this function."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return Launched(None, _text(exc.stdout), _text(exc.stderr), True)
    except OSError as exc:
        return Launched(127, "", str(exc), False)
    return Launched(proc.returncode, proc.stdout, proc.stderr, False)


def run_check(cmd: str, cwd: Path, timeout: float) -> tuple[int, str]:
    """Run a task's `check` shell command in the work dir. Returns (exit code, output)."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, _text(exc.stdout) + _text(exc.stderr)
    return proc.returncode, proc.stdout + proc.stderr


def call_quota() -> dict | None:
    """Run `quota status --json`. None when quota is offline. Tests replace this function."""
    cmd = shlex.split(os.environ.get("LADDER_QUOTA_CMD", str(DEFAULT_QUOTA)))
    try:
        proc = subprocess.run(
            [*cmd, "status", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def pause(seconds: float) -> None:
    """Sleep helper; tests replace this so the settle wait costs nothing."""
    time.sleep(seconds)


def build_prompt(task_prompt: str) -> str:
    return f"{task_prompt.rstrip()}\n\n{FINAL_LINE}\n"


def build_command(
    model: ModelCfg, effort: str, mode: str, prompt: str, work: Path, out: Path
) -> list[str]:
    """Argument list for one CLI launch. Never a shell string."""
    if mode not in ("read", "write"):
        raise LadderError(f"unknown mode {mode!r}")
    write = mode == "write"
    cli, m = model.cli, model.cli_model
    if cli == "codex":
        argv = [
            "codex",
            "exec",
            "-s",
            "workspace-write" if write else "read-only",
            "--skip-git-repo-check",
            "--json",
            "-m",
            m,
        ]
        if effort != DEFAULT_EFFORT:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        return [*argv, "-o", str(out), prompt]
    if cli == "claude":
        argv = [
            "env",
            "-u",
            "CLAUDE_CONFIG_DIR",
            "claude",
            "-p",
            "--model",
            m,
            "--output-format",
            "json",
        ]
        if effort != DEFAULT_EFFORT:
            argv += ["--effort", effort]
        if write:
            argv += ["--permission-mode", "acceptEdits", "--allowedTools=Read,Grep,Glob,Edit,Write"]
        else:
            argv += ["--allowedTools=Read,Grep,Glob"]
        return [*argv, "--", prompt]
    if cli == "cursor":
        argv = ["cursor-agent", "-p", "--trust"]
        argv += ["--force"] if write else ["--mode", "ask"]
        return [*argv, "--model", m, "--workspace", str(work), prompt]
    if cli == "opencode":
        # The spec gives opencode no read-only flag, so read mode is not enforced for it.
        return ["opencode", "run", "--standalone", "--format", "json", "-m", m, prompt]
    raise LadderError(f"unknown cli {cli!r} for {model.id}")


def last_text_event(stream: str) -> str:
    """Text of the last `"type": "text"` event in an opencode JSON-lines stream."""
    text = ""
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        value = event.get("text", part.get("text"))
        if isinstance(value, str):
            text = value
    return text


def collect_answer(cli: str, launched: Launched, out: Path) -> str:
    if cli == "codex":
        return out.read_text(encoding="utf-8") if out.exists() else ""
    if cli == "opencode":
        return last_text_event(launched.stdout)
    if cli == "claude":
        try:
            data = json.loads(launched.stdout)
        except (ValueError, TypeError):
            return launched.stdout
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return data["result"]
        return launched.stdout
    return launched.stdout


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _token_record(
    tokens_in: object, tokens_out: object, reasoning: object, cached: object
) -> dict[str, int | None]:
    values = [_int_or_none(v) for v in (tokens_in, tokens_out, reasoning, cached)]
    total = values[0] + values[1] if values[0] is not None and values[1] is not None else None
    names = ("tokens_in", "tokens_out", "tokens_reasoning", "tokens_cached", "tokens_total")
    return dict(zip(names, [*values, total], strict=True))


def token_usage(cli: str, stream: str) -> dict[str, int | None]:
    """Extract token counts from CLI JSON output; unknown or incomplete counts stay null."""
    if cli == "codex":
        for line in reversed(stream.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                response = event.get("response")
                usage = response.get("usage") if isinstance(response, dict) else None
            if isinstance(usage, dict):
                return _token_record(
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("reasoning_output_tokens"),
                    usage.get("cached_input_tokens"),
                )
        return _token_record(None, None, None, None)
    if cli == "claude":
        try:
            data = json.loads(stream)
        except (ValueError, TypeError):
            return _token_record(None, None, None, None)
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            return _token_record(
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("reasoning_tokens"),
                usage.get("cache_read_input_tokens"),
            )
        return _token_record(None, None, None, None)
    if cli == "opencode":
        totals: list[int | None] = [None, None, None, None]
        for line in stream.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            part = event.get("part") if isinstance(event, dict) else None
            tokens = part.get("tokens") if isinstance(part, dict) else None
            if not isinstance(tokens, dict):
                continue
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            values = [
                tokens.get("input"),
                tokens.get("output"),
                tokens.get("reasoning"),
                cache.get("read"),
            ]
            for index, value in enumerate(values):
                count = _int_or_none(value)
                if count is not None:
                    totals[index] = (totals[index] or 0) + count
        return _token_record(*totals)
    return _token_record(None, None, None, None)


# --- usage measurement and budget gate ------------------------------------------------


@dataclass(frozen=True)
class Window:
    used_pct: float | None
    resets_at: int | None  # epoch ms, or None when quota does not say


def snapshot(sub: str, windows: frozenset[str] | None) -> dict[str, Window] | None:
    """Usage windows for one subscription, or None when quota is offline.

    `windows` is the subscription's configured window list; None means every limit counts.
    """
    status = call_quota()
    if status is None:
        return None
    entry = next(
        (s for s in status.get("subscriptions", []) if isinstance(s, dict) and s.get("id") == sub),
        None,
    )
    limits = entry.get("limits") if entry else None
    if not isinstance(limits, list):
        return None
    out: dict[str, Window] = {}
    for limit in limits:
        name = limit.get("window")
        if not isinstance(name, str) or (windows is not None and name not in windows):
            continue
        used = limit.get("usedPct")
        out[name] = Window(float(used) if used is not None else None, limit.get("resetsAt"))
    return out


def usage_deltas(
    before: dict[str, Window] | None, after: dict[str, Window] | None
) -> dict[str, float]:
    if not before or not after:
        return {}
    deltas: dict[str, float] = {}
    for name, w_after in after.items():
        w_before = before.get(name)
        if w_before is None or w_before.used_pct is None or w_after.used_pct is None:
            continue
        deltas[name] = w_after.used_pct - w_before.used_pct
    return deltas


class Ledger:
    """Test usage per subscription window, reset whenever the window's resetsAt changes."""

    def __init__(self, path: Path, data: dict) -> None:
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path) -> Ledger:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return cls(path, data)

    def windows(self, sub: str) -> list[str]:
        return list(self.data.get(sub, {}))

    def total(self, sub: str, window: str, resets_at: int | None) -> float:
        entry = self.data.get(sub, {}).get(window)
        if not entry:
            return 0.0
        if resets_at is not None and entry.get("resetsAt") != resets_at:
            return 0.0
        return float(entry.get("delta", 0.0))

    def add(self, sub: str, window: str, resets_at: int | None, delta: float) -> None:
        if delta <= 0:
            return
        windows = self.data.setdefault(sub, {})
        entry = windows.get(window)
        if entry is None or entry.get("resetsAt") != resets_at:
            entry = {"resetsAt": resets_at, "delta": 0.0}
            windows[window] = entry
        entry["delta"] = round(entry["delta"] + delta, 4)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")


def budget_reason(
    sub: str, windows: dict[str, Window] | None, ledger: Ledger, job: str | None = None
) -> str | None:
    """Why the budget gate refuses a run on this subscription, or None if it passes."""
    windows = windows or {}
    names = sorted(set(ledger.windows(sub)) | set(windows))
    for name in names:
        w = windows.get(name)
        if (
            job not in {"read", "triage"}
            and w is not None
            and w.used_pct is not None
            and w.used_pct >= USED_LIMIT
        ):
            return f"{sub} window {name} is at {w.used_pct:.0f}% (limit {USED_LIMIT:.0f}%)"
    for name in names:
        resets = windows[name].resets_at if name in windows else None
        total = ledger.total(sub, name, resets)
        if total >= TEST_CAP:
            return f"test usage on {sub} window {name} is {total:.1f} points (cap {TEST_CAP:.1f})"
    return None


def finish_usage(
    ledger: Ledger, sub: str, windows: frozenset[str] | None, before: dict[str, Window] | None
) -> float | None:
    """Wait for tokenwatch to settle, read usage again, ledger the positive deltas."""
    pause(SETTLE_SECONDS)
    after = snapshot(sub, windows)
    if after is None:
        warn(f"quota offline after the run; usage for {sub} not recorded")
    deltas = usage_deltas(before, after)
    for name, delta in deltas.items():
        assert after is not None
        ledger.add(sub, name, after[name].resets_at, delta)
    ledger.save()
    return max(deltas.values()) if deltas else None


# --- tasks, rows and grades -----------------------------------------------------------


@dataclass(frozen=True)
class Task:
    id: str
    job: str
    mode: str
    timeout: float
    check: str | None
    links: dict[str, str]
    dir: Path

    def key_items(self) -> list[dict]:
        return json.loads((self.dir / "key.json").read_text(encoding="utf-8"))["items"]


@dataclass(frozen=True)
class RunRow:
    run_id: str
    task: str
    job: str
    model: str
    effort: str
    n: int
    status: str
    rc: int | None
    seconds: float
    check_rc: int | None
    usage_delta: float | None
    date: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_reasoning: int | None = None
    tokens_cached: int | None = None
    tokens_total: int | None = None

    @classmethod
    def from_csv(cls, d: dict[str, str]) -> RunRow:
        return cls(
            run_id=d["run_id"],
            task=d["task"],
            job=d["job"],
            model=d["model"],
            effort=d["effort"],
            n=int(d["n"]),
            status=d["status"],
            rc=_opt_int(d["rc"]),
            seconds=float(d["seconds"] or 0),
            check_rc=_opt_int(d["check_rc"]),
            usage_delta=float(d["usage_delta"]) if d["usage_delta"] else None,
            tokens_in=_opt_int(d.get("tokens_in", "")),
            tokens_out=_opt_int(d.get("tokens_out", "")),
            tokens_reasoning=_opt_int(d.get("tokens_reasoning", "")),
            tokens_cached=_opt_int(d.get("tokens_cached", "")),
            tokens_total=_opt_int(d.get("tokens_total", "")),
            date=d["date"],
        )

    def to_csv(self) -> list[str]:
        return [
            self.run_id,
            self.task,
            self.job,
            self.model,
            self.effort,
            str(self.n),
            self.status,
            "" if self.rc is None else str(self.rc),
            f"{self.seconds:.1f}",
            "" if self.check_rc is None else str(self.check_rc),
            "" if self.usage_delta is None else f"{self.usage_delta:.2f}",
            *(
                "" if value is None else str(value)
                for value in (
                    self.tokens_in,
                    self.tokens_out,
                    self.tokens_reasoning,
                    self.tokens_cached,
                    self.tokens_total,
                )
            ),
            self.date,
        ]


def _opt_int(value: str) -> int | None:
    return int(value) if value else None


@dataclass(frozen=True)
class ComboScore:
    """One job x model x effort, aggregated over the job's tasks."""

    pct: float | None
    passed: bool
    complete: bool
    graded: int
    total: int
    seconds: float | None
    usage: float | None
    false_alarms: int
    date: str
    todo: tuple[str, ...]
    notes: tuple[str, ...]
    has_rows: bool
    tokens: float | None = None


@dataclass(frozen=True)
class Verdict:
    """Where one model stands in the effort search."""

    kind: str  # "need" (run combo), "best" (has a best effort), or "failed"
    effort: str | None
    combo: ComboScore | None
    scores: dict[str, ComboScore]


@dataclass(frozen=True)
class Plan:
    model: str | None
    effort: str | None
    combo: ComboScore | None
    notes: tuple[str, ...]


def judge_model(efforts: Sequence[str], score_of: Callable[[str], ComboScore]) -> Verdict:
    """Effort search for one model (SPEC: Ladder rules, steps 1-4)."""
    scores: dict[str, ComboScore] = {}

    def get(effort: str) -> ComboScore:
        if effort not in scores:
            scores[effort] = score_of(effort)
        return scores[effort]

    if tuple(efforts) == (DEFAULT_EFFORT,):
        s = get(DEFAULT_EFFORT)
        if not s.complete:
            return Verdict("need", DEFAULT_EFFORT, s, scores)
        if s.passed:
            return Verdict("best", DEFAULT_EFFORT, s, scores)
        return Verdict("failed", None, None, scores)

    # Step 1: low and medium always.
    for effort in ("low", "medium"):
        s = get(effort)
        if not s.complete:
            return Verdict("need", effort, s, scores)
    low, medium = scores["low"], scores["medium"]

    # Step 2: low passes and medium is not more than 10 points above it: skip high.
    if (
        low.passed
        and medium.pct is not None
        and low.pct is not None
        and medium.pct - low.pct <= EFFORT_MARGIN
    ):
        evaluated = ["low", "medium"]
    else:
        # Step 3: otherwise run high.
        high = get("high")
        if not high.complete:
            return Verdict("need", "high", high, scores)
        evaluated = ["low", "medium", "high"]

    # Step 4: the lowest passing effort within 10 points of the model's highest pct.
    pcts = [scores[e].pct for e in evaluated if scores[e].pct is not None]
    if not pcts:
        return Verdict("failed", None, None, scores)
    top = max(pcts)
    for effort in evaluated:
        s = scores[effort]
        if s.passed and s.pct is not None and s.pct >= top - EFFORT_MARGIN:
            return Verdict("best", effort, s, scores)
    return Verdict("failed", None, None, scores)


def plan_next(
    job: str,
    candidates: Sequence[tuple[str, Sequence[str]]],
    score_of: Callable[[str, str], ComboScore],
) -> Plan:
    """Climb rule over the rung-ordered candidates (SPEC: Ladder rules). Pure: no I/O."""
    verdicts: dict[int, Verdict] = {}

    def verdict(i: int) -> Verdict:
        if i not in verdicts:
            model, efforts = candidates[i]
            verdicts[i] = judge_model(efforts, lambda e: score_of(model, e))
        return verdicts[i]

    def notes_so_far() -> tuple[str, ...]:
        out: list[str] = []
        for i, v in sorted(verdicts.items()):
            model = candidates[i][0]
            out += [f"{model} {note}" for s in v.scores.values() for note in s.notes]
        return tuple(out)

    best_seen = False
    for i, (model, _) in enumerate(candidates):
        v = verdict(i)
        if v.kind == "need":
            assert v.combo is not None and v.effort is not None
            return Plan(model, v.effort, v.combo, notes_so_far())
        if not best_seen:
            best_seen = v.kind == "best"
            continue
        # i is the cross-check model after the first model with a best effort.
        break

    # Sonnet is always run for these jobs, even when the climb is already done.
    if job in ALWAYS_SONNET_JOBS:
        for i, (model, _) in enumerate(candidates):
            if model == "sonnet":
                v = verdict(i)
                if v.kind == "need" and v.combo is not None and v.effort is not None:
                    return Plan(model, v.effort, v.combo, notes_so_far())
    return Plan(None, None, None, notes_so_far())


def parse_grade(raw: str, item_ids: list[str]) -> dict:
    """Strict grader JSON: {"items": [{id, met, note}], "falseAlarms": int}, ids == key ids."""
    try:
        data = json.loads(raw.strip())
    except ValueError as exc:
        raise GradeFormatError(f"reply is not JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"items", "falseAlarms"}:
        raise GradeFormatError("top level must be exactly {items, falseAlarms}")
    false_alarms = data["falseAlarms"]
    if type(false_alarms) is not int or false_alarms < 0:
        raise GradeFormatError("falseAlarms must be an int >= 0")
    items = data["items"]
    if not isinstance(items, list):
        raise GradeFormatError("items must be a list")
    by_id: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "met", "note"}:
            raise GradeFormatError("each item needs exactly id, met, note")
        if not isinstance(item["id"], str) or type(item["met"]) is not bool:
            raise GradeFormatError("item id must be a string and met a bool")
        if not isinstance(item["note"], str):
            raise GradeFormatError("item note must be a string")
        if item["id"] in by_id:
            raise GradeFormatError(f"duplicate item id {item['id']!r}")
        by_id[item["id"]] = item
    if set(by_id) != set(item_ids):
        raise GradeFormatError(f"item ids {sorted(by_id)} do not match key {sorted(item_ids)}")
    return {"items": [by_id[i] for i in item_ids], "falseAlarms": false_alarms}


def tree_files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            if not path.is_symlink():
                found[path.relative_to(root).as_posix()] = path
    return found


def _lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return ["[binary file]\n"]


def make_diff(before: Path, after: Path) -> str:
    """Unified diff of every regular file that differs between two trees."""
    old, new = tree_files(before), tree_files(after)
    chunks: list[str] = []
    for rel in sorted(set(old) | set(new)):
        a, b = _lines(old.get(rel)), _lines(new.get(rel))
        if a != b:
            chunks.extend(difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return "".join(chunks)


def ladder_note(pct: float | None) -> str:
    return "ladder n/a" if pct is None else f"ladder {pct:.0f}%"


def token_note(tokens: float | None) -> str:
    value = "n/a" if tokens is None else f"{tokens:g}"
    return f"tokens {value}"


# --- the workspace: every file the commands read or write -----------------------------


class Workspace:
    def __init__(self, root: Path, models_path: Path) -> None:
        self.root = root
        self.models_path = models_path
        self.tasks_dir = root / "tasks"
        self.runs_dir = root / "runs"
        self.grades_dir = root / "grades"
        self.results_path = root / "results.csv"
        self.ledger_path = root / "ledger.json"
        self._models: dict[str, ModelCfg] | None = None

    def models(self) -> dict[str, ModelCfg]:
        if self._models is None:
            if not self.models_path.exists():
                raise LadderError(f"models file not found: {self.models_path}")
            self._models = load_models(self.models_path)
        return self._models

    def model(self, model_id: str) -> ModelCfg:
        try:
            return self.models()[model_id]
        except KeyError as exc:
            raise LadderError(f"unknown model {model_id!r}") from exc

    def jobs(self) -> dict[str, dict]:
        path = self.tasks_dir / "jobs.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def tasks(self) -> list[Task]:
        found: list[Task] = []
        if not self.tasks_dir.is_dir():
            return found
        for d in sorted(self.tasks_dir.iterdir()):
            spec_path = d / "task.json"
            if d.is_dir() and spec_path.exists():
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                found.append(
                    Task(
                        id=d.name,
                        job=spec["job"],
                        mode=spec["mode"],
                        timeout=spec["timeoutSec"],
                        check=spec.get("check"),
                        links=spec.get("links", {}),
                        dir=d,
                    )
                )
        return found

    def task(self, task_id: str) -> Task:
        for t in self.tasks():
            if t.id == task_id:
                return t
        raise LadderError(f"unknown task {task_id!r}")

    def rows(self) -> list[RunRow]:
        if not self.results_path.exists():
            return []
        with self.results_path.open(newline="", encoding="utf-8") as fh:
            return [RunRow.from_csv(d) for d in csv.DictReader(fh)]

    def _append_row(self, row: RunRow) -> None:
        new = not self.results_path.exists()
        if not new:
            with self.results_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                old_rows = list(reader)
                old_fields = reader.fieldnames or []
            if old_fields != RESULT_FIELDS:
                # Upgrade legacy CSV headers while keeping every existing value readable.
                with self.results_path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=RESULT_FIELDS)
                    writer.writeheader()
                    writer.writerows(
                        {field: item.get(field, "") for field in RESULT_FIELDS} for item in old_rows
                    )
        with self.results_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(RESULT_FIELDS)
            writer.writerow(row.to_csv())

    def _next_number(self, base: str) -> int:
        if not self.runs_dir.is_dir():
            return 1
        prefix = base + "__"
        nums = [
            int(p.name[len(prefix) :])
            for p in self.runs_dir.iterdir()
            if p.is_dir() and p.name.startswith(prefix) and p.name[len(prefix) :].isdigit()
        ]
        return max(nums, default=0) + 1

    # -- run ----------------------------------------------------------------------------

    def _ready_run(
        self, task_id: str, model_id: str, effort: str | None
    ) -> tuple[Task, ModelCfg, str]:
        task = self.task(task_id)
        model = self.model(model_id)
        reason = model.refusal(task.job)
        if reason:
            raise Refused(f"refused: {reason}")
        return task, model, resolve_effort(model, effort)

    def preview_run(
        self, task_id: str, model_id: str, effort: str | None
    ) -> tuple[list[str], Path]:
        """The argv and cwd run_task would launch. Creates nothing and reads no quota."""
        task, model, effort = self._ready_run(task_id, model_id, effort)
        base = f"{task_id}__{model_id}__{effort}"
        work = self.runs_dir / f"{base}__{self._next_number(base)}" / "work"
        prompt = build_prompt((task.dir / "prompt.md").read_text(encoding="utf-8"))
        return build_command(model, effort, task.mode, prompt, work, work.parent / "out.md"), work

    def run_task(self, task_id: str, model_id: str, effort: str | None) -> RunRow:
        task, model, effort = self._ready_run(task_id, model_id, effort)

        ledger = Ledger.load(self.ledger_path)
        before = snapshot(model.subscription, model.windows)
        if before is None:
            warn(f"quota offline; budget gate for {model.subscription} uses the ledger only")
        reason = budget_reason(model.subscription, before, ledger, task.job)
        if reason:
            raise Refused(f"refused: budget gate: {reason}")

        base = f"{task_id}__{model_id}__{effort}"
        run_id = f"{base}__{self._next_number(base)}"
        run_dir = self.runs_dir / run_id
        work = run_dir / "work"
        run_dir.mkdir(parents=True)
        if (task.dir / "input").is_dir():
            shutil.copytree(task.dir / "input", work, symlinks=True)
        else:
            work.mkdir()
        for name, target in task.links.items():
            (work / name).symlink_to(target)

        prompt = build_prompt((task.dir / "prompt.md").read_text(encoding="utf-8"))
        out = run_dir / "out.md"
        argv = build_command(model, effort, task.mode, prompt, work, out)
        started = time.monotonic()
        launched = launch(argv, work, task.timeout)
        seconds = time.monotonic() - started

        answer = collect_answer(model.cli, launched, out)
        out.write_text(answer, encoding="utf-8")
        log = launched.stdout
        if launched.stderr:
            log += "\n--- stderr ---\n" + launched.stderr
        (run_dir / "log.txt").write_text(log, encoding="utf-8")
        if task.mode == "write":
            # Diff before the overlay goes in, so hidden tests never show up in the grader's view.
            (run_dir / "changes.diff").write_text(
                make_diff(task.dir / "input", work), encoding="utf-8"
            )

        overlay = task.dir / "overlay"
        if overlay.is_dir():
            shutil.copytree(overlay, work, dirs_exist_ok=True)
        check_rc: int | None = None
        if task.check:
            check_rc, check_out = run_check(task.check, work, task.timeout)
            (run_dir / "check.log").write_text(check_out, encoding="utf-8")

        # Crashed, timed out or hit a usage limit (rc != 0, no answer) is "error", not "failed".
        status = (
            "error"
            if launched.timed_out or (launched.returncode != 0 and not answer.strip())
            else "ok"
        )
        usage = finish_usage(ledger, model.subscription, model.windows, before)
        tokens = token_usage(model.cli, launched.stdout)
        row = RunRow(
            run_id=run_id,
            task=task_id,
            job=task.job,
            model=model_id,
            effort=effort,
            n=int(run_id.rsplit("__", 1)[1]),
            status=status,
            rc=launched.returncode,
            seconds=seconds,
            check_rc=check_rc,
            usage_delta=usage,
            **tokens,
            date=dt.date.today().isoformat(),
        )
        (run_dir / "meta.json").write_text(
            json.dumps(
                {**row.__dict__, "timed_out": launched.timed_out, "mode": task.mode},
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        self._append_row(row)
        return row

    # -- grade --------------------------------------------------------------------------

    def find_row(self, run_id: str) -> RunRow:
        for row in self.rows():
            if row.run_id == run_id:
                return row
        raise LadderError(f"no results row for {run_id}")

    def _ready_grade(self, run_id: str, grader_id: str) -> tuple[Task, ModelCfg, str]:
        row = self.find_row(run_id)
        if row.status != "ok":
            raise LadderError(f"{run_id} is an error run; nothing to grade")
        task = self.task(row.task)
        grader = self.model(grader_id)
        reason = grader.refusal(task.job)
        if reason:
            raise Refused(f"refused: {reason}")
        return task, grader, grader_effort(grader)

    def preview_grade(self, run_id: str, grader_id: str = "sol") -> tuple[list[str], Path]:
        """The argv and cwd grade_run would launch. The grader's temp dir is not created yet,
        so the cwd is a placeholder for it."""
        _, grader, effort = self._ready_grade(run_id, grader_id)
        tmp = Path(GRADE_TMP_PLACEHOLDER)
        argv = build_command(grader, effort, "read", GRADER_PROMPT, tmp, tmp / "reply.md")
        return argv, tmp

    def grade_run(self, run_id: str, grader_id: str = "sol") -> Path:
        task, grader, effort = self._ready_grade(run_id, grader_id)
        run_dir = self.runs_dir / run_id
        item_ids = [item["id"] for item in task.key_items()]

        ledger = Ledger.load(self.ledger_path)
        before = snapshot(grader.subscription, grader.windows)
        reason = budget_reason(grader.subscription, before, ledger, task.job)
        if reason:
            raise Refused(f"refused: budget gate: {reason}")

        tmp = Path(tempfile.mkdtemp(prefix="ladder-grade-"))
        try:
            shutil.copy(task.dir / "prompt.md", tmp / "prompt.md")
            shutil.copy(run_dir / "out.md", tmp / "out.md")
            shutil.copy(task.dir / "key.json", tmp / "key.json")
            if (run_dir / "changes.diff").exists():
                shutil.copy(run_dir / "changes.diff", tmp / "changes.diff")
            (tmp / "GRADER.md").write_text(GRADER_INSTRUCTIONS, encoding="utf-8")

            grade: dict | None = None
            raw = ""
            error = ""
            for _attempt in range(2):  # one retry on bad JSON
                out = tmp / "reply.md"
                argv = build_command(grader, effort, "read", GRADER_PROMPT, tmp, out)
                launched = launch(argv, tmp, task.timeout)
                raw = collect_answer(grader.cli, launched, out)
                try:
                    grade = parse_grade(raw, item_ids)
                    break
                except GradeFormatError as exc:
                    error = str(exc)
            if grade is None:
                (run_dir / "grade_raw.txt").write_text(raw, encoding="utf-8")
                finish_usage(ledger, grader.subscription, grader.windows, before)
                raise GradeFailed(f"grade failed after 2 attempts: {error}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        finish_usage(ledger, grader.subscription, grader.windows, before)
        self.grades_dir.mkdir(exist_ok=True)
        out_path = self.grades_dir / f"{run_id}.json"
        out_path.write_text(
            json.dumps({**grade, "grader": f"{grader_id}@{effort}"}, indent=2) + "\n",
            encoding="utf-8",
        )
        return out_path

    def regrade_run(self, run_id: str, grader_id: str = "sol") -> Path:
        old = self.grades_dir / f"{run_id}.json"
        if old.exists():
            previous = self.grades_dir / f"{run_id}.prev.json"
            old.replace(previous)
        return self.grade_run(run_id, grader_id)

    # -- score, next, propose -----------------------------------------------------------

    def _grade(self, run_id: str) -> dict | None:
        path = self.grades_dir / f"{run_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def combo_score(
        self, job: str, model: str, effort: str, rows: list[RunRow] | None = None
    ) -> ComboScore:
        if rows is None:
            rows = self.rows()
        cfg = self.jobs().get(job)
        if cfg is None:
            raise LadderError(f"job {job!r} is not in jobs.json")
        job_tasks = [t for t in self.tasks() if t.job == job]
        met = total = false_alarms = graded = 0
        critical_ok = checks_ok = True
        seconds: list[float] = []
        usage: list[float] = []
        token_counts: list[int] = []
        dates: list[str] = []
        todo: list[str] = []
        notes: list[str] = []
        has_rows = False

        for task in job_tasks:
            run_rows = [
                r for r in rows if r.task == task.id and r.model == model and r.effort == effort
            ]
            has_rows = has_rows or bool(run_rows)
            token_counts.extend(r.tokens_total for r in run_rows if r.tokens_total is not None)
            ok = [r for r in run_rows if r.status == "ok"]
            done = [r for r in ok if self._grade(r.run_id) is not None]
            if done:
                row = max(done, key=lambda r: r.n)
                grade = self._grade(row.run_id)
                assert grade is not None
                met_by_id = {it["id"]: it["met"] for it in grade["items"]}
                for item in task.key_items():
                    total += 1
                    if met_by_id.get(item["id"]):
                        met += 1
                    elif item.get("critical"):
                        critical_ok = False
                false_alarms += int(grade["falseAlarms"])
                graded += 1
                if row.check_rc not in (None, 0):
                    checks_ok = False
                seconds.append(row.seconds)
                if row.usage_delta is not None:
                    usage.append(row.usage_delta)
                dates.append(row.date)
            elif ok:
                todo.append(f"grade {max(ok, key=lambda r: r.n).run_id}")
            elif len(run_rows) >= 2:
                notes.append(f"{task.id}: skipped after {len(run_rows)} error runs")
            elif run_rows:
                todo.append(f"run {task.id} (retry after error)")
            else:
                todo.append(f"run {task.id}")

        pct = 100.0 * met / total if total else None
        max_fa = cfg.get("maxFalseAlarms")
        passed = (
            pct is not None
            and pct >= cfg["passPct"]
            and critical_ok
            and checks_ok
            and (max_fa is None or false_alarms <= max_fa)
        )
        return ComboScore(
            pct=pct,
            passed=passed,
            complete=not todo and has_rows,
            graded=graded,
            total=len(job_tasks),
            seconds=sum(seconds) / len(seconds) if seconds else None,
            usage=sum(usage) if usage else None,
            false_alarms=false_alarms,
            date=max(dates, default=""),
            todo=tuple(todo),
            notes=tuple(notes),
            has_rows=has_rows,
            tokens=statistics.median(token_counts) if token_counts else None,
        )

    def judge(self, job: str, model: str, rows: list[RunRow]) -> Verdict:
        efforts = self.model(model).efforts
        return judge_model(efforts, lambda e: self.combo_score(job, model, e, rows))

    def next_plan(self, job: str) -> Plan:
        if job not in self.jobs():
            raise LadderError(f"job {job!r} is not in jobs.json")
        models = self.models()
        candidates = [
            (mid, models[mid].efforts)
            for mid in RUNG_ORDER
            if mid in models and models[mid].refusal(job) is None
        ]
        rows = self.rows()
        return plan_next(job, candidates, lambda m, e: self.combo_score(job, m, e, rows))

    def score_table(self, job: str | None) -> list[tuple[str, str, str, ComboScore]]:
        rows = self.rows()
        jobs = [job] if job else list(self.jobs())
        table = []
        for j in jobs:
            combos = sorted(
                {(r.model, r.effort) for r in rows if r.job == j}, key=lambda c: (c[0], c[1])
            )
            for model, effort in combos:
                table.append((j, model, effort, self.combo_score(j, model, effort, rows)))
        return table

    def propose(self) -> list[dict]:
        rows = self.rows()
        entries: list[dict] = []
        models = self.models()
        for job in self.jobs():
            for mid in RUNG_ORDER:
                if mid not in models or not any(r.job == job and r.model == mid for r in rows):
                    continue
                verdict = self.judge(job, mid, rows)
                if verdict.kind == "need":
                    continue  # not final yet
                passed = None
                if verdict.kind == "best" and verdict.effort is not None:
                    s = verdict.scores[verdict.effort]
                    passed = {
                        "effort": verdict.effort,
                        "date": s.date,
                        "note": f"{ladder_note(s.pct)}; {token_note(s.tokens)}",
                    }
                failed = [
                    {
                        "effort": e,
                        "date": s.date,
                        "note": f"{ladder_note(s.pct)}; {token_note(s.tokens)}",
                    }
                    for e, s in verdict.scores.items()
                    if s.complete and not s.passed
                ]
                entries.append({"job": job, "model": mid, "passed": passed, "failed": failed})
        return entries

    def budget_table(self) -> list[tuple[str, str, float]]:
        ledger = Ledger.load(self.ledger_path)
        return [
            (sub, window, ledger.total(sub, window, None))
            for sub in sorted(ledger.data)
            for window in sorted(ledger.data[sub])
        ]


# --- command line ---------------------------------------------------------------------


def _workspace() -> Workspace:
    models_path = Path(os.environ.get("LADDER_MODELS_FILE", DEFAULT_MODELS))
    return Workspace(ROOT, models_path)


def _print_plan(plan: Plan) -> None:
    if plan.model is None:
        print("done")
    else:
        print(f"{plan.model} {plan.effort}")
        assert plan.combo is not None
        for item in plan.combo.todo:
            print(f"  {item}")
    for note in plan.notes:
        print(f"  note: {note}")


def _print_dry_run(argv: list[str], cwd: Path) -> None:
    print(f"cwd: {cwd}")
    print(f"argv: {json.dumps(argv)}")


def _print_score(table: list[tuple[str, str, str, ComboScore]]) -> None:
    header = f"{'job':<12}{'model':<14}{'effort':<9}{'graded':<8}{'pct':>7}  {'pass':<10}"
    print(header + f"{'seconds':>9}{'tokens':>9}{'usage':>8}")
    for job, model, effort, s in table:
        pct = "n/a" if s.pct is None else f"{s.pct:.1f}"
        if not s.complete:
            verdict = "incomplete"
        else:
            verdict = "yes" if s.passed else "no"
        seconds = "" if s.seconds is None else f"{s.seconds:.1f}"
        tokens = "" if s.tokens is None else f"{s.tokens:g}"
        usage = "" if s.usage is None else f"{s.usage:.1f}"
        print(
            f"{job:<12}{model:<14}{effort:<9}{f'{s.graded}/{s.total}':<8}{pct:>7}  "
            f"{verdict:<10}{seconds:>9}{tokens:>9}{usage:>8}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ladder.py", description="Find the cheapest model + effort per job."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run one task with one model and effort")
    p.add_argument("task")
    p.add_argument("model")
    p.add_argument("effort", nargs="?")
    p.add_argument("--dry-run", action="store_true", help="print the argv and cwd, launch nothing")
    p = sub.add_parser("grade", help="grade one run")
    p.add_argument("run_id")
    p.add_argument("--grader", default="sol")
    p.add_argument("--dry-run", action="store_true", help="print the argv and cwd, launch nothing")
    p = sub.add_parser("regrade", help="move the previous grade aside and grade a run again")
    p.add_argument("run_id")
    p.add_argument("--grader", default="sol")
    p.add_argument("--dry-run", action="store_true", help="print the argv and cwd, launch nothing")
    p = sub.add_parser("score", help="aggregate results per job x model x effort")
    p.add_argument("job", nargs="?")
    p = sub.add_parser("next", help="print the next model and effort to run for a job")
    p.add_argument("job")
    sub.add_parser("propose", help="print capability entries as JSON")
    sub.add_parser("budget", help="print the test-usage ledger against the cap")
    args = parser.parse_args(argv)

    ws = _workspace()
    try:
        if args.cmd == "run" and args.dry_run:
            _print_dry_run(*ws.preview_run(args.task, args.model, args.effort))
        elif args.cmd == "run":
            row = ws.run_task(args.task, args.model, args.effort)
            print(f"{row.run_id}: {row.status} rc={row.rc} check={row.check_rc}")
            print(f"usage delta={row.usage_delta}")
        elif args.cmd == "grade" and args.dry_run:
            _print_dry_run(*ws.preview_grade(args.run_id, args.grader))
        elif args.cmd == "grade":
            print(ws.grade_run(args.run_id, args.grader))
        elif args.cmd == "regrade" and args.dry_run:
            _print_dry_run(*ws.preview_grade(args.run_id, args.grader))
        elif args.cmd == "regrade":
            print(ws.regrade_run(args.run_id, args.grader))
        elif args.cmd == "score":
            _print_score(ws.score_table(args.job))
        elif args.cmd == "next":
            _print_plan(ws.next_plan(args.job))
        elif args.cmd == "propose":
            print(json.dumps(ws.propose(), indent=2))
        elif args.cmd == "budget":
            table = ws.budget_table()
            if not table:
                print(f"no test usage recorded (cap {TEST_CAP:.1f} points per window)")
            for sub_name, window, total in table:
                print(f"{sub_name:<20}{window:<20}{total:6.2f} / {TEST_CAP:.1f}")
    except LadderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
