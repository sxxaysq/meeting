"""The CLI accepts only native sources and preserves replay semantics."""
import json
import sys

import pytest

from src import cli
from src.llm_client import ScriptedLifecycleClient
from src.models import InputContractError
from src.repository import TaskRepository
from .helpers import item


def test_cli_native_processing_and_replay(monkeypatch, tmp_path, capsys):
    database = tmp_path / "tasks.sqlite"
    departments = tmp_path / "departments.json"
    departments.write_text(json.dumps([{"department_id": "D1", "name": "Example department",
                                       "route": "test://D1", "aliases": []}]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["m6", "init-db", "--database", str(database),
                                      "--departments", str(departments)])
    cli.main()
    capsys.readouterr()
    source = tmp_path / "native.json"
    source.write_text(json.dumps({"items": [item(department="Example department")],
                                  "source_document_id": "cli-native"}), encoding="utf-8")
    clients = []
    class CountingClient(ScriptedLifecycleClient):
        def __init__(self, responses):
            super().__init__(responses)
            self.calls = 0

        def decide(self, *args, **kwargs):
            self.calls += 1
            return super().decide(*args, **kwargs)

    def scripted_client(**kwargs):
        client = CountingClient([{"decision": "CREATE", "target_index": None,
            "reason": "New goal", "evidence": None, "fields": [], "scope": "same_task"}])
        clients.append(client)
        return client
    monkeypatch.setattr(cli, "OpenAICompatibleLifecycleClient", scripted_client)
    command = ["m6", "process", str(source), "--database", str(database),
               "--output", str(tmp_path / "result.json")]
    monkeypatch.setattr(sys, "argv", command)
    cli.main()
    first = json.loads(capsys.readouterr().out)
    assert first["action_counts"] == {"CREATE": 1}
    cli.main()
    replay = json.loads(capsys.readouterr().out)
    assert replay["execution_status_counts"] == {"DUPLICATE": 1}
    assert len(TaskRepository(database).list_tasks()) == 1
    assert clients[0].calls == 1 and clients[1].calls == 0


def test_cli_rejects_intermediate_metadata_without_model_call(monkeypatch, tmp_path):
    def forbidden_client_call(**kwargs):
        return ScriptedLifecycleClient([])
    monkeypatch.setattr(cli, "OpenAICompatibleLifecycleClient", forbidden_client_call)
    source = tmp_path / "unsupported.json"
    source.write_text(json.dumps({"items": [item()], "validation": {"status": "PASS"},
                                  "source_document_id": "unsupported"}), encoding="utf-8")
    database = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(sys, "argv", ["m6", "process", str(source), "--database", str(database),
                                      "--output", str(tmp_path / "result.json")])
    with pytest.raises(InputContractError):
        cli.main()
    assert TaskRepository(database).list_tasks() == []
