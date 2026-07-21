import os
import logging

from cfbs.commands import build_command
from cf_remote.commands import deploy as deploy_command
from cf_remote.commands import destroy as destroy_command
from cf_remote.remote import run_command, transfer_file

from cfengine_cli.utils import UserError
from cfengine_cli.cfengine_wrapper.cfengine_objects import (
    Executable,
    ensure_default_agent_flags,
)
from cfengine_cli.cfengine_wrapper.cfengine_utils import (
    clients_by_hub_ip,
    extract_agent_file,
    prompt_two_options,
    prompt_yes_no,
    require_executable,
    select_report_targets,
)

_DEFAULT_CFENGINE_INPUTS_DIR = "/var/cfengine/inputs"


# ---------------------------------------------------------------------------
# File-resolution helpers for run()
# ---------------------------------------------------------------------------


def _remote_path_exists(host: str, path: str) -> bool:
    return run_command(host, f"test -f {path}", sudo=False) is not None


def _replace_file_token(command: str, old: str, new: str) -> str:
    tokens = command.split()
    return " ".join(new if t == old else t for t in tokens)


def _resolve_bare_filename_local(command: str, file_arg: str) -> str:
    cwd_path = os.path.join(os.getcwd(), file_arg)
    inputs_path = os.path.join(_DEFAULT_CFENGINE_INPUTS_DIR, file_arg)
    cwd_exists = os.path.isfile(cwd_path)
    inputs_exists = os.path.isfile(inputs_path)

    if not cwd_exists and not inputs_exists:
        raise UserError(
            f"Could not find '{file_arg}' in the current directory or in "
            f"{_DEFAULT_CFENGINE_INPUTS_DIR}."
        )

    if cwd_exists and inputs_exists:
        choice = prompt_two_options(
            f"'{file_arg}' exists both in the current directory and in "
            f"{_DEFAULT_CFENGINE_INPUTS_DIR}.",
            f"the copy in the current directory ({cwd_path})",
            f"the copy already in {_DEFAULT_CFENGINE_INPUTS_DIR} ({inputs_path})",
        )
        use_cwd = choice != "b"
    else:
        use_cwd = cwd_exists

    return _replace_file_token(command, file_arg, cwd_path) if use_cwd else command


def _remote_home_dir(location: str) -> str:
    user = location.split("@", 1)[0]
    return "/root" if user == "root" else f"/home/{user}"


def _resolve_file_remote(location: str, command: str, file_arg: str) -> str:
    local_exists = os.path.isfile(file_arg)

    local_home = os.path.expanduser("~")
    remote_home = _remote_home_dir(location)

    if file_arg.startswith(local_home + os.sep):
        # e.g. "~/update.cf", shell expands to /home/{user}/update.cf
        rel = file_arg[len(local_home) + 1 :]
        remote_path = f"{remote_home}/{rel}"
    elif file_arg.startswith("/"):
        remote_path = file_arg
    else:
        remote_path = f"{_DEFAULT_CFENGINE_INPUTS_DIR}/{file_arg}"

    remote_exists = _remote_path_exists(location, remote_path)

    if not local_exists and not remote_exists:
        raise UserError(
            f"Could not find '{file_arg}' locally or on {location} (checked {remote_path})."
        )

    if local_exists and remote_exists:
        choice = prompt_two_options(
            f"'{file_arg}' exists both locally and on {location} (at {remote_path}).\nUse the:",
            "local copy (uploads it, possibly overwriting the remote copy)",
            f"copy already on {location}, unchanged",
        )
    elif local_exists:
        choice = "a"
    else:
        choice = "b"

    if choice == "b":
        return _replace_file_token(command, file_arg, remote_path)

    uploaded_path = f"{remote_home}/{os.path.basename(file_arg)}"
    logging.warning(f"Uploading {file_arg} to {location}:{uploaded_path}")
    transfer_file(location, file_arg)
    return _replace_file_token(command, file_arg, uploaded_path)


def _resolve_command_for_agent(agent: Executable, command: str) -> str:
    if agent.name != "cf-agent":
        return command

    command = ensure_default_agent_flags(command)
    file_arg = extract_agent_file(command)
    if not file_arg:
        return command

    if agent.is_local:
        if "/" in file_arg:
            return command  # no ambiguity for a local target
        return _resolve_bare_filename_local(command, file_arg)

    return _resolve_file_remote(agent.location, command, file_arg)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _refresh_agent(agent: Executable) -> int:
    try:
        return agent.run("-KIf update.cf", "-KI")
    except (Exception, SystemExit) as e:
        logging.warning(f"Skipping {agent.label}: {e}")
        return 1


def _query_hub_delta(hub: Executable, client_ips: list[str]) -> int:
    """
    Ask a hub to recompute delta report data for itself and
    for every client bootstrapped to it.
    """
    try:
        queries = ["--query delta -H 127.0.0.1"] + [
            f"--query delta -H {ip}" for ip in client_ips
        ]
        return hub.run(*queries)
    except (Exception, SystemExit) as e:
        logging.warning(f"Skipping hub {hub.label}: {e}")
        return 1


def report(
    target: str | None = None,
    max_hosts: int | None = None,
    refresh_all: bool = False,
    run_agent: bool = False,
) -> int:
    installations, agents = select_report_targets(target, max_hosts, refresh_all)
    hub_client_map = clients_by_hub_ip()

    hub_agent_failed = {}
    if run_agent:
        for installation in installations:
            rc = _refresh_agent(installation.agent)
            hub_agent_failed[installation.location] = rc != 0
            if rc != 0:
                logging.warning(f"Agent run failed on {installation.agent.label}")

        for agent in agents:
            rc = _refresh_agent(agent)
            if rc != 0:
                logging.warning(f"Refresh failed on {agent.label})")

    any_hub_failed = False
    for installation in installations:
        if run_agent and hub_agent_failed[installation.location]:
            # No point querying delta if agent run failed
            any_hub_failed = True
            continue
        hub_ip = (
            None if installation.is_local else installation.location.split("@", 1)[1]
        )
        client_ips = hub_client_map.get(hub_ip, []) if hub_ip else []
        rc = _query_hub_delta(installation.hub, client_ips)
        if rc != 0:
            any_hub_failed = True
            logging.warning(f"Hub refresh failed on {installation.label})")

    return 1 if any_hub_failed else 0


def new_setup_code(target: str | None = None) -> int:
    hub = require_executable("cf-hub", target)
    return hub.run("--new-setup-code")


def run(*args, target: str | None = None) -> int:
    agent = require_executable("cf-agent", target)
    if not args:
        return agent.run("-KIf update.cf", "-KI")
    resolved = [_resolve_command_for_agent(agent, command) for command in args]
    return agent.run(*resolved)


def destroy(groupname, del_all=False) -> int:
    if del_all:
        return destroy_command(None)
    return destroy_command(groupname)


def build() -> int:
    rc = build_command()
    if rc != 0:
        return rc
    if prompt_yes_no("Deploy the built policy set now?", default=True):
        return deploy(None, None)
    return 0


def deploy(target: str | list[str] | None, masterfiles: str | None = None) -> int:
    if isinstance(target, str):
        target = [target]
    hubs = [require_executable("cf-agent", h).location for h in (target or [])] or None
    return deploy_command(hubs, masterfiles)
