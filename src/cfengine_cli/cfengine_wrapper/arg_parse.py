import argparse

from cf_remote.args import (
    add_save_args,
    add_deploy_args,
    add_install_args,
    add_uninstall_args,
    add_spawn_args,
    add_destroy_args,
    add_connect_args,
)


def parse_wrapper_args(subp: argparse._SubParsersAction):
    update_parser = subp.add_parser(
        "update",
        help="Updates the current cfbs project",
        description="A wrapper around the cfbs `update` function",
    )
    update_parser.add_argument(
        "to_update",
        nargs="*",
        help="Directory of cfbs-project to update",
    )
    remove_parser = subp.add_parser(
        "remove",
        help="Removes the specified module(s) from cfbs project",
        description="A wrapper around the cfbs `remove` function",
    )
    remove_parser.add_argument(
        "module",
        nargs="+",
        help="Module(s) for which to remove",
    )

    add_parser = subp.add_parser(
        "add",
        help="Adds the specified module(s) to cfbs project",
        description="A wrapper around the cfbs `add` function",
    )
    add_parser.add_argument(
        "module",
        nargs="+",
        help="Module(s) for which to add",
    )
    search_parser = subp.add_parser(
        "search",
        help="Searches the build-index for specified module(s)",
        description="A wrapper around the cfbs `search` function",
    )
    search_parser.add_argument(
        "module",
        nargs="+",
        help="Module(s) for which to lookup",
    )

    input_parser = subp.add_parser(
        "input",
        help="Sets/updates input.json for selected module(s)",
        description="A wrapper around the cfbs `input` function",
    )
    input_parser.add_argument(
        "module",
        nargs="+",
        help="Module(s) for which to set input",
    )

    add_connect_args(
        subp.add_parser(
            "connect",
            help="Opens interactive ssh shell",
            description="A wrapper around cf-remote `connect` function",
        )
    )

    moduleinfo_parser = subp.add_parser(
        "moduleinfo",
        help="Shows information about your  cfbs-project or a specific module",
        description="A wrapper around the cfbs `status` function",
    )

    moduleinfo_parser.add_argument(
        "modules",
        nargs="*",
        help="Module(s) for which you would like more info, utilizes cfbs `info` function",
    )

    show_parser = subp.add_parser(
        "show", help="Shows your saved host-groups or info about a specified host"
    )
    show_parser.add_argument(
        "--hosts",
        "--host",
        "-H",
        help="Shows more specific information about specific host(s)",
    )

    add_save_args(
        subp.add_parser(
            "save", help="Save host(s) with a group name to use in other commands"
        )
    )

    sp = subp.add_parser(
        "setup-code", help="Fetches a new setup-code for mission-portal login"
    )
    sp.add_argument(
        "--hub",
        "-H",
        help="Hub from which to fetch new setup-code",
        type=str,
        default=None,
    )

    sp = subp.add_parser(
        "build",
        help="Build a policy set from a CFEngine Build project",
        description="A wrapper around the cf-remote `build`-function with some added niceties",
    )
    sp.add_argument(
        "--non-interactive",
        help="Non-interactive mode (picks the default for all prompts)",
        action="store_true",
    )
    sp.add_argument("--hub", help="Hub(s) to deploy to after building", type=str)

    deploy_parser = subp.add_parser(
        "deploy",
        help="Deploy policy-set (masterfiles) to hub.",
        description="A wrapper around the cf-remote `deploy`-function with some added niceties.",
    )
    add_deploy_args(deploy_parser)
    deploy_parser.add_argument(
        "--non-interactive",
        help="Non-interactive mode (picks the default for all prompts)",
        action="store_true",
    )

    install_parser = subp.add_parser(
        "install",
        help="Install CFEngine on the given hosts",
        description="A wrapper around the cf-remote `install` function",
    )
    install_parser.add_argument(
        "--version",
        "-V",
        help="Specify version",
        type=str,
    )
    add_install_args(install_parser)

    uninstall_parser = subp.add_parser(
        "uninstall",
        help="Uninstall CFEngine on the given hosts",
        description="A wrapper around the cf-remote `uninstall` function",
    )
    add_uninstall_args(uninstall_parser)

    report_parser = subp.add_parser(
        "report",
        help="Refresh reporting data",
    )
    report_parser.add_argument(
        "--run-agent",
        action="store_true",
        help="Runs the agent on the chosen host(s) before collecting report data.",
    )
    report_parser.add_argument(
        "--hub",
        "-H",
        type=str,
        default=None,
        help="Only refresh one hub specified by name/IP (e.g. 'local' or '192.168.56.90') and accompanying clients",
    )

    run_parser = subp.add_parser(
        "run",
        help="Run the CFEngine agent, fetching, evaluating, and enforcing policy.",
        description="A wrapper around the cf-remote `run`-function with some added niceties",
        epilog="""Examples:
  `cfengine run` defaults to use `cf-agent -KIf update.cf && cf-agent -KI`

   Run can also be used directly on a specific file, e.g.
  'cfengine run /tmp/some_policy.cf' or 'cfengine run "-KIf /tmp/some_policy.cf"'
   If no flags are present in the command, then -KIf will be automatically prepended.

   Multiple commands can also be run in sequence, such as:
  'cfengine run /tmp/some_policy.cf /tmp/some_other_policy.cf /tmp/and_another.cf'
   Where all three files will be run in sequence, exiting on first fail
   """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_parser.add_argument(
        "run_args",
        nargs="*",
        help="Command(s) to run with cf-agent",
    )
    run_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Select which installation of cf-agent to use by name/IP (e.g. 'local' or '192.168.56.90'). "
        "If omitted and multiple installations are found, you'll be prompted.",
    )

    spawn_parser = subp.add_parser(
        "spawn",
        help="Spawn hosts in the clouds",
        description="A wrapper around the cf-remote `spawn`-function",
    )
    add_spawn_args(spawn_parser)

    destroy_parser = subp.add_parser(
        "destroy",
        help="Destroy hosts spawned in the clouds",
        description="A wrapper around the cf-remote `destroy`-function",
    )
    add_destroy_args(destroy_parser)

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
