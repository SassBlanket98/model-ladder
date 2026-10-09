# model-ladder

A single-file CLI that finds the cheapest coding model, and the lowest reasoning effort, that can do a given kind of job. It runs models on tasks with known answers, starting with the cheapest and climbing when a model fails. When one passes, the next model up runs once as a cross-check.

I run several coding agents (Codex, Claude Code, Cursor, OpenCode) on fixed subscriptions and kept guessing which model was good enough for reading code, building a slice, or reviewing a diff. I built this to measure it on tasks from my own work. AI coding agents wrote the code under my direction.

## How a task works

Each task is a folder:

```
tasks/<taskId>/
  task.json    job type, mode, timeout, optional check command and MCP servers
  prompt.md    what the model is asked; never mentions the key
  input/       copied to a fresh work dir for every run
  overlay/     hidden acceptance tests, copied in after the model finishes
  key.json     what a good answer must state or implement; never copied to the work dir
```

A run goes like this:

1. `input/` is copied to a new work dir and the model CLI is launched there with the prompt, stdin closed and a timeout.
2. The files the run changed are recorded, then `overlay/` is copied over the work dir. The model never sees those files, and they stay out of the diff the grader reads.
3. The task's `check` command runs in the work dir and its exit code is recorded.
4. A second model grades the answer against `key.json` and returns strict JSON: one verdict per key item, plus a count of false alarms (wrong claims of a defect).

A model passes a job when it meets the job's percentage of key items, meets every item marked critical, stays within the false-alarm limit, and every `check` exited 0.

## Example tasks

`tasks/build-slugify` is a small write-mode task: fix a `slugify` function with three reported bugs. It has visible tests in `input/`, hidden tests in `overlay/`, a reference fix in `solution/` and a key.

`tests/test_example_task.py` checks the task itself: the hidden tests fail on the untouched input and pass with the reference solution, and the visible tests pass both before and after.

The tasks I use day to day come from my own private projects, so they are not in this repository.

### A task that uses MCP

`tasks/build-catalog-mcp` asks the agent to fix a pricing module. A local catalogue MCP server provides the current products, prices and bulk discount policy. The task's hidden tests check the result and require successful calls to `list_skus`, `get_price` for every product, and `get_discount_policy`.

The server uses Python's standard library and JSON-RPC over stdio. It implements initialization, tool discovery and tool calls, with input validation and structured results. Its tools declare that they read local data. It logs calls to `MCP_CALL_LOG`, which the runner sets separately for each run.

The runner configures task servers for Codex and Claude Code. It refuses MCP tasks on the other CLI routes. Codex has been tested with a real agent run: all seven catalogue calls succeeded and all nine hidden checks passed. The Claude launch configuration has unit tests; it has not been tested with a real agent here.

To inspect the task without launching an agent:

```
uv run python -m pytest -q tests/test_mcp_task.py
```

To run it with Codex installed and logged in:

```
LADDER_MODELS_FILE=$PWD/models.example.json uv run python ladder.py run --dry-run build-catalog-mcp luna low
LADDER_MODELS_FILE=$PWD/models.example.json uv run python ladder.py run build-catalog-mcp luna low
```

The run folder contains `mcp-calls.jsonl` and `check.log`. A successful agent exit and a successful task check are separate results; inspect both. The server and catalogue remain outside the agent's work folder, and the hidden tests are copied in after it finishes. This is a benchmark convention, not a security boundary against an agent deliberately reading or changing those files.

## Running it

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). The tool has no dependencies outside the standard library; pytest and ruff are dev dependencies.

```
uv run python -m pytest -q        # no network or real model launches
uv run ruff check .
```

To try the commands without my setup, point it at the example model list:

```
export LADDER_MODELS_FILE=$PWD/models.example.json
uv run python ladder.py next build      # prints the next model and effort to try
uv run python ladder.py score           # results table, empty until you run something
```

`ladder.py run <taskId> <model> [<effort>]` launches a real agent CLI, so it needs that CLI installed and logged in, and real model ids in your models file.

| Command | What it does |
|---|---|
| `run` | Run one task with one model and effort |
| `grade`, `regrade` | Grade a run against the key with a second model |
| `score` | Aggregate results per job, model and effort |
| `next` | Print the next model and effort to try for a job |
| `propose` | Print the capability entries the results justify, as JSON |
| `budget` | Print how much quota the tests have used against the cap |

[SPEC.md](SPEC.md) has the full behaviour: launch commands per CLI, the climb rules, grading format and the usage budget.

## Limits

- It is built around my own setup. The order models are tried in is a list in `ladder.py` (`RUNG_ORDER`), and the ids in your models file have to match it.
- The usage budget reads quota from a separate local tool. Without it, runs still work and usage is recorded as unknown. Set `LADDER_QUOTA_CMD` to use your own.
- Grading by a model is a judgement, so the key items are written to be checkable and the `check` command carries the pass or fail for write tasks.
- `run` executes the task's `check` command and an agent CLI on your machine. Only run tasks you trust.

## Licence

MIT. See [LICENSE](LICENSE).
