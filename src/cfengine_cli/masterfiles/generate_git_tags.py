import re
import os
import subprocess
from collections import OrderedDict
from cfbs.utils import (
    write_json,
)

CACHE_DIR = "cache/git/github.com"
TARGET_DIR = "git/github.com"

TAG_REGEX = re.compile(r"v?\d+\.\d+\.\d+(-\d+)?")

REPOS = [
    "cfengine/core",
    "cfengine/enterprise",
    "cfengine/nova",
    "cfengine/mission-portal",
    "cfengine/buildscripts",
    "cfengine/masterfiles",
]


def clone_or_update_repo(repo):
    # Clone repo if not present, else fetch latest version
    repo_path = os.path.join(CACHE_DIR, repo)

    if os.path.isdir(repo_path):
        print(f"Updating {repo}...")
        subprocess.run(["git", "fetch", "--tags"], cwd=repo_path, check=True)

    else:
        print(f"Cloning {repo}...")
        os.makedirs(os.path.dirname(repo_path), exist_ok=True)
        subprocess.run(
            ["git", "clone", f"git@github.com:{repo}.git", repo_path],
            check=True,
        )
    return repo_path


def get_commit_shas_from_tags(repo_path):
    # Returns a mapping of git tag to commit SHA for all version tags in the repo
    output = (
        subprocess.check_output(["git", "show-ref", "--tags"], cwd=repo_path)
        .decode()
        .strip()
    )
    tag_map = {}

    for line in output.splitlines():
        ref = line.split()[1]
        tag = ref.split("refs/tags/")[1]
        if re.fullmatch(TAG_REGEX, tag):
            sha = (
                subprocess.check_output(
                    ["git", "log", "-n", "1", "--format=%H", tag], cwd=repo_path
                )
                .decode()
                .strip()
            )
            tag_map[tag] = sha

    return tag_map


def build_tag_map(repo):
    repo_path = clone_or_update_repo(repo)
    tag_map = get_commit_shas_from_tags(repo_path)

    return sort_git_tags(tag_map)


def write_tag_map(repo, tag_map):
    repo_dir = os.path.join(TARGET_DIR, repo)
    os.makedirs(repo_dir, exist_ok=True)
    write_json(f"{repo_dir}/tags.json", tag_map)


def sort_git_tags(tag_map):
    # Sorts git tags by version descending
    return OrderedDict(
        sorted(
            tag_map.items(),
            reverse=True,
            key=lambda item: tuple(
                int(x) for x in item[0].lstrip("v").replace("-", ".").split(".")
            ),
        )
    )


def generate_git_tags_map():
    os.makedirs(TARGET_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    for repo in REPOS:
        print(f"\nProcessing {repo}...")
        tag_map = build_tag_map(repo)
        write_tag_map(repo, tag_map)
