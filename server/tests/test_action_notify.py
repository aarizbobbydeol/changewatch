"""The GitHub Action script reads a real git repository: changed lines, exclusions, CODEOWNERS."""
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

ACTION = Path(__file__).resolve().parent.parent.parent / "action" / "notify.py"


def _load():
    spec = importlib.util.spec_from_file_location("cw_action_notify", ACTION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_action_sends_changed_lines_codeowners_and_respects_excludes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / ".github").mkdir()
    (repo / "secrets").mkdir()
    (repo / "infra").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@acme.example")
    _git(repo, "config", "user.name", "Dev")
    billing = repo / "app" / "billing.py"
    billing.write_text("\n".join(f"line {i}" for i in range(1, 50)) + "\n")
    (repo / ".github" / "CODEOWNERS").write_text("app/billing.py @acme/payments\n")
    (repo / "secrets" / "prod.key").write_text("A=1\n")
    (repo / "infra" / "main.tf").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    first = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    lines = billing.read_text().splitlines()
    lines[41] = "line 42 changed"
    billing.write_text("\n".join(lines) + "\n")
    (repo / "secrets" / "prod.key").write_text("A=2\n")
    (repo / "infra" / "main.tf").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "Change 42")

    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"deploy_id": 7}'

    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data)
        sent["key"] = req.headers.get("X-api-key")
        return Resp()

    monkeypatch.chdir(repo)
    for k, v in {"CW_URL": "https://cw.test", "CW_KEY": "cw_ingest_x", "CW_MODE": "full", "CW_EXCLUDE": "infra/**",
                 "CW_BEFORE": first, "CW_SHA": ""}.items():
        monkeypatch.setenv(k, v)
    mod = _load()
    with patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        assert mod.main() == 0

    body = sent["body"]
    assert [f["path"] for f in body["files"]] == ["app/billing.py"]  # secrets/ and infra/ never sent
    assert body["files"][0]["added_ranges"] == [[42, 42]]
    assert "line 42 changed" in body["files"][0]["content"]
    assert body["codeowners"] == "app/billing.py @acme/payments\n"
    assert body["author"] == "dev@acme.example"
    assert "prod.key" not in body["diff"] and "main.tf" not in body["diff"]
    assert sent["key"] == "cw_ingest_x"


@pytest.mark.parametrize("url,allowed", [
    ("http://203.0.113.10:8000", False),        # the key and the diff would cross the internet in clear text
    ("http://localhost:8000", True),
    ("https://changewatch.example.com", True),
])
def test_action_refuses_plain_http_to_other_hosts(monkeypatch, capsys, url, allowed):
    monkeypatch.setenv("CW_URL", url)
    monkeypatch.setenv("CW_KEY", "cw_ingest_x")
    monkeypatch.delenv("CW_ALLOW_INSECURE", raising=False)
    mod = _load()
    with patch.object(mod, "git", return_value=""), \
         patch.object(mod.urllib.request, "urlopen", side_effect=OSError("no network in tests")) as opened:
        assert mod.main() == 0  # never fails the workflow
    assert opened.called is allowed
    if not allowed:
        assert "must use https://" in capsys.readouterr().out
