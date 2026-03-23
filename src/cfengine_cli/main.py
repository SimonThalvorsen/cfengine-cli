import argparse
import os
import sys
import traceback
import pathlib
import subprocess

from cf_remote import log
from cfengine_cli.version import cfengine_cli_version_string
from cfengine_cli import commands
from cfengine_cli.utils import UserError
from cfbs.utils import CFBSProgrammerError


def _get_arg_parser():
    ap = argparse.ArgumentParser(
        description="Human-oriented CLI for interacting with CFEngine tools",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    ap.add_argument(
        "--log-level",
        help="Specify level of logging: DEBUG, INFO, WARNING, ERROR, or CRITICAL",
        type=str,
        default="WARNING",
    )
    ap.add_argument(
        "--version",
        "-V",
        help="Print version number",
        action="version",
        version=f"{cfengine_cli_version_string()}",
    )

    command_help_hint = (
        "Commands (use %s COMMAND --help to get more info)"
        % os.path.basename(sys.argv[0])
    )
    subp = ap.add_subparsers(dest="command", title=command_help_hint)

    subp.add_parser("help", help="Print help information")
    subp.add_parser(
        "version",
        help="Print the version string",
    )
    subp.add_parser("build", help="Build a policy set from a CFEngine Build project")
    subp.add_parser("deploy", help="Deploy a built policy set")
    fmt = subp.add_parser("format", help="Autoformat .json and .cf files")
    fmt.add_argument("files", nargs="*", help="Files to format")
    fmt.add_argument("--line-length", default=80, type=int, help="Maximum line length")
    lnt = subp.add_parser(
        "lint",
        help="Look for syntax errors and other simple mistakes",
    )
    lnt.add_argument(
        "--strict",
        type=str,
        default="yes",
        help="Strict mode. Default=yes, checks for custom promisetypes",
    )
    lnt.add_argument("files", nargs="*", help="Files to format")
    subp.add_parser(
        "report",
        help="Run the agent and hub commands necessary to get new reporting data",
    )
    subp.add_parser(
        "run", help="Run the CFEngine agent, fetching, evaluating, and enforcing policy"
    )

    profile_parser = subp.add_parser(
        "profile", help="Parse CFEngine profiling output (cf-agent -Kp)"
    )
    profile_parser.add_argument(
        "profiling_input", help="Path to the profiling input file"
    )
    profile_parser.add_argument("--top", type=int, default=10)
    profile_parser.add_argument("--bundles", action="store_true")
    profile_parser.add_argument("--promises", action="store_true")
    profile_parser.add_argument("--functions", action="store_true")
    profile_parser.add_argument(
        "--flamegraph", type=str, help="Generate input file for ./flamegraph.pl"
    )

    dev_parser = subp.add_parser(
        "dev", help="Utilities intended for developers / maintainers of CFEngine"
    )
    dev_subparsers = dev_parser.add_subparsers(dest="dev_command")
    dev_subparsers.add_parser("update-dependency-tables")
    pdt = dev_subparsers.add_parser("print-dependency-tables")
    pdt.add_argument(
        "versions",
        nargs="+",
        help="Versions to compare (minimum 1 required)",
    )
    parser = dev_subparsers.add_parser("format-docs")
    parser.add_argument("files", nargs="*")
    parser = dev_subparsers.add_parser("lint-docs")
    parser.add_argument("files", nargs="*")
    parser = dev_subparsers.add_parser("generate-release-information")

    parser.add_argument(
        "--omit-download",
        help="Use existing masterfiles instead of downloading in 'cfbs generate-release-information'",
        action="store_true",
    )
    parser.add_argument(
        "--check-against-git",
        help="Check whether masterfiles from cfengine.com and github.com match in 'cfbs generate-release-information'",
        action="store_true",
    )
    parser.add_argument(
        "--from",
        help="Specify minimum version in 'cfbs generate-release-information'",
        dest="minimum_version",
    )

    return ap


def get_args():
    ap = _get_arg_parser()
    args = ap.parse_args()
    return args


def run_command_with_args(args) -> int:
    if not args.command:
        raise UserError("No command specified - try 'cfengine help'")
    if args.command == "help":
        return commands.help()
    if args.command == "version":
        return commands.version()
    # The real commands:
    if args.command == "build":
        return commands.build()
    if args.command == "deploy":
        return commands.deploy()
    if args.command == "format":
        return commands.format(args.files, args.line_length)
    if args.command == "lint":
        return commands.lint(args.files, args.strict.lower() in ("y", "ye", "yes"))
    if args.command == "report":
        return commands.report()
    if args.command == "run":
        return commands.run()
    if args.command == "dev":
        return commands.dev(args.dev_command, args)
    if args.command == "profile":
        return commands.profile(args)
    raise UserError(f"Unknown command: '{args.command}'")


def validate_args(args):
    if args.command == "dev" and args.dev_command is None:
        raise UserError("Missing subcommand - cfengine dev <subcommand>")


def _main():
    args = get_args()
    if args.log_level:
        log.set_level(args.log_level)
    validate_args(args)
    return run_command_with_args(args)


def main():
    if os.getenv("CFBACKTRACE") == "1":
        r = _main()
        return r
    try:
        exit_code = _main()
        assert type(exit_code) is int
        sys.exit(exit_code)
    except UserError as e:
        print(str(e))
        sys.exit(-1)
    # Exceptions below are not expected, print extra info:
    except subprocess.CalledProcessError as e:
        print(f"subprocess command failed: {' '.join(e.cmd)}")
    except AssertionError as e:
        tb = traceback.extract_tb(e.__traceback__)
        frame = tb[-1]
        this_file = pathlib.Path(__file__)
        cfbs_prefix = os.path.abspath(this_file.parent.parent.resolve())
        filename = os.path.abspath(frame.filename)
        # Opportunistically cut off beginning of path if possible:
        if filename.startswith(cfbs_prefix):
            filename = filename[len(cfbs_prefix) :]
            if filename.startswith("/"):
                filename = filename[1:]
        line = frame.lineno
        # Avoid using frame.colno - it was not available in python 3.5,
        # and even in the latest version, it is not declared in the
        # docstring, so you will get linting warnings;
        # https://github.com/python/cpython/blob/v3.13.5/Lib/traceback.py#L276-L288
        # column = frame.colno
        assertion = frame.line
        explanation = str(e)
        message = "Assertion failed - %s%s (%s:%s)" % (
            assertion,
            (" - " + explanation) if explanation else "",
            filename,
            line,
        )
        print("Error: " + message)
    except CFBSProgrammerError as e:
        print("Error: " + str(e))
    print(
        "       This is an unexpected error indicating a bug, please create a ticket at:"
    )
    print("       https://northerntech.atlassian.net/")
    print(
        "       (Rerun with CFBACKTRACE=1 in front of your command to show the full backtrace)"
    )

    # TODO: Handle other exceptions
    return 1


if __name__ == "__main__":
    main()
