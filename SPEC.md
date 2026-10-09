# model-ladder: spec

Finds the cheapest model + effort that can do each job type, on real past tasks with known answers.
Results become `capability` entries in `~/.agents/picker/models.json` (applied by the lead, not by this tool).

## Layout
```
ladder.py            single-file CLI, Python 3.12+, stdlib only, type hints, run with `uv run ladder.py` or python3
tests/               pytest, no network, no real model launches (launch is injected/mocked)
tasks/jobs.json      { "<job>": { "passPct": 80, "maxFalseAlarms": null|int } }   (review: 78, 1)
tasks/<taskId>/task.json   { "job", "mode": "read"|"write", "timeoutSec", "check": "<shell cmd>"?, "links": {"<name>": "<abs path>"}? }
tasks/<taskId>/prompt.md   what the model is asked; never mentions the key
tasks/<taskId>/input/      copied to a fresh work dir for every run
tasks/<taskId>/overlay/    optional; copied over the work dir AFTER the model finishes, BEFORE `check` (hidden acceptance tests)
tasks/<taskId>/key.json    { "items": [ { "id", "text", "critical": bool } ], "nonIssues": ["..."]? }   never copied to the work dir
runs/<runId>/        work/ (the work dir), out.md (model answer), log.txt, meta.json
grades/<runId>.json  { "items": [ { "id", "met": bool, "note" } ], "falseAlarms": int, "grader": "<model@effort>" }
results.csv          one row per run
ledger.json          test usage per subscription window
```
runId = `<taskId>__<model>__<effort>__<n>`.

## Commands
- `ladder.py run <taskId> <model> [<effort>]` : model is an id from models.json. Refuses (exit 3, reason printed) when: the model's subscription is `lastResort`; the budget gate fails; the job is in the model's neverJobs. Copies input/, makes `links` symlinks, launches the CLI with stdin closed and the task timeout, writes out.md/log.txt, applies overlay/, runs `check` (cwd = work dir, exit code recorded), appends results.csv, updates ledger.json.
- `ladder.py grade <runId> [--grader sol]` : launches the grader read-only in a temp dir holding only prompt.md, out.md, key.json, a `git diff`-style listing of files the run changed (write mode), and the grader instructions. The grader must return JSON only, matching grades/ format: one verdict per key item (met only if the answer clearly states or implements it), and falseAlarms = number of distinct wrong claims of a defect or fact (items in nonIssues count). Parse strictly; on bad JSON retry once, then fail.
- `ladder.py regrade <runId> [--grader sol]` : moves an existing grade to `grades/<runId>.prev.json`, then grades the run again.
- `ladder.py score [<job>]` : per job x model x effort, aggregate over that job's tasks: pct = met items / all items; pass = pct >= passPct AND every critical item met AND (falseAlarms <= maxFalseAlarms when set) AND every `check` exited 0. Prints a table with pct, pass, seconds, median tokens_total, and the usage delta.
- `ladder.py next <job>` : prints the next (model, effort) to run for that job, or `done`. Rules below.
- `ladder.py propose` : prints the capability entries the results justify, as JSON: `passed` for the chosen effort of each passing model, `failed` for efforts that failed, with date and a note containing `ladder <pct>%` and median token total.
- `ladder.py budget` : prints the ledger against the cap.

## Launch commands (stdin always /dev/null; prompt passed as one argument)
- cli codex: `codex exec -s <read-only|workspace-write> --skip-git-repo-check --json -m <cliModel> -c model_reasoning_effort=<effort> -o <runs/id/out.md> <prompt>` with cwd = work dir. JSON stdout is retained in log.txt; token usage is read from the usage event.
- cli claude: `env -u CLAUDE_CONFIG_DIR claude -p --model <cliModel> --output-format json --effort <effort> --allowedTools=Read,Grep,Glob` (read) or `--permission-mode acceptEdits --allowedTools=Read,Grep,Glob,Edit,Write` (write) `-- <prompt>`, cwd = work dir. The JSON result is parsed into out.md; raw JSON is in log.txt.
- cli cursor: `cursor-agent -p --trust <--mode ask | --force> --model <cliModel> --workspace <work dir> <prompt>`, stdout = out.md.
- cli opencode: `opencode run --standalone --format json -m <cliModel> <prompt>`, cwd = work dir; out.md = the text of the last event with type "text" in the JSON stream; the raw stream goes to log.txt. Token fields from step-finish events are summed.
- effort "default" = the CLI takes no effort flag.
The prompt is prompt.md plus one fixed line: "Write your full answer as your final message." Models read from models.json at `~/.agents/picker/models.json` (override: env LADDER_MODELS_FILE).

## Usage measurement and budget
Every run records `tokens_in`, `tokens_out`, `tokens_reasoning`, `tokens_cached`, and `tokens_total` in meta.json and results.csv; unavailable values are null. Before and after each run call `~/.agents/bin/quota status --json` (override: env LADDER_QUOTA_CMD) and store, for the model's subscription, usedPct per window; delta = after - before (tokenwatch may lag: wait 20 s before the "after" read). If quota is offline record nulls and warn. ledger.json sums positive deltas per subscription+window since that window's last reset (resetsAt changes = new cycle). Gate: refuse a run when the summed test delta for any window of that subscription is >= 5.0 points, or when any window usedPct >= 90 unless the task's job is `read` or `triage`. Forecasts are deliberately ignored.
Grader runs are measured and ledgered the same way against the grader's subscription.

## Ladder rules (`next`)
Rung order of models (cheapest first): luna, haiku, deepseek, sonnet, composer, grok-medium, then sol, opus, grok-high. Models barred from the job (neverJobs/onlyJobs) or on lastResort subscriptions are left out. Frontier models are only reached when no cheaper model passed.
Effort search inside one model (efforts in order low, medium, high; "default" = single run):
1. Run low, then medium, always.
2. If low passes and medium's pct is not more than 10 points above low: best = low; skip high.
3. Otherwise run high.
4. best = the lowest effort that passes and is within 10 points of that model's highest pct. No effort passes = model failed.
Climb: after the first model with a best effort, run exactly one more model (the next in rung order) as a cross-check, then `done`. Sonnet is always run (effort search) for jobs review, plan-small, plan, judgement, even after `done` would otherwise be reached.
A run that crashed, timed out or hit a usage limit (rc != 0 with empty out.md) is "error", not "failed": `next` offers it again once, then skips it with a note.
