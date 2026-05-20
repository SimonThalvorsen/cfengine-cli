"""Changelog generator for CFEngine repositories.

Auto-detects which repos to include based on the current working directory

Usage:
  changelog-generator [options] [commit-range]

Examples:
  cfengine dev generate-changelog (last known from changelog..master)
  cfengine dev generate-changelog 3.26.0..3.27.0
  cfengine dev generate-changelog --output CHANGELOG.md 3.25.0..HEAD
"""

import os
import re
import subprocess
import sys

# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

JIRA_REGEX = r"(?:Jira:? *)?(?:https?://northerntech\.atlassian\.net/browse/)?((?:CFE|ENT|INF|ARCHIVE|MEN|QA)-[0-9]+)"
JIRA_TITLE_REGEX = r"^(?:CFE|ENT|INF|ARCHIVE|MEN|QA)-[0-9]+"
TRACKER_REGEX = r"\(?(?:Ref:? *)?%s\)?:? *" % JIRA_REGEX

DEP_RE = r"Updated dependency '([^']+)' from version (\S+) to (\S+)"
REVERT_RE = r'^Revert "Updated dependency \'([^\']+)\' from version (\S+) to (\S+)"'
REAPPLY_RE = r'^Reapply "Updated dependency \'([^\']+)\' from version (\S+) to (\S+)"'


# ---------------------------------------------------------------------------
# Fetch and merge depndacy upgrades / reverts
# ---------------------------------------------------------------------------
def collect_package_updates(repos, git_args):
    original_dir = os.getcwd()

    dep_history: dict[str, list[tuple[str, str]]] = {}

    for repo in repos:
        repo_path = os.path.join(original_dir, repo)
        if not os.path.isdir(repo_path):
            print(
                f"Warning: repo path not found, skipping: {repo_path}", file=sys.stderr
            )
            continue

        os.chdir(repo_path)

        proc = subprocess.Popen(
            ["git", "log", "--no-merges", "--reverse", "--pretty=format:%s"] + git_args,
            stdout=subprocess.PIPE,
        )

        for raw in proc.stdout:
            subject = raw.decode().strip()

            # Revert cancels the most recent matching update
            m = re.match(REVERT_RE, subject)
            if m:
                dep, frm, to = m.group(1), m.group(2), m.group(3)
                history = dep_history.get(dep, [])
                if history and history[-1] == (frm, to):
                    history.pop()
                continue

            # Reapply re-adds an update (treat same as a fresh update)
            m = re.match(REAPPLY_RE, subject)
            if m:
                dep, frm, to = m.group(1), m.group(2), m.group(3)
                dep_history.setdefault(dep, []).append((frm, to))
                continue

            # Normal update
            m = re.search(DEP_RE, subject)
            if m:
                dep, frm, to = m.group(1), m.group(2), m.group(3)
                dep_history.setdefault(dep, []).append((frm, to))

        proc.wait()
        os.chdir(original_dir)

    # Collapse chains: first from_ver -> last to_ver
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
# Git-log parser
# ---------------------------------------------------------------------------
def parse_git_log(repos, git_args):
    """Walk git history across repos and return (entry_list, missed_tickets)."""
    entries = {}  # sha -> [msg, ...]
    linked_shas = {}  # sha -> [linked_sha, ...]
    sha_to_tracker = {}  # sha -> set of ticket strings

    def add_entry(sha, msg):
        if msg.lower().strip() == "none":
            return
        entries.setdefault(sha, []).append(msg)

    original_dir = os.getcwd()

    for repo in repos:
        repo_path = os.path.join(original_dir, repo)
        if not os.path.isdir(repo_path):
            print(
                f"Warning: repo path not found, skipping: {repo_path}", file=sys.stderr
            )
            continue

        os.chdir(repo_path)

        sha_proc = subprocess.Popen(
            ["git", "rev-list", "--no-merges", "--reverse"] + git_args,
            stdout=subprocess.PIPE,
        )

        for raw_sha in sha_proc.stdout:
            sha = raw_sha.decode().rstrip("\n")

            blob = subprocess.Popen(
                ["git", "log", "--format=%B", "-n", "1", sha],
                stdout=subprocess.PIPE,
            )

            title_fetched = False
            title = ""
            commit_msg = ""
            log_entry_title = False
            log_entry_commit = False
            log_entry_local = False
            log_entry = ""

            for raw_line in blob.stdout:
                line = raw_line.decode().rstrip("\r\n").replace("`", "'")  # ENT-7979

                if line == "" and log_entry:
                    add_entry(sha, log_entry)
                    log_entry = ""
                    log_entry_local = False

                # Extract and strip tracker references
                for match in re.finditer(TRACKER_REGEX, line, re.IGNORECASE):
                    sha_to_tracker.setdefault(sha, set()).add("".join(match.groups("")))
                    tracker_removed = re.sub(
                        TRACKER_REGEX, "", line, flags=re.IGNORECASE
                    ).strip()
                    if re.match(JIRA_TITLE_REGEX, line) and not title_fetched:
                        log_entry_title = True
                    line = tracker_removed

                if not title_fetched:
                    title = line
                    title_fetched = True
                    continue

                m = re.match("^ *Changelog: *(.*)", line, re.IGNORECASE)
                if m:
                    log_entry_title = False
                    if log_entry:
                        add_entry(sha, log_entry)
                        log_entry = ""
                    log_entry_local = False
                    subject = m.group(1)
                    if re.match(r"^Title[ .]*$", subject, re.IGNORECASE):
                        log_entry = title
                    elif re.match(r"^Commit[ .]*$", subject, re.IGNORECASE):
                        log_entry_commit = True
                    elif re.match(r"^None[ .]*$", subject, re.IGNORECASE):
                        pass
                    else:
                        log_entry_local = True
                        log_entry = subject
                    continue

                # Cancel-Changelog / revert
                for cancel_expr in [
                    r"^ *Cancel-Changelog: *([0-9a-f]+).*",
                    r"^This reverts commit ([0-9a-f]+).*",
                ]:
                    m = re.match(cancel_expr, line, re.IGNORECASE)
                    if m:
                        if log_entry:
                            add_entry(sha, log_entry)
                            log_entry = ""
                        log_entry_local = False
                        target = m.group(1)
                        linked = [target] + linked_shas.get(target, [])
                        for lsha in linked:
                            linked_shas.pop(lsha, None)
                            entries.pop(lsha, None)
                        break

                # Cherry-pick link
                m = re.match(
                    r"^\(cherry picked from commit ([0-9a-f]+)\)", line, re.IGNORECASE
                )
                if m:
                    if log_entry:
                        add_entry(sha, log_entry)
                        log_entry = ""
                    log_entry_local = False
                    other = m.group(1)
                    linked_shas.setdefault(sha, []).append(other)
                    linked_shas.setdefault(other, []).append(sha)
                    continue

                # Skip Signed-off-by
                if re.match(r"^Signed-off-by:.*", line, re.IGNORECASE):
                    continue
                # Skip ticket
                if re.match(r"^ *Ticket:", line, re.IGNORECASE):
                    continue

                if log_entry_local:
                    log_entry += "\n" + line
                else:
                    if commit_msg:
                        commit_msg += "\n"
                    commit_msg += line

            blob.wait()

            if log_entry_title:
                add_entry(sha, title)
            elif log_entry_commit:
                add_entry(sha, commit_msg)
            elif log_entry:
                add_entry(sha, log_entry)

        sha_proc.wait()

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
# Repo detection
# ---------------------------------------------------------------------------
REPO_SETS = {
    "core": [
        "../core",
    ],
    "enterprise": [
        "../enterprise",
        "../nova",
        "../mission-portal",
    ],
    "tmp-test-gh-changelog": [
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
    repos = REPO_SETS.get(curr_dir)
    if repos is None:
        print(
            f"Error: current directory '{curr_dir}' is not a recognised repository "
            "(expected: core, enterprise or masterfiles).",
            file=sys.stderr,
        )
        sys.exit(1)
    return repos


def get_current_branch():
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()


# ---------------------------------------------------------------------------
# Get version
# ---------------------------------------------------------------------------
def read_cfversion():
    try:
        with open(".CFVERSION") as f:
            parts = f.readline().strip().split(".")
            return int(parts[0]), int(parts[1]), int(parts[2])
    except FileNotFoundError:
        print("Error: .CFVERSION not found in current directory.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, IndexError):
        print(
            "Error: .CFVERSION has unexpected format (expected MAJOR.MINOR.PATCH).",
            file=sys.stderr,
        )
        sys.exit(1)


def get_next_version(old_version, branch):
    version_parts = old_version.split(".")
    print(version_parts)
    if branch == "master":
        version_parts[1] = str(int(version_parts[1]) + 1)
        version_parts[2] = "0"
    else:
        if branch == f"{version_parts[0]}.{version_parts[1]}.x":
            version_parts[2] = str(int(version_parts[2]) + 1)
        else:
            branch_name_parts = branch.split(".")
            version_parts = branch_name_parts[0:2] + ["0"]
    print(version_parts)
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
            prev = f.readline().strip("# \n")
    except FileNotFoundError as e:
        print(e)
        sys.exit(1)

    assert prev, "Could not read previous version from CHANGELOG.md"

    branch = get_current_branch()
    expected = get_next_version(prev, branch)
    actual = f"{major}.{minor}.{patch}"

    assert actual == expected, (
        f"Version mismatch: CHANGELOG has {prev}, branch '{branch}' expects "
        f"next version to be {expected}, but cfversion says {actual}"
    )

    if args.git_args:
        versions = args.git_args
    else:
        prev_major, prev_minor, prev_patch = prev.split(".")
        versions = [f"{prev_major}.{prev_minor}.{prev_patch}..HEAD"]

    entry_list, missed_tickets = parse_git_log(repos, versions)
    entry_list.sort()

    output = ""
    has_output = False

    lines = []
    for entry in entry_list:
        entry = "- " + entry
        entry = re.sub(r"\n\n+", "\n", entry)  # collapse blank lines
        entry = entry.replace("\n", "\n  ")  # indent continuations
        lines.append(entry)
    if lines:
        output = f"## {major}.{minor}.{patch}\n"
        has_output = True

    pkg_changes = collect_package_updates(REPO_SETS["packaging"], versions)
    if pkg_changes:
        lines.append("\n**Packaging changes:**")
        has_output = True
    for entry in pkg_changes:
        entry = "- " + entry
        entry = re.sub(r"\n\n+", "\n", entry)  # collapse blank lines
        entry = entry.replace("\n", "\n  ")  # indent continuations
        lines.append(entry)

    if output:
        output += "\n".join(lines)
        output += "\n"

    if args.output and has_output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Changelog written to {args.output}", file=sys.stderr)
    elif has_output:
        sys.stdout.write(output)

    for sha, number in missed_tickets.items():
        print(
            f"*** Commit {sha} had a number `{number}` which may be a missed "
            "ticket reference.",
            file=sys.stderr,
        )

    return 0
