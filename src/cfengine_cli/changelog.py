"""Changelog generator for CFEngine repositories.

Auto-detects which repos to include based on the current working directory (core, masterfiles, enterprise).

Enterprise's changelog also reflects changes in mission-portal, nova and buildscripts (dependency-updates).
Core and Masterfiles only reflect themselves.

Usage:
  cfengine dev changelog-generator [options] [commit-range]

Examples:
  - cfengine dev generate-changelog
    on 3.27.x this will check changelog for latest known (e.g. 3.27.1) and update the changelog for 3.27.1 -> 3.27.2

  - cfengine dev generate-changelog -o 3.26.0..3.27.0
    on any branch will print changelog from version 3.26.0 -> 3.27.0 to stdout
"""

import os
import re
import subprocess
import logging
import sys
from cfengine_cli.utils import get_upstream_remote

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

JIRA_REGEX = r"(?:Jira:? *)?(?:https?://northerntech\.atlassian\.net/browse/)?((?:CFE|ENT|INF|ARCHIVE|MEN|QA)-[0-9]+)"
JIRA_TITLE_REGEX = r"^(?:CFE|ENT|INF|ARCHIVE|MEN|QA)-[0-9]+"
TRACKER_REGEX = r"\(?(?:Ref:? *)?%s\)?:? *" % JIRA_REGEX

DEP_RE = r"Updated dependency '([^']+)' from version (\S+) to (\S+)"
REVERT_RE = r'^Revert "Updated dependency \'([^\']+)\' from version (\S+) to (\S+)"'
REAPPLY_RE = r'^Reapply "Updated dependency \'([^\']+)\' from version (\S+) to (\S+)"'


def fetch_git_output(path, args):
    return subprocess.run(
        ["git", "-C", f"{path}"] + args,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.splitlines(keepends=True)


REPO_OWNER = "cfengine"


def get_versions(prev, branch, repo, repo_path, git_args, repo_owner=REPO_OWNER):
    """Assumes cfengine dev generate-changelog is run inside cfengine repos only"""
    if git_args:
        return git_args
    else:
        name = os.path.basename(repo)
        remote = get_upstream_remote(name, repo_owner, repo_path)
        if remote is None:
            logging.error(
                "No remote in '{}' points to 'github.com/{}/{}'".format(
                    repo, repo_owner, name
                )
            )
            exit(1)
        return [f"{prev}..{remote}/{branch}"]


# ---------------------------------------------------------------------------
# Fetch and merge dependency upgrades / reverts
# ---------------------------------------------------------------------------
def collect_version_updates(repos, prev, branch, git_args):
    dep_history: dict[str, list[tuple[str, str]]] = {}
    for repo in repos:
        repo_path = os.path.join(os.getcwd(), repo)
        for raw in fetch_git_output(
            repo_path,
            [
                "log",
                "--no-merges",
                "--reverse",
                "--pretty=format:%s",
            ]
            + get_versions(prev, branch, repo, repo_path, git_args),
        ):
            subject = raw.decode().strip()

            m = re.match(REVERT_RE, subject)
            if m:
                dep, frm, to = m.group(1), m.group(2), m.group(3)
                history = dep_history.get(dep, [])
                if history and history[-1] == (frm, to):
                    history.pop()
                continue

            m = re.match(REAPPLY_RE, subject) or re.search(DEP_RE, subject)
            if m:
                dep, frm, to = m.group(1), m.group(2), m.group(3)
                dep_history.setdefault(dep, []).append((frm, to))
                continue

    # Collapse dep chains
    results = []
    for dep, history in dep_history.items():
        if not history:
            continue
        first_from = history[0][0]
        last_to = history[-1][1]
        results.append(
            f"Updated dependency '{dep}' from version {first_from} to {last_to}"
        )

    results.sort()
    return results


# ---------------------------------------------------------------------------
# Git-log parsing
# ---------------------------------------------------------------------------
def parse_sha(raw_sha, entries, sha_to_tracker, linked_shas, repo):
    def add_entry(sha, msg):
        if msg.lower().strip() == "none":
            return
        entries.setdefault(sha, []).append(msg)

    sha = raw_sha.decode().rstrip("\n")
    subject = "".join(
        line.decode()
        for line in fetch_git_output(
            os.path.join(os.getcwd(), repo),
            [
                "log",
                "--format=%B",
                "-n",
                "1",
                sha,
            ],
        )
    )

    for match in re.finditer(TRACKER_REGEX, subject, re.IGNORECASE):
        sha_to_tracker.setdefault(sha, set()).add("".join(match.groups("")))

    commit_stripped = re.sub(TRACKER_REGEX, "", subject, flags=re.IGNORECASE)
    parts = commit_stripped.split("\n", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""

    TOKEN_PATTERNS = [
        r"Changelog:",
        r"Signed-off-by:",
        r"Co-authored-by:",
        r"Ticket:",
        r"\(cherry picked from commit [0-9a-f]+\)",
        r"Cancel-Changelog:\s*[0-9a-f]+",
        r"This reverts commit [0-9a-f]+",
    ]
    token_re = re.compile("|".join(TOKEN_PATTERNS), re.IGNORECASE)

    trailers = {}
    current_token = None
    collected_lines = []

    for line in body.splitlines():
        stripped = line.strip()
        if token_re.match(stripped):
            if current_token:
                trailers[current_token] = "\n".join(collected_lines).strip()

            if re.match(r"Changelog:", stripped, re.IGNORECASE):
                current_token = "Changelog"
                first_val = re.sub(r"Changelog:\s*", "", stripped, flags=re.IGNORECASE)
                collected_lines = [first_val] if first_val else []

            elif m := re.match(
                r"\(cherry picked from commit ([0-9a-f]+)\)", stripped, re.IGNORECASE
            ):
                trailers["CherryPick"] = m and m.group(1)
                current_token = None
                collected_lines = []

            elif m := re.match(
                r"Cancel-Changelog:\s*([0-9a-f]+)", stripped, re.IGNORECASE
            ):
                trailers["Cancel"] = m and m.group(1)
                current_token = None
                collected_lines = []

            elif m := re.match(
                r"This reverts commit ([0-9a-f]+)", stripped, re.IGNORECASE
            ):
                trailers["Cancel"] = m and m.group(1)
                current_token = None
                collected_lines = []

            else:
                current_token = "Other"
                collected_lines = []
        else:
            if current_token:
                collected_lines.append(line)

    if current_token:
        trailers[current_token] = "\n".join(collected_lines).strip()

    body_lines = []
    for line in body.splitlines():
        if token_re.match(line.strip()):
            break
        body_lines.append(line)
    clean_commit_body = "\n".join(body_lines).strip()

    if "Cancel" in trailers:
        target = trailers["Cancel"]
        linked = [target] + linked_shas.get(target, [])
        for lsha in linked:
            linked_shas.pop(lsha, None)
            entries.pop(lsha, None)
        return

    if "CherryPick" in trailers:
        other = trailers["CherryPick"]
        linked_shas.setdefault(sha, []).append(other)
        linked_shas.setdefault(other, []).append(sha)

    if "Changelog" in trailers:
        changelog_val = trailers["Changelog"]
        if re.match(r"Title[ .]*", changelog_val, re.IGNORECASE):
            add_entry(sha, title)
        elif re.match(r"(Commit|Body)[ .]*", changelog_val, re.IGNORECASE):
            add_entry(sha, clean_commit_body)
        elif re.match(r"None[ .]*", changelog_val, re.IGNORECASE):
            pass
        else:
            add_entry(sha, changelog_val)
    elif re.match(JIRA_TITLE_REGEX, title):
        add_entry(sha, title)


def parse_git_log(repos, prev, branch, git_args):
    """Walk git history across repos and return (entry_list, missed_tickets)."""
    entries = {}  # sha -> [msg, ...]
    linked_shas = {}  # sha -> [linked_sha, ...]
    sha_to_tracker = {}  # sha -> set of ticket strings

    for repo in repos:
        repo_path = os.path.join(os.getcwd(), repo)
        for raw_sha in fetch_git_output(
            repo_path,
            [
                "rev-list",
                "--no-merges",
                "--reverse",
            ]
            + get_versions(prev, branch, repo, repo_path, git_args),
        ):
            parse_sha(raw_sha, entries, sha_to_tracker, linked_shas, repo)

    entry_list = []
    missed_tickets = {}

    for sha, msgs in entries.items():
        tracker = ""
        if sha_to_tracker.get(sha):
            jiras = sorted(t.upper() for t in sha_to_tracker[sha])
            tracker = "(" + ", ".join(jiras) + ")"

        for entry in msgs:
            m = re.search(r"[0-9]{4,}", entry)
            if m:
                missed_tickets[sha] = m.group(0)
            entry = entry.strip("\n")
            if tracker:
                sep = (
                    "\n"
                    if (len(entry) - entry.rfind("\n") + len(tracker)) >= 70
                    else " "
                )
                entry += sep + tracker
            entry_list.append(entry)

    return entry_list, missed_tickets


# ---------------------------------------------------------------------------
# Repostuff
# ---------------------------------------------------------------------------
REPO_MAP = {
    "core": [
        "../core",
    ],
    "enterprise": [
        "../enterprise",
        "../nova",
        "../mission-portal",
    ],
    "masterfiles": [
        "../masterfiles",
    ],
    "packaging": [
        "../buildscripts",
    ],
}


def detect_repos():
    curr_dir = os.path.basename(os.path.abspath(os.curdir))
    repos = REPO_MAP.get(curr_dir)
    if repos is None:
        logging.error(
            f" current directory '{curr_dir}' is not a recognised repository "
            "(expected: core, enterprise or masterfiles).",
        )
        exit(1)
    return repos


def get_current_branch():
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()


def remote_is_ahead(repo_path, remote, branch):
    out = subprocess.check_output(
        ["git", "-C", repo_path, "ls-remote", remote, f"refs/heads/{branch}"],
        text=True,
    ).split()
    if not out:
        return False  # branch doesn't exist on the remote
    try:
        local = subprocess.check_output(
            ["git", "-C", repo_path, "rev-parse", f"{remote}/{branch}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return True  # never fetched
    return out[0] != local


def _fetch_prompt():
    # Prompt on stderr so it doesn't end up in the changelog with -o
    answer = None
    while answer not in ("", "y", "n", "yes", "no"):
        print("Fetch? (Y/n): ", end="", file=sys.stderr, flush=True)
        answer = input().strip().lower()
    return answer in ("", "y", "yes")


def fetch_upstream(repos, branch, assume_yes=False, repo_owner=REPO_OWNER):
    """Fetch the upstream remote in each repo where it's behind GitHub,
    asking first unless assume_yes is set."""
    outdated = {}  # repo_path -> remote
    for repo in repos:
        repo_path = os.path.join(os.getcwd(), repo)
        remote = get_upstream_remote(os.path.basename(repo), repo_owner, repo_path)
        if remote is None:
            continue  # reported by get_versions
        if remote_is_ahead(repo_path, remote, branch):
            outdated[repo_path] = remote

    if not outdated:
        return
    for repo_path, remote in outdated.items():
        print(
            f"'{remote}/{branch}' is out of date in {os.path.basename(repo_path)}",
            file=sys.stderr,
        )
    if not assume_yes and not _fetch_prompt():
        return
    for repo_path, remote in outdated.items():
        subprocess.run(["git", "-C", repo_path, "fetch", remote], check=True)


# ---------------------------------------------------------------------------
# Versionstuff
# ---------------------------------------------------------------------------
def read_cfversion():
    try:
        with open(".CFVERSION") as f:
            parts = f.readline().strip().split(".")
            return int(parts[0]), int(parts[1]), int(parts[2])
    except FileNotFoundError:
        logging.error(" .CFVERSION not found in current directory.")
        exit(1)
    except (ValueError, IndexError):
        logging.error(
            " .CFVERSION has unexpected format (expected MAJOR.MINOR.PATCH).",
        )
        exit(1)


def get_next_version(old_version, branch):
    if branch == "HEAD":
        logging.error(
            " Cannot generate changelog whilst in 'detached HEAD' state",
        )
        exit(1)

    version_parts = old_version.split(".")
    if branch == "master":
        version_parts[1] = str(int(version_parts[1]) + 1)
        version_parts[2] = "0"
    else:
        if branch == f"{version_parts[0]}.{version_parts[1]}.x":
            version_parts[2] = str(int(version_parts[2]) + 1)
        else:
            branch_name_parts = branch.split(".")
            version_parts = branch_name_parts[0:2] + ["0"]
    return ".".join(version_parts)


# ---------------------------------------------------------------------------
# Generate changelog
# ---------------------------------------------------------------------------
def generate_changelog_impl(args):
    major, minor, patch = read_cfversion()
    if args.show_version:
        print(f"{major}.{minor}.{patch}")
        return 0

    repos = detect_repos()

    try:
        with open("CHANGELOG.md") as f:
            prev = f.readline().strip(":;# \n")
    except FileNotFoundError as e:
        print(e)
        exit(1)

    assert prev, "Could not read previous version from CHANGELOG.md"

    branch = get_current_branch()
    expected = get_next_version(prev, branch)
    actual = f"{major}.{minor}.{patch}"

    assert actual == expected, (
        f"Version mismatch: CHANGELOG has {prev}, branch '{branch}' expects "
        f"next version to be {expected}, but cfversion says {actual}"
    )

    # packaging changes only included in enterprise
    is_enterprise = os.path.basename(os.path.abspath(os.curdir)) == "enterprise"

    if not args.git_args and not args.no_fetch:
        fetch_upstream(
            repos + (REPO_MAP["packaging"] if is_enterprise else []), branch, args.yes
        )

    entry_list, missed_tickets = parse_git_log(repos, prev, branch, args.git_args)
    entry_list.sort()

    output = ""

    lines = []
    for entry in entry_list:
        entry = "- " + entry
        entry = re.sub(r"\n\n+", "\n", entry)  # collapse blank lines
        entry = entry.replace("\n", "\n  ")  # indent continuations
        lines.append(entry)
    if lines:
        output = (
            f"## {actual}\n"
            if not args.git_args
            else f"## {args.git_args[0].split('..')[-1]}\n"
        )

    if is_enterprise:
        pkg_changes, missed_pkg_tickets = parse_git_log(
            REPO_MAP["packaging"], prev, branch, args.git_args
        )
        missed_tickets.update(missed_pkg_tickets)
        pkg_changes += collect_version_updates(
            REPO_MAP["packaging"], prev, branch, args.git_args
        )
        pkg_changes.sort()
        if pkg_changes:
            lines.append("\n**Packaging changes:**")
        for entry in pkg_changes:
            entry = "- " + entry
            entry = re.sub(r"\n\n+", "\n", entry)  # collapse blank lines
            entry = entry.replace("\n", "\n  ")  # indent continuations
            lines.append(entry)

    if output:
        output += "\n".join(lines)
        output += "\n\n"

    if args.output and output:
        print(output)
    elif output:
        try:
            with open("CHANGELOG.md", "r+") as f:
                old = f.read()
                f.seek(0, 0)
                f.write(output + old)
        except FileNotFoundError as e:
            print(e)
            exit(1)

    for sha, number in missed_tickets.items():
        logging.warning(
            f" *** Commit {sha} had a number `{number}` which may be a missed "
            "ticket reference.",
        )

    return 0
