import pytest

from cfengine_cli.cfengine_wrapper import cfengine_utils

# ---------------------------------------------------------------------------
# Host selection
# ---------------------------------------------------------------------------


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
