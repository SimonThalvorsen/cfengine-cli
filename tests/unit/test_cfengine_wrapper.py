import os

import pytest

from cfengine_cli.cfengine_wrapper import cfengine_commands, cfengine_utils
from cfengine_cli.cfengine_wrapper.cfengine_utils import _Id

# ---------------------------------------------------------------------------
# Detecting changes since the last build
# ---------------------------------------------------------------------------


@pytest.fixture
def built_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cfbs.json").write_text("{}")
    (tmp_path / "mymodule").mkdir()
    (tmp_path / "mymodule" / "main.cf").write_text("bundle agent main {}")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "masterfiles.tgz").write_bytes(b"built")
    # Sources older than the build
    for path in ["cfbs.json", "mymodule/main.cf"]:
        os.utime(tmp_path / path, (1000, 1000))
    return tmp_path


def test_changed_since_build_without_tarball(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cfengine_commands._policy_changed_since_build()


def test_changed_since_build_up_to_date(built_project):
    assert not cfengine_commands._policy_changed_since_build()


def test_changed_since_build_after_policy_change(built_project):
    (built_project / "mymodule" / "main.cf").write_text("changed")
    os.utime(built_project / "mymodule" / "main.cf", None)
    os.utime(built_project / "out" / "masterfiles.tgz", (2000, 2000))
    assert cfengine_commands._policy_changed_since_build()


def test_changed_since_build_ignores_out_and_hidden(built_project):
    os.utime(built_project / "out" / "masterfiles.tgz", (2000, 2000))
    (built_project / ".git").mkdir()
    (built_project / ".git" / "index").write_text("")
    (built_project / ".gitignore").write_text("out/\n")
    assert not cfengine_commands._policy_changed_since_build()


# ---------------------------------------------------------------------------
# Host selection
# ---------------------------------------------------------------------------


def test_prompt_choice_defaults_to_first(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    candidates = [_Id("root@1.1.1.1", ["hub"]), _Id("root@2.2.2.2", ["client"])]
    assert cfengine_utils._prompt_choice(candidates, "cf-agent") == candidates[0]


def test_select_without_connecting_lists_hubs_first(monkeypatch):
    known = [
        ("root@2.2.2.2", ["client"]),
        ("root@1.1.1.1", ["hub"]),
    ]
    monkeypatch.setattr(
        cfengine_utils,
        "_known_hosts",
        lambda role=None: iter([k for k in known if role != "hub" or k[1] == ["hub"]]),
    )
    monkeypatch.setattr(cfengine_utils, "_find_local_path", lambda name: None)

    def no_info(host):
        raise AssertionError(f"connected to {host} before it was chosen")

    monkeypatch.setattr(cfengine_utils, "_host_info", no_info)

    shown = []

    def choose_first(candidates, description):
        shown.extend(c.location for c in candidates)
        return candidates[0]

    monkeypatch.setattr(cfengine_utils, "_prompt_choice", choose_first)
    resolved = []
    monkeypatch.setattr(
        cfengine_utils,
        "_resolve",
        lambda name, ident: resolved.append(ident.location) or "agent",
    )

    assert cfengine_utils._select_without_connecting("cf-agent") == "agent"
    assert shown == ["root@1.1.1.1", "root@2.2.2.2"]
    assert resolved == ["root@1.1.1.1"]
