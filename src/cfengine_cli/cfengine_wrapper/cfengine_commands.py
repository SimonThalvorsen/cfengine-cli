import os

import cfbs
from cfbs.commands import build_command, info_command, status_command
from cf_remote import log
from cf_remote.commands import deploy as deploy_command, info
from cf_remote.commands import destroy as destroy_command
from cf_remote.commands import save as save_command
from cf_remote.commands import show as show_command
from cf_remote.remote import run_command, transfer_file

from cfengine_cli.utils import UserError
from cfengine_cli.cfengine_wrapper.cfengine_objects import (
    Executable,
    ensure_default_agent_flags,
)
from cfengine_cli.cfengine_wrapper.cfengine_utils import (
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


def _remote_path_for(location: str, file_arg: str) -> str:
    local_home = os.path.expanduser("~")
    remote_home = _remote_home_dir(location)

    if file_arg.startswith(local_home + os.sep):
        rel = file_arg[len(local_home) + 1 :]
        return f"{remote_home}/{rel}"
    if file_arg.startswith("/"):
        return file_arg
    return f"{_DEFAULT_CFENGINE_INPUTS_DIR}/{file_arg}"


def _resolve_file_remote(
    location: str, command: str, file_arg: str
) -> tuple[str, str | None]:

    remote_path = _remote_path_for(location, file_arg)
    exists_in_inputs = _remote_path_exists(location, remote_path)

    if os.path.isfile(file_arg):
        remote_home = _remote_home_dir(location)
        uploaded_path = f"{remote_home}/{os.path.basename(file_arg)}"
        if exists_in_inputs:
            log.warning(
                f"File `{file_arg}` also exists in `{remote_path}`, consider renaming `{file_arg}` in the future."
            )
        log.info(f"Uploading {file_arg} to {location}:{uploaded_path}")
        transfer_file(location, file_arg)
        return _replace_file_token(command, file_arg, uploaded_path), uploaded_path

    if not exists_in_inputs:
        raise UserError(
            f"Could not find '{file_arg}' locally or on {location} (checked {remote_path})."
        )
    return _replace_file_token(command, file_arg, remote_path), None


def _resolve_command_for_agent(
    agent: Executable, command: str
) -> tuple[str, str | None]:
    if agent.name != "cf-agent":
        return command, None

    command = ensure_default_agent_flags(command)
    file_arg = extract_agent_file(command)
    if not file_arg:
        return command, None

    if agent.is_local:
        if "/" in file_arg:
            return command, None  # no ambiguity for a local target
        return _resolve_bare_filename_local(command, file_arg), None

    return _resolve_file_remote(agent.location, command, file_arg)


def _remove_remote_file(location: str, path: str) -> None:
    if run_command(location, f"rm -f {path}", sudo=False) is None:
        log.warning(f"Failed to remove uploaded file '{path}' from {location}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def save(hosts: str, role: str, name: str) -> int:  # TODO: Add to existing group
    return save_command(hosts=hosts, role=role, name=name)


def _refresh_agent(agent: Executable) -> int:
    try:
        return agent.run("-KIf update.cf", "-KI")
    except (Exception, SystemExit) as e:
        log.warning(f"Skipping {agent.label}: {e}")
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
        log.warning(f"Skipping hub {hub.label}: {e}")
        return 1


def report(
    target: str | None = None,
    run_agent: bool = False,
) -> int:
    errors = 0
    hubs, clients = select_report_targets(target)

    hub_agent_failed = {}
    if run_agent:
        for hub in hubs:
            rc = _refresh_agent(hub.agent)
            hub_agent_failed[hub.location] = rc != 0
            if rc != 0:
                log.error(f"Agent run failed on {hub.agent.label}")
                errors += 1

        for agent in clients:
            rc = _refresh_agent(agent)
            if rc != 0:
                log.error(f"Refresh failed on {agent.label})")
                errors += 1

    for hub in hubs:
        if run_agent and hub_agent_failed[hub.location]:
            log.warning(f"Agent run failed for {hub.location}, some data may be stale.")
        client_ips = [client.location.split("@", 1)[1] for client in clients]
        rc = _query_hub_delta(hub.hub, client_ips)
        if rc != 0:
            log.error(f"Hub refresh failed on {hub.label})")
            errors += 1

    if errors > 0:
        log.error(f"Encountered {errors}.")
    return errors


def setup_code(target: str | None = None) -> int:
    hub = require_executable("cf-hub", target)
    return hub.run("--new-setup-code")


def run(*args, target: str | None = None) -> int:
    agent = require_executable("cf-agent", target)
    if not args:
        return agent.run("-KIf update.cf", "-KI")

    resolved = []
    cleanup_paths = []
    for command in args:
        resolved_command, cleanup_path = _resolve_command_for_agent(agent, command)
        resolved.append(resolved_command)
        if cleanup_path:
            cleanup_paths.append(cleanup_path)

    try:
        return agent.run(*resolved)
    finally:
        for path in cleanup_paths:
            _remove_remote_file(agent.location, path)


def destroy(groupname, del_all=False) -> int:
    if del_all:
        return destroy_command(None)
    return destroy_command(groupname)


def build(hub=None, non_interactive=False) -> int:
    rc = build_command()
    if rc != 0:
        return rc
    if prompt_yes_no(
        "Deploy the built policy set now?",
        default=True,
        non_interactive=non_interactive,
    ):
        return deploy(hub, None, non_interactive)
    return 0


def deploy(
    target: str | list[str] | None,
    masterfiles: str | None = None,
    non_interactive: bool = False,
) -> int:
    error = 0
    if isinstance(target, str):
        target = [target]
    hubs = {
        x.location: x
        for h in (target or [])
        for x in [require_executable("cf-agent", h)]
    } or None

    # TODO/WOULD be nice: Deploy without run
    if hubs:
        error = deploy_command(hubs.keys(), masterfiles)
    else:
        return deploy_command(hubs, masterfiles)

    if prompt_yes_no(
        "Run policy set now?", default=True, non_interactive=non_interactive
    ):
        for hub in hubs:
            hubs[hub].run("-KIf update.cf", "-KI")
    return error


def show(target: list[str] | None = None) -> int:
    if target == [] or target is None:
        return show_command(False)
    if isinstance(target, str):
        target = [target]
    return info(target)


def moduleinfo(modules: list[str]) -> int:
    if modules == []:
        try:
            status_command()
        except Exception as e:
            log.error(f"Failed to validate cfbs-status, make sure you are inside a cfbs-project: {e}")
        return -1

    return info_command(modules)
