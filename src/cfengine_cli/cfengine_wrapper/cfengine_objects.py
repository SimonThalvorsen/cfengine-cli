from dataclasses import dataclass
from cf_remote.remote import run_command
import subprocess
import logging
import os


def ensure_default_agent_flags(command: str) -> str:
    """
    cf-agent needs -K (no-lock), -I (inform), and -f (specify
    file) to actually run against a policy file the way people expect.
    If `command` has no flags at all -- e.g. someone just ran
    `cfengine run mypolicy.cf` -- prepend "-KIf" so a bare filename works.

    Deliberately does NOT try to detect and fill in individual missing
    letters (e.g. leaving -f out of "-KI somefile.cf")
    If any flag is present at all, we assume the invocation was deliberate and
    leave it exactly as given.
    """
    tokens = command.split()
    has_flags = any(t.startswith("-") for t in tokens)
    if has_flags:
        return command
    return f"-KIf {command}".strip()


class Executable:
    """
    A single binary (cf-agent or cf-hub) at a known location -- either
    "local" or a remote host identifier ("user@ip"). Knows its own path
    and how to run a command against itself, whether that means a local
    subprocess or an SSH call via cf-remote.
    """

    def __init__(self, name: str, location: str, path: str, aliases=None) -> None:
        self.name = name  # "cf-agent" / "cf-hub"
        self.location = location  # "local" or "user@ip"
        self.path = path  # absolute path to the binary at that location
        self.aliases = (
            aliases or []
        )  # friendly names from cf-remote's saved state, e.g. "hub", "local", "remote"

    @property
    def is_local(self) -> bool:
        return self.location == "local"

    @property
    def label(self) -> str:
        """Best human-facing identifier: a friendly alias if we have one, else the location."""
        if self.aliases:
            return f"{self.aliases[0]} ({self.location})"
        return self.location

    def run(self, *commands) -> int:
        rc = 0
        for command in commands:
            rc = self._run_one(command)
            if rc != 0:
                return rc
        return rc

    def _run_one(self, command: str) -> int:
        if self.is_local:
            args = [self.path] + command.split()
            # cf-agent picks its workdir based on privilege: as root it uses
            # /var/cfengine/inputs (where policy actually lives); as a
            # regular user it falls back to ~/.cfagent/inputs instead,
            # which won't have update.cf. Elevate to match the remote path,
            # which already runs everything via sudo.
            if os.geteuid() != 0:
                args = ["sudo"] + args
            result = subprocess.run(args)
            return result.returncode

        full_command = f"{self.path} {command}"
        logging.warning(f"Executing command {full_command} on {self.location}")
        output = run_command(self.location, full_command, sudo=True)
        if (
            output is None
        ):  # TODO: Test this, I've had some policy failing but returning output instead or error-code (I think)
            # Error already logged in run_command
            return 1
        if output:
            print(output)
        return 0

    def __repr__(self) -> str:
        return f"<Executable {self.name} @ {self.location}>"


@dataclass
class Installation:
    """
    One coherent system that has BOTH cf-agent and cf-hub, so callers
    that need both (e.g. report()) can pick one location and get a
    matched pair, rather than resolving each binary independently.
    """

    location: str
    agent: Executable
    hub: Executable

    @property
    def is_local(self) -> bool:
        return self.location == "local"

    @property
    def aliases(self) -> list:
        # agent and hub are always built from the same alias list for a
        # given host (see _find_all_paired), so either would do here.
        return self.agent.aliases

    @property
    def label(self) -> str:
        if self.aliases:
            return f"{self.aliases[0]} ({self.location})"
        return self.location
