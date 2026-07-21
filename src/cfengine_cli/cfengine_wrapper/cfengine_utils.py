import os
import shutil
import logging
import sys

from collections.abc import Iterator
from cfengine_cli.paths import bin
from cfengine_cli.utils import UserError
from cfengine_cli.cfengine_wrapper.cfengine_objects import Executable, Installation
from cf_remote.remote import get_info
from cf_remote.paths import CLOUD_STATE_FPATH
from cf_remote.utils import read_json


def prompt_yes_no(prompt: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        raise UserError(f"{prompt} -- no terminal to confirm.")
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def prompt_two_options(header: str, option_a: str, option_b: str) -> str:
    print(header)
    print(f"  1) {option_a}")
    print(f"  2) {option_b}")
    while True:
        choice = input("Select [1-2]: ").strip()
        if choice == "1":
            return "a"
        if choice == "2":
            return "b"
        print("Invalid selection, try again.")


def extract_agent_file(command: str) -> str | None:
    """
    Best-effort extraction of the filename cf-agent would read as policy
    input, from an already-flagged command string (e.g. "-KIf update.cf").
    Returns None if there's no file argument to check at all (e.g. plain
    "-KI"), in which case there's nothing to resolve.
    """
    tokens = command.split()
    for i, token in enumerate(tokens):
        if token.startswith("-") and not token.startswith("--") and "f" in token:
            if i + 1 < len(tokens):
                return tokens[i + 1]
    return None


def _find_local_path(binary_name: str) -> str | None:
    candidate = bin(binary_name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which(binary_name)


def _known_hosts(role_filter=None) -> Iterator[tuple[str, list[str]]]:
    """
    Yields (host_id, aliases) from cf-remote's saved/spawned VM state --
    host_id is "user@ip" (same source _get_hubs() in cf_remote.commands
    reads from); aliases are the friendly names cf-remote knows this host
    by: the group name it was saved/spawned under (e.g. "hub", "hub2")
    and its per-host key within that group. Optionally filter by
    vm["role"] (e.g. "hub" or "client").
    """
    if not os.path.exists(CLOUD_STATE_FPATH):
        return
    vms_info = read_json(CLOUD_STATE_FPATH)
    if not vms_info:
        return
    for group_name, group in vms_info.items():
        alias_group = group_name.lstrip("@")
        for host_key, vm in group.items():
            if host_key == "meta":
                continue
            if role_filter and vm.get("role") != role_filter:
                continue
            host_id = "{}@{}".format(vm["user"], vm["public_ips"][0])
            aliases = [alias_group]
            if host_key != alias_group:
                aliases.append(host_key)
            yield host_id, aliases


def _find_all(binary_name: str) -> list[Executable]:
    """Every location -- local, plus every matching remote host -- with `binary_name` installed."""
    executables = []

    local_path = _find_local_path(binary_name)
    if local_path:
        executables.append(Executable(binary_name, "local", local_path))

    role_filter = "hub" if binary_name == "cf-hub" else None
    key = "agent" if binary_name == "cf-agent" else "hub"
    for host, aliases in _known_hosts(role_filter=role_filter):
        try:
            data = get_info(host)
        except (Exception, SystemExit) as e:
            """Need to catch SystemExit as cf-remote's get_info() will SystemExit if
            any ssh-connections does not work, for our case we still want to fetch
            the ones that are up in case the user wants to use a different host"""
            logging.warning(f"Skipping {host}: {e}")
            continue
        if not data:
            continue
        binary_path = data.get(key) if key == "agent" else "cf-hub" # band-aid fix, hostinfo does not have hub-executable path
        if binary_path:
            executables.append(
                Executable(binary_name, host, binary_path, aliases=aliases)
            )

    return executables


def _find_all_paired() -> list[Installation]:
    """Every location -- local or remote -- that has BOTH cf-agent and cf-hub."""
    installations = []

    local_agent_path = _find_local_path("cf-agent")
    local_hub_path = _find_local_path("cf-hub")
    if local_agent_path and local_hub_path:
        installations.append(
            Installation(
                location="local",
                agent=Executable("cf-agent", "local", local_agent_path),
                hub=Executable("cf-hub", "local", local_hub_path),
            )
        )

    for host, aliases in _known_hosts(role_filter="hub"):
        try:
            data = get_info(host)
        except (Exception, SystemExit) as e:
            # Same reasoning as _find_all()
            logging.warning(f"Skipping {host}: {e}")
            continue
        if not data:
            continue
        agent_path = data.get("agent")
        # If role is hub, assume hub exists and path resolves correctly
        is_hub = data.get("role") == "hub"
        hub_path = "cf-hub" if is_hub else None
        if agent_path and hub_path:
            installations.append(
                Installation(
                    location=host,
                    agent=Executable("cf-agent", host, agent_path, aliases=aliases),
                    hub=Executable("cf-hub", host, hub_path, aliases=aliases),
                )
            )

    return installations


def _prompt_choice(candidates, description):
    if not sys.stdin.isatty():
        labels = ", ".join(c.label for c in candidates)
        raise UserError(
            f"Multiple installations of {description} found ({labels}) "
            f"and no terminal to prompt on. Specify one with --host."
        )
    print(f"Multiple installations of {description} found:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}) {c.label}")
    while True:
        choice = input(f"Select which to use [1-{len(candidates)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        print("Invalid selection, try again.")


def _exact_match(candidate, target: str) -> bool:
    return target == candidate.location or target in candidate.aliases


def _loose_match(candidate, target: str) -> bool:
    """Substring match, for when nothing matched exactly."""
    if target in candidate.location:
        return True
    return any(target in alias for alias in candidate.aliases)


def _select(candidates, description, target: str | None = None):
    """
    Picks one candidate (an Executable or Installation) out of a list:
    - none found -> UserError
    - `target` given -> match against location OR any alias (exact, or
      substring -- an IP, username, or friendly name like "hub"),
      UserError on no match
    - exactly one -> use it, no prompt
    - multiple, no target -> interactive prompt
    """
    if not candidates:
        raise UserError(
            f"Could not find {description} locally or on any configured remote host."
        )

    if isinstance(target, list):
        if len(target) > 1:
            raise UserError(
                f"Expected a single {description}, but got {len(target)}: "
                f"{', '.join(target)}."
            )
        target = target[0] if target else None

    if target:
        matches = [c for c in candidates if _exact_match(c, target)]
        if not matches:
            matches = [c for c in candidates if _loose_match(c, target)]
        if not matches:
            available = ", ".join(c.label for c in candidates)
            raise UserError(
                f"No installation of {description} matches '{target}'. Available: {available}"
            )
        if len(matches) == 1:
            return matches[0]
        return _prompt_choice(matches, description)

    if len(candidates) == 1:
        return candidates[0]

    return _prompt_choice(candidates, description)


def require_executable(name: str, target: str | None = None) -> Executable:
    chosen = _select(_find_all(name), name, target)
    logging.warning(
        f"Using {'local' if chosen.is_local else 'remote'} installation of {name} ({chosen.label})"
    )
    return chosen


def require_installation(target: str | None = None) -> Installation:
    chosen = _select(_find_all_paired(), "cf-agent + cf-hub", target)
    logging.warning(
        f"Using {'local' if chosen.is_local else 'remote'} installation of cf-agent and cf-hub ({chosen.label})"
    )
    return chosen
