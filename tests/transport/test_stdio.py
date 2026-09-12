"""§5 and §24: stdout carries protocol and nothing else, ever.

On stdio, stdout is not a place where output appears --- it is the wire. A banner,
a warning, a progress line or one forgotten ``print`` is not untidy output but a
framing error, and the client's very next parse fails on it. What the user then
sees is a server that died, with nothing anywhere to say why.

So these tests read the raw bytes rather than using a client. An SDK client parses
stdout and quietly discards what it cannot understand, which would hide exactly
the evidence being looked for. Every line is required to be a JSON-RPC message,
and the noisiest paths RepoAtlas has --- a sync that invokes git, and a capability
that deliberately writes to stdout --- are made to run while that is asserted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from repoatlas.services.workspace import Workspace
from tests.transport.conftest import (
    GLOBAL,
    INITIALIZE,
    INITIALIZED,
    protocol_messages,
    raw_exchange,
    requires_console_script,
    server_environment,
    tool_call,
)

pytestmark = requires_console_script


def test_startup_and_a_tool_call_emit_no_non_protocol_stdout(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """The baseline: a whole session, and every stdout line is a message."""
    workspace, _, state = indexed
    workspace.close()

    code, out, _ = raw_exchange(
        state,
        [INITIALIZE, INITIALIZED, tool_call(2, "repoatlas_status", {"context": GLOBAL})],
    )
    messages = protocol_messages(out)
    assert code == 0, "the server exits cleanly when its transport ends"
    assert [message.get("id") for message in messages] == [1, 2]
    assert "result" in messages[1]


def test_the_command_prints_no_banner_before_the_handshake(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """A Typer or Rich greeting here would corrupt the first message.

    Asserted against an *empty* request stream, so the only thing that could have
    written to stdout is startup itself.
    """
    workspace, _, state = indexed
    workspace.close()

    code, out, _ = raw_exchange(state, [])
    assert out == "", f"the server wrote to stdout before any request: {out!r}"
    assert code == 0


def test_a_sync_over_the_protocol_does_not_contaminate_stdout(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """§24. The mutation is the loudest path: it invokes git and writes the index.

    A child process that inherited descriptor 1 would put its own output straight
    into the protocol stream, and git is invoked once per repository here.
    """
    workspace, _, state = indexed
    workspace.close()

    code, out, _ = raw_exchange(
        state,
        [
            INITIALIZE,
            INITIALIZED,
            tool_call(2, "repoatlas_sync", {"context": GLOBAL}),
            tool_call(3, "repoatlas_status", {"context": GLOBAL}),
        ],
    )
    messages = protocol_messages(out)
    assert code == 0
    assert [message.get("id") for message in messages] == [1, 2, 3]
    synced = json.loads(messages[1]["result"]["content"][0]["text"])
    assert synced["status"] in {"completed", "incomplete"}


def test_a_search_carrying_repository_text_stays_inside_its_message(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """Evidence text is data inside a result, never lines on the stream.

    Source with embedded newlines is the ordinary case, so a result that was
    written out rather than encoded would break framing on the first multi-line
    file --- which is to say, immediately.
    """
    workspace, _, state = indexed
    workspace.close()

    code, out, _ = raw_exchange(
        state,
        [
            INITIALIZE,
            INITIALIZED,
            tool_call(
                2,
                "repoatlas_search",
                {"question": "Where is SERVICE_HOST configured?", "context": GLOBAL},
            ),
        ],
    )
    messages = protocol_messages(out)
    assert code == 0
    payload = json.loads(messages[1]["result"]["content"][0]["text"])
    assert payload["evidence"], "the search returned source text"
    assert any("\n" in item["text"] for item in payload["evidence"]), (
        "the fixture supplies multi-line evidence, which is what makes this a test"
    )


def test_a_refusal_is_also_only_protocol_on_stdout(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """An error path prints no traceback, because a traceback would be framing."""
    workspace, _, state = indexed
    workspace.close()

    code, out, _ = raw_exchange(
        state,
        [
            INITIALIZE,
            INITIALIZED,
            tool_call(2, "repoatlas_relationships", {"context": GLOBAL, "target": "env:NOPE"}),
        ],
    )
    messages = protocol_messages(out)
    assert code == 0
    result = messages[1]["result"]
    assert result["isError"] is True
    assert "Traceback" not in out


#: A server whose ``status`` capability writes to stdout on purpose. The point is
#: not that RepoAtlas does this --- it does not --- but that if any layer beneath
#: the transport ever did, the protocol would survive it.
NOISY_SERVER = """
import sys
from repoatlas.capabilities.service import CapabilityService
from repoatlas.mcp.server import serve

original = CapabilityService.status


def noisy(self, request):
    print("BANNER-ON-STDOUT")
    sys.stdout.write("MORE-NOISE-ON-STDOUT\\n")
    sys.stdout.flush()
    return original(self, request)


CapabilityService.status = noisy
serve()
"""


def test_a_stray_write_to_stdout_reaches_the_operator_and_not_the_client(
    indexed: tuple[Workspace, Path, Path], tmp_path: Path
) -> None:
    """The guarantee is structural, not a promise that nobody will ever print.

    Descriptor 1 is pointed at stderr for the server's life, so a write from any
    layer --- this one is injected below the transport, where the adapter's own
    discipline cannot help --- lands with the operator instead of in the stream.
    Without that, this session would end with the client unable to parse anything.
    """
    workspace, _, state = indexed
    workspace.close()

    driver = tmp_path / "noisy_server.py"
    driver.write_text(NOISY_SERVER, encoding="utf-8")

    code, out, err = raw_exchange(
        state,
        [INITIALIZE, INITIALIZED, tool_call(2, "repoatlas_status", {"context": GLOBAL})],
        command=[sys.executable, str(driver)],
    )
    messages = protocol_messages(out)
    assert code == 0
    assert [message.get("id") for message in messages] == [1, 2]
    assert "BANNER-ON-STDOUT" not in out
    assert "MORE-NOISE-ON-STDOUT" not in out
    assert "BANNER-ON-STDOUT" in err
    assert "MORE-NOISE-ON-STDOUT" in err


#: A server with every outbound network operation made fatal. Patching the
#: operations rather than the socket type on purpose: ``ssl`` subclasses
#: ``socket.socket`` at import time, so replacing the class breaks the import and
#: proves nothing about what the server would have done.
OFFLINE_SERVER = """
import socket


def blocked(*args, **kwargs):
    raise AssertionError("NETWORK-ATTEMPTED")


socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.socket.bind = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked

from repoatlas.mcp.server import serve

serve()
"""


def test_the_server_runs_a_whole_session_with_networking_made_fatal(
    indexed: tuple[Workspace, Path, Path], tmp_path: Path
) -> None:
    """§41: the adapter is local, and the SDK's web stack is imported but unused.

    The MCP SDK brings an HTTP server and client along whether or not a stdio
    server wants them, so "it only imports transport code" is not a claim that can
    be made by reading the import list. It is made here instead: the noisiest
    session RepoAtlas has --- a retrieval and a sync that invokes git --- runs to
    completion with connect, bind and name resolution all raising.
    """
    workspace, _, state = indexed
    workspace.close()

    driver = tmp_path / "offline_server.py"
    driver.write_text(OFFLINE_SERVER, encoding="utf-8")

    code, out, err = raw_exchange(
        state,
        [
            INITIALIZE,
            INITIALIZED,
            tool_call(
                2,
                "repoatlas_search",
                {"question": "Where is SERVICE_HOST configured?", "context": GLOBAL},
            ),
            tool_call(3, "repoatlas_sync", {"context": GLOBAL}),
        ],
        command=[sys.executable, str(driver)],
    )
    messages = protocol_messages(out)
    assert "NETWORK-ATTEMPTED" not in err, err[-2000:]
    assert code == 0
    assert [message.get("id") for message in messages] == [1, 2, 3]
    searched = json.loads(messages[1]["result"]["content"][0]["text"])
    assert searched["evidence"], "retrieval worked with no network at all"
    synced = json.loads(messages[2]["result"]["content"][0]["text"])
    assert synced["status"] in {"completed", "incomplete"}


def test_operational_output_never_carries_repository_source(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """§23. Whatever reaches stderr is metadata, not evidence.

    Diverting stdout to stderr makes stderr the place stray output lands, so it is
    worth asserting that the ordinary paths put nothing there at all: a log line
    holding a search result would put source text into a place nobody treats as
    sensitive.
    """
    workspace, _, state = indexed
    workspace.close()

    _, _, err = raw_exchange(
        state,
        [
            INITIALIZE,
            INITIALIZED,
            tool_call(
                2,
                "repoatlas_search",
                {"question": "Where is SERVICE_HOST configured?", "context": GLOBAL},
            ),
        ],
    )
    assert "SERVICE_HOST" not in err
    assert "def " not in err


def test_the_command_takes_no_json_flag_and_needs_no_arguments() -> None:
    """§25 and §4: one new command, no options, and no argument to get wrong."""
    import inspect

    from repoatlas.cli.main import mcp

    assert not inspect.signature(mcp).parameters


@pytest.mark.parametrize("flag", ["--json", "--repo", "--verbose"])
def test_the_command_borrows_no_v1_option(flag: str) -> None:
    """The output is already JSON-RPC, and the scope is already in every call."""
    from repoatlas.cli.main import mcp

    assert flag.lstrip("-").replace("-", "_") not in inspect_parameters(mcp)


def inspect_parameters(function: object) -> set[str]:
    import inspect

    return set(inspect.signature(function).parameters)  # type: ignore[arg-type]


def test_the_server_environment_carries_no_host_configuration(tmp_path: Path) -> None:
    """§22: the process is another client of the same local installation.

    A guard on the fixture rather than on the product: if these tests ever started
    inheriting the developer's environment, every isolation claim in this
    directory would quietly stop meaning anything.
    """
    environment = server_environment(tmp_path)
    assert set(environment) == {
        "PATH",
        "HOME",
        "REPOATLAS_CONFIG",
        "REPOATLAS_DB",
    }
