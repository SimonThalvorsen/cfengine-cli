import argparse
import os
import sys
import traceback
import pathlib
import subprocess

from cf_remote import log
from cf_remote.main import resolve_hosts
from cf_remote.utils import strip_user, CFRExitError
from cfengine_cli.cfengine_wrapper import cfengine_commands
from cfengine_cli.cfengine_wrapper.arg_parse import parse_wrapper_args
from cfengine_cli.version import cfengine_cli_version_string
from cfengine_cli import commands
from cfengine_cli.utils import UserError
from cf_remote.commands import (
    spawn,
    list_boxes,
    list_platforms,
    init_cloud_config,
    install,
    uninstall,
)
from cf_remote.spawn import CFRUserError, Providers
from cf_remote.validate import (
    validate_edition_args,
    validate_install_args,
    validate_uninstall_args,
    validate_spawn_args,
    validate_deploy_args,
    validate_destroy_args,
)
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
    parse_wrapper_args(
        subp
    )  # The flags for run/report/spawn/destroy/...all wrapper functions

    subp.add_parser("help", help="Print help information")
    subp.add_parser(
        "version",
        help="Print the version string",
    )
    fmt = subp.add_parser("format", help="Autoformat .json and .cf files")
    fmt.add_argument("files", nargs="*", help="Files to format")
    fmt.add_argument("--line-length", default=80, type=int, help="Maximum line length")
    fmt.add_argument("--check", action="store_true")
    lnt = subp.add_parser(
        "lint",
        help="Look for syntax errors and other simple mistakes",
    )
    lnt.add_argument(
        "--strict",
        type=str,
        default="yes",
        help="Strict mode. Default=yes, checks for undefined promise types, bundles, bodies, functions",
    )
    lnt.add_argument(
        "--syntax-description",
        type=str,
        help="Lint based on a user given syntax description",
    )
    lnt.add_argument("files", nargs="*", help="Files to lint")

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
    parser = dev_subparsers.add_parser("syntax-tree")
    parser.add_argument("file", help="CFEngine policy file to print syntax tree for")
    parser = dev_subparsers.add_parser("generate-release-information")

    parser.add_argument(
        "--omit-download",
        help="Use existing masterfiles instead of downloading in 'cfengine dev generate-release-information'",
        action="store_true",
    )
    parser.add_argument(
        "--check-against-git",
        help="Check whether masterfiles from cfengine.com and github.com match in 'cfengine dev generate-release-information'",
        action="store_true",
    )
    parser.add_argument(
        "--from",
        help="Specify minimum version in 'cfengine dev generate-release-information'",
        dest="minimum_version",
    )

    up_parser = subp.add_parser(
        "up", help="Spawn and install with cf-remote from a yaml config"
    )
    up_parser.add_argument(
        "config", default="config.yaml", nargs="?", help="Path to yaml config"
    )
    up_parser.add_argument(
        "--validate", action="store_true", help="Validate the given config"
    )
    up_parser.add_argument(
        "--reset", action="store_true", help="Create a fresh new environment"
    )
    parser = dev_subparsers.add_parser(
        "generate-changelog",
        description="""Changelog generator for CFEngine repositories.

Auto-detects which repos to include based on the current working directory (core, masterfiles, enterprise)

Enterprise' changelog also reflects changes in mission-portal, nova and buildscripts (dependency-updates)
Core and Masterfiles only reflect themselves""",
        epilog="""Examples:
  - cfengine dev generate-changelog
    on 3.27.x this will check changelog for latest known (e.g. 3.27.1) and update the changelog for 3.27.1 -> 3.27.2

  - cfengine dev generate-changelog -o 3.26.0..3.27.0
    on any branch will print changelog from version 2.26.0 -> 3.27.0 to stdout""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-o",
        "--output",
        action="store_true",
        default=False,
        help="Write changelog to stdout instead of prepending to CHANGELOG.md",
    )
    parser.add_argument(
        "--show-version",
        action="store_true",
        dest="show_version",
        help="Print the version from .CFVERSION and exit",
    )
    parser.add_argument(
        "git_args",
        nargs="*",
        metavar="GIT_ARG",
        help="Commit range [other optional args], e.g. 3.27.0..origin/3.27.x",
    )

    inip = subp.add_parser(
        "init", help="Initialize a [policy-set, policy-module, promise-type] example."
    )
    init_type = inip.add_mutually_exclusive_group()
    init_type.add_argument(
        "--policy-set",
        help="Initializes a Build project for working on your policy set on top of the default masterfiles",
        action="store_true",
    )
    init_type.add_argument(
        "--promise-type",
        help="Initializes a Build project for working on a new custom promise type in python",
        action="store_true",
    )
    init_type.add_argument(
        "--policy-module",
        help="Initializes a Build project for working on a module for build.cfengine.com (or internal use)",
        action="store_true",
    )
    inip.add_argument(
        "--with-input",
        help="Adds input data to the --policy-module project, for working on a module which takes input.",
        action="store_true",
    )
    inip.add_argument(
        "--non-interactive",
        help="Non-interactive mode (picks the default for all prompts)",
        action="store_true",
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
    if args.command == "save":
        return cfengine_commands.save(hosts=args.hosts, role=args.role, name=args.name)
    if args.command == "init":
        return commands.init(args)
    if args.command == "build":
        return cfengine_commands.build(args.hub, args.non_interactive)
    if args.command == "deploy":
        return cfengine_commands.deploy(args.hub, args.masterfiles, args.non_interactive)
    if args.command == "input":
        return cfengine_commands.cfbs_input(args.module)
    if args.command == "format":
        return commands.format(args.files, args.line_length, args.check)
    if args.command == "lint":
        return commands.lint(
            args.files,
            (args.strict.lower() in ("y", "ye", "yes")),
            args.syntax_description,
        )
    if args.command == "report":
        return cfengine_commands.report(
            target=args.hub,
            run_agent=args.run_agent,
        )
    if args.command == "setup-code":
        return cfengine_commands.setup_code(target=args.hub)
    if args.command == "install":
        if args.trust_keys:
            trust_keys = args.trust_keys.split(",")
        else:
            trust_keys = None

        return install(
            args.hub,
            args.clients,
            package=args.package,
            bootstrap=args.bootstrap,
            hub_package=args.hub_package,
            client_package=args.client_package,
            version=args.version,
            demo=args.demo,
            call_collect=args.call_collect,
            edition=args.edition,
            remote_download=args.remote_download,
            trust_keys=trust_keys,
            insecure=args.insecure,
        )
    elif args.command == "uninstall":
        all_hosts = (args.hosts or []) + (args.hub or []) + (args.clients or [])
        return uninstall(all_hosts, purge=args.purge)
    if args.command == "run":
        return cfengine_commands.run(*args.run_args, target=args.host)
    if args.command == "spawn":
        if args.list_platforms:
            return list_platforms()
        if args.list_boxes:
            return list_boxes()
        if args.init_config:
            return init_cloud_config()
        if args.name and "," in args.name:
            raise UserError("Group --name may not contain commas")
        if args.role and args.role.endswith("s"):
            # role should be singular
            args.role = args.role[:-1]
        if args.provider == "gcp":
            provider = Providers.GCP
        elif args.provider == "aws":
            provider = Providers.AWS
            if args.network:
                raise UserError("--network not supported for AWS")
            if args.no_public_ip:
                raise UserError("--no-public-ip not supported for AWS")
        else:
            assert args.provider == "vagrant"
            provider = Providers.VAGRANT

        if provider != Providers.VAGRANT:
            if args.cpus:
                raise UserError(f"--cpus not supported for {args.provider}")
            if args.sync_folder:
                raise UserError(f"--sync-folder not supported for {args.provider}")
            if args.provision:
                raise UserError(f"--provision not supported for {args.provider}")

        if args.network and (args.network.count("/") != 1):
            raise UserError(
                "Invalid network specified, needs to be in the network/subnet format"
            )

        return spawn(
            args.platform,
            args.count,
            args.role,
            args.name,
            provider=provider,
            size=args.size,
            network=args.network,
            public_ip=not args.no_public_ip,
            extend_group=args.append,
            vagrant_cpus=args.cpus,
            vagrant_sync_folder=args.sync_folder,
            vagrant_provision=args.provision,
        )
    if args.command == "destroy":
        return cfengine_commands.destroy(args.name, del_all=args.all)
    if args.command == "dev":
        return commands.dev(args.dev_command, args)
    if args.command == "profile":
        return commands.profile(args)
    if args.command == "up":
        return commands.up(args)
    if args.command == "show":
        return cfengine_commands.show(args.hosts)
    if args.command == "moduleinfo":
        return cfengine_commands.moduleinfo(args.modules)
    if args.command == "connect":
        return cfengine_commands.connect(args.hosts)
    raise UserError(f"Unknown command: '{args.command}'")


def validate_args(args):
    if args.command == "dev" and args.dev_command is None:
        raise UserError("Missing subcommand - cfengine dev <subcommand>")

    if args.command == "spawn":
        validate_spawn_args(args)

    if args.command == "destroy":
        validate_destroy_args(args)
        if args.all and args.name:
            raise UserError(
                "Only one of '--all' or 'NAME' may be specified for destruction"
            )

    if args.command == "install":
        validate_edition_args(args)

    if "hosts" in args and args.hosts:
        log.debug(f"validate_args, hosts in args, args.hosts='{args.hosts}'")
        args.hosts = resolve_hosts(args.hosts)
    if "clients" in args and args.clients:
        args.clients = resolve_hosts(args.clients)
    if "bootstrap" in args and args.bootstrap:
        args.bootstrap = [
            strip_user(host_info)
            for host_info in resolve_hosts(args.bootstrap, bootstrap_ips=True)
        ]
    if "hub" in args and args.hub:
        log.debug(f"validate_args, hubs in args, args.hub='{args.hub}'")
        args.hub = resolve_hosts(args.hub)

    if args.command == "uninstall":
        validate_uninstall_args(args)

    if args.command == "install":
        validate_install_args(args)

    if args.command == "deploy":
        validate_deploy_args(args)


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
    except (UserError, CFRUserError, CFRExitError) as e:
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
