"""The MCP example task: the server speaks the protocol, and the check needs real tool use."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ladder

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "build-catalog-mcp"
SERVER = TASK / "mcp" / "catalog_server.py"


class Client:
    """Just enough of an MCP stdio client to drive the server: one JSON message per line."""

    def __init__(self, call_log: Path | None = None) -> None:
        env = {"MCP_CALL_LOG": str(call_log)} if call_log else {}
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={"PATH": "/usr/bin:/bin", **env},
        )
        self.next_id = 0

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        assert self.proc.stdout is not None
        answer = json.loads(self.proc.stdout.readline())
        assert answer["jsonrpc"] == "2.0" and answer["id"] == self.next_id
        return answer

    def start(self) -> dict:
        hello = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return hello["result"]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        assert self.proc.wait(timeout=5) == 0


@pytest.fixture
def client(tmp_path: Path):
    c = Client(tmp_path / "mcp-calls.jsonl")
    yield c
    if c.proc.poll() is None:
        c.close()


def test_initialize_negotiates_version_and_offers_tools(client: Client) -> None:
    result = client.start()
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "catalog"
    # A notification gets no reply, so the next line on stdout answers the ping.
    assert client.request("ping")["result"] == {}


def test_unsupported_version_falls_back_to_the_latest_supported() -> None:
    c = Client()
    answer = c.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    assert answer["result"]["protocolVersion"] == "2025-06-18"
    c.close()


def test_tools_list_has_valid_schemas(client: Client) -> None:
    client.start()
    tools = client.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_skus", "get_price", "get_discount_policy"]
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
    assert tools[1]["inputSchema"]["required"] == ["sku"]


def test_every_tool_declares_it_is_read_only_and_closed_world(client: Client) -> None:
    # A real Codex run (approval policy "never") refused each catalogue call as "requires
    # approval". The catalogue only reads a local file, so each tool declares that truthfully.
    client.start()
    tools = client.request("tools/list")["result"]["tools"]
    for tool in tools:
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }, tool["name"]


def test_tool_call_returns_text_and_structured_content(client: Client) -> None:
    client.start()
    result = client.call("get_price", {"sku": "WID-2"})["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "sku": "WID-2",
        "unit_price_cents": 2650,
        "currency": "USD",
    }
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_bad_argument_is_a_tool_error_the_model_can_read(client: Client) -> None:
    client.start()
    result = client.call("get_price", {"sku": "NOPE-0"})["result"]
    assert result["isError"] is True
    assert "list_skus" in result["content"][0]["text"]


@pytest.mark.parametrize("arguments", [["bad"], [], "bad", 1, True, None])
def test_non_object_tool_arguments_do_not_crash_server(client: Client, arguments) -> None:
    client.start()
    reply = client.request("tools/call", {"name": "get_price", "arguments": arguments})
    assert reply["error"]["code"] == -32602
    assert client.request("ping")["result"] == {}


@pytest.mark.parametrize("params", [["bad"], "bad", 1, True])
def test_non_object_params_do_not_crash_server(client: Client, params) -> None:
    client.start()
    reply = client.request("tools/call", params)
    assert reply["error"]["code"] == -32602
    assert client.request("ping")["result"] == {}


@pytest.mark.parametrize("name", ["list_skus", "get_price", "get_discount_policy"])
def test_unexpected_tool_arguments_are_rejected(client: Client, name: str) -> None:
    client.start()
    result = client.call(name, {"sku": "WID-1", "unexpected": True})["result"]
    assert result["isError"] is True
    assert client.request("ping")["result"] == {}


def test_unknown_tool_and_method_are_protocol_errors(client: Client) -> None:
    client.start()
    assert client.call("drop_tables")["error"]["code"] == -32602
    assert client.request("resources/list")["error"]["code"] == -32601


def test_calls_are_logged(client: Client, tmp_path: Path) -> None:
    client.start()
    client.call("get_discount_policy")
    client.call("get_price", {"sku": "NOPE-0"})
    client.close()
    lines = (tmp_path / "mcp-calls.jsonl").read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in lines]
    assert [(c["tool"], c["ok"]) for c in calls] == [
        ("get_discount_policy", True),
        ("get_price", False),
    ]


# --- the task's check ---------------------------------------------------------------


def run_check(tmp_path: Path, *, with_solution: bool, use_tools: bool) -> int:
    run_dir = tmp_path / "run"
    work = run_dir / "work"
    shutil.copytree(TASK / "input", work)
    if use_tools:
        c = Client(run_dir / "mcp-calls.jsonl")
        c.start()
        skus = c.call("list_skus")["result"]["structuredContent"]["skus"]
        for row in skus:
            c.call("get_price", {"sku": row["sku"]})
        c.call("get_discount_policy")
        c.close()
    if with_solution:
        shutil.copytree(TASK / "solution", work, dirs_exist_ok=True)
    shutil.copytree(TASK / "overlay", work, dirs_exist_ok=True)
    check = json.loads((TASK / "task.json").read_text(encoding="utf-8"))["check"]
    rc, _ = ladder.run_check(check, work, 60, {"LADDER_RUN_DIR": str(run_dir)})
    return rc


def test_check_fails_on_untouched_input(tmp_path: Path) -> None:
    assert run_check(tmp_path, with_solution=False, use_tools=True) != 0


def test_check_passes_with_reference_solution_and_tool_use(tmp_path: Path) -> None:
    assert run_check(tmp_path, with_solution=True, use_tools=True) == 0


def test_right_answer_without_tool_use_fails(tmp_path: Path) -> None:
    assert run_check(tmp_path, with_solution=True, use_tools=False) != 0


@pytest.mark.parametrize(
    ("tool", "sku"),
    [("list_skus", None), ("get_discount_policy", None)]
    + [("get_price", sku) for sku in ("WID-1", "WID-2", "GAD-7", "BOLT-3", "NUT-4")],
)
def test_check_requires_every_catalogue_lookup(tmp_path: Path, tool: str, sku: str | None) -> None:
    assert run_check(tmp_path, with_solution=True, use_tools=True) == 0
    run_dir = tmp_path / "run"
    log = run_dir / "mcp-calls.jsonl"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    remaining = [c for c in calls if not (c["tool"] == tool and c["arguments"].get("sku") == sku)]
    log.write_text("".join(json.dumps(c) + "\n" for c in remaining))
    rc, _ = ladder.run_check(
        "python3 -m unittest -q test_hidden",
        run_dir / "work",
        60,
        {"LADDER_RUN_DIR": str(run_dir)},
    )
    assert rc != 0, f"check accepted missing {tool}({sku})"


# --- launch commands ----------------------------------------------------------------


def model(cli: str) -> ladder.ModelCfg:
    return ladder.ModelCfg(
        id="m",
        cli=cli,
        cli_model="some-model",
        subscription="sub",
        efforts=("default",),
        last_resort=False,
        windows=None,
        never_jobs=frozenset(),
        only_jobs=None,
    )


SERVERS = {"catalog": ["python3", "/abs/task/mcp/catalog_server.py"]}
ENV = {"MCP_CALL_LOG": "/abs/run/mcp-calls.jsonl"}


def test_task_loader_resolves_the_server_path() -> None:
    task = ladder.Workspace(ROOT, ROOT / "models.example.json").task("build-catalog-mcp")
    assert task.mcp == {"catalog": ["python3", str(SERVER)]}


def test_claude_gets_a_strict_mcp_config_and_the_tool_allowance(tmp_path: Path) -> None:
    argv = ladder.build_command(
        model("claude"), "default", "write", "P", tmp_path, tmp_path / "o", SERVERS, ENV
    )
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config == {
        "mcpServers": {
            "catalog": {
                "command": "python3",
                "args": ["/abs/task/mcp/catalog_server.py"],
                "env": ENV,
            }
        }
    }
    assert "--strict-mcp-config" in argv
    assert "--allowedTools=Read,Grep,Glob,Edit,Write,mcp__catalog" in argv
    assert argv[-2:] == ["--", "P"]


def test_codex_gets_config_overrides(tmp_path: Path) -> None:
    argv = ladder.build_command(
        model("codex"), "default", "write", "P", tmp_path, tmp_path / "o", SERVERS, ENV
    )
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert overrides == [
        'mcp_servers.catalog.command="python3"',
        'mcp_servers.catalog.args=["/abs/task/mcp/catalog_server.py"]',
        'mcp_servers.catalog.env={MCP_CALL_LOG = "/abs/run/mcp-calls.jsonl"}',
    ]
    assert argv[-1] == "P"


@pytest.mark.parametrize("cli", ["cursor", "opencode"])
def test_other_clis_refuse_mcp_tasks(cli: str, tmp_path: Path) -> None:
    with pytest.raises(ladder.Refused):
        ladder.build_command(
            model(cli), "default", "write", "P", tmp_path, tmp_path / "o", SERVERS, ENV
        )


def test_commands_without_mcp_are_unchanged(tmp_path: Path) -> None:
    argv = ladder.build_command(model("claude"), "default", "read", "P", tmp_path, tmp_path / "o")
    assert "--mcp-config" not in argv
    assert "--allowedTools=Read,Grep,Glob" in argv
