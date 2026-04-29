import os
from cfengine_cli.masterfiles.generate_release_information import (
    generate_release_information_impl,
)
from cfengine_cli.utils import UserError
from cfengine_cli.changelog import generate_changelog_impl
from cfengine_cli.deptool import (
    update_dependency_tables as _update_dependency_tables,
    print_release_dependency_tables,
)
from cfengine_cli.docs import update_docs, check_docs
from cfengine_cli.syntax_tree import syntax_tree


def generate_release_information_command(
    omit_download=False, check=False, min_version=None
):
    generate_release_information_impl(omit_download, check, min_version)
    return 0


def _continue_prompt() -> bool:
    answer = None
    while answer not in ("y", "n", "yes", "no"):
        print("Continue? (Y/n): ", end="")
        answer = input().strip().lower()
    return answer in ("y", "yes")


def _expect_repo(repo):
    cwd = os.getcwd()
    if cwd.endswith(repo):
        return
    print(f"Note: This command is intended to be run in the {repo} repo")
    print(f"      https://github.com/cfengine/{repo}")
    if not _continue_prompt():
        raise UserError(f"Aborted (expected to be in {repo} repo)")


def update_dependency_tables() -> int:
    _expect_repo("buildscripts")
    return _update_dependency_tables()


def print_dependency_tables(args) -> int:
    _expect_repo("buildscripts")
    return print_release_dependency_tables(args.versions)


def format_docs(files) -> int:
    _expect_repo("documentation")
    return update_docs(files)


def lint_docs() -> int:
    _expect_repo("documentation")
    return check_docs()


def generate_release_information(
    omit_download=False, check=False, min_version=None
) -> int:
    _expect_repo("release-information")
    generate_release_information_command(omit_download, check, min_version)
    return 0


def dispatch_dev_subcommand(subcommand, args) -> int:
    if subcommand == "update-dependency-tables":
        return update_dependency_tables()
    if subcommand == "print-dependency-tables":
        return print_dependency_tables(args)
    if subcommand == "format-docs":
        return format_docs(args.files)
    if subcommand == "lint-docs":
        return lint_docs()
    if subcommand == "syntax-tree":
        return syntax_tree(args.file)
    if subcommand == "generate-release-information":
        return generate_release_information(
            args.omit_download, args.check_against_git, args.minimum_version
        )
    if subcommand == "generate-changelog":
        return generate_changelog_impl(args)

    raise UserError("Invalid cfengine dev subcommand - " + subcommand)
