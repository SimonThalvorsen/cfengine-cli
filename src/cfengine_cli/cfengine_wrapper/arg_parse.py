import argparse


def parse_wrapper_args(subp: argparse._SubParsersAction):
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

    subp.add_parser("build", help="Build a policy set from a CFEngine Build project\n\
A wrapper arount the cfbs `build`-function.")
    sp = subp.add_parser("deploy", help="Deploy policy-set (masterfiles) to hub\n\
A wrapper around the cf-remote `deploy`-function with some added niceties.")
    sp.add_argument("--hub", help="Hub(s) to deploy to", type=str)
    sp.add_argument(
        "masterfiles",
        help="Policy-set location (tarball URL or local path to tarball / directory)",
        type=str,
        nargs="?",
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
    # install_parser._option_string_actions.get("--version").help = "absdfsf"
    # TODO: Update cf-remote/cfbs to have more modular arg-parsing, then we can import
    # and override any differences? technically illegal since _option_string_actions,
    # but will save ~ 200-1000 loc depending on how much we import into cfengine-cli

    install_parser.add_argument(
        "--edition",
        "-E",
        choices=["community", "enterprise"],
        help="Enterprise or community packages",
        type=str,
    )
    install_parser.add_argument(
        "--package", help="Local path to package or URL to download", type=str
    )
    install_parser.add_argument(
        "--hub-package",
        help="Local path to package or URL to download for --hub",
        type=str,
    )
    install_parser.add_argument(
        "--client-package",
        help="Local path to package or URL to download for --clients",
        type=str,
    )
    install_parser.add_argument(
        "--bootstrap", "-B", help="cf-agent --bootstrap argument", type=str
    )
    install_parser.add_argument(
        "--clients", "-c", help="Where to install client package", type=str
    )
    install_parser.add_argument("--hub", help="Where to install hub package", type=str)
    install_parser.add_argument(
        "--demo",
        help="Use defaults to make demos smoother (NOT secure)",
        action="store_true",
    )
    install_parser.add_argument(
        "--call-collect",
        help="Enable call collect in --demo def.json",
        action="store_true",
    )
    install_parser.add_argument(
        "--remote-download",
        help="Package will be downloaded directly to the target machine",
        action="store_true",
    )
    install_parser.add_argument(
        "--trust-keys",
        help="Comma-separated list of paths to keys hosts should trust"
        + " (implies '--trust-server no' when boostraping)",
        type=str,
    )
    install_parser.add_argument(
        "--insecure",
        help="Ignore mismatching checksums when downloading urls",
        action="store_true",
    )

    uninstall_parser = subp.add_parser(
        "uninstall",
        help="Uninstall CFEngine on the given hosts",
        description="A wrapper around the cf-remote `uninstall` function",
    )
    uninstall_parser.add_argument(
        "--purge", help="Complete uninstallation", action="store_true"
    )
    uninstall_parser.add_argument(
        "--clients", "-c", help="Where to uninstall", type=str
    )
    uninstall_parser.add_argument("--hub", help="Where to uninstall", type=str)
    uninstall_parser.add_argument("--hosts", "-H", help="Where to uninstall", type=str)

    report_parser = subp.add_parser(
        "report",
        help="Run the agent and hub commands necessary to get new reporting data",
    )
    report_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Select which installation to use by name/IP (e.g. 'local' or '192.168.56.90'). "
        "If omitted and multiple installations of cf-agent+cf-hub are found, you'll be prompted.",
    )

    run_parser = subp.add_parser(
        "run",
        description="Run the CFEngine agent, fetching, evaluating, and enforcing policy.\n\
A wrapper around the cf-remote `run`-function with some added niceties",
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

    sp = subp.add_parser(
        "spawn",
        help="Spawn hosts in the clouds",
        description="A wrapper around the cf-remote `spawn`-function",
    )
    sp.add_argument(
        "--list-platforms", help="List supported platforms", action="store_true"
    )
    sp.add_argument(
        "--list-boxes", help="List installed vagrant boxes", action="store_true"
    )
    sp.add_argument(
        "--init-config",
        help="Initialize configuration file for spawn functionality",
        action="store_true",
    )
    sp.add_argument("--platform", help="Platform or vagrant box to use", type=str)
    sp.add_argument("--count", default=1, help="How many hosts to spawn", type=int)
    sp.add_argument(
        "--role", help="Role of the hosts", choices=["hub", "hubs", "client", "clients"]
    )
    sp.add_argument(
        "--name", help="Name of the group of hosts (can be used in other commands)"
    )
    sp.add_argument(
        "--append",
        help="Append the new VMs to a pre-existing group",
        action="store_true",
    )
    sp.add_argument(
        "--provider",
        help="VM provider",
        type=str,
        default="aws",
        choices=["aws", "gcp", "vagrant"],
    )
    sp.add_argument("--cpus", help="Number of CPUs of the vagrant instances", type=int)
    sp.add_argument(
        "--sync-folder",
        help="Root folder of synchronized folders of vagrant instance",
        type=str,
    )
    sp.add_argument(
        "--provision",
        help="full path to provision shell script for Vagrant VM",
        type=str,
    )
    sp.add_argument("--size", help="Size/type of the instances", type=str)
    sp.add_argument(
        "--network", help="network/subnet to assign the VMs to (GCP only)", type=str
    )
    sp.add_argument(
        "--no-public-ip",
        help="No public IP needed (GCP only; WARNING: The VMs will only be accessible"
        + " from some other VM in the same cloud/network!)",
        action="store_true",
    )

    dp = subp.add_parser(
        "destroy",
        help="Destroy hosts spawned in the clouds",
        description="A wrapper around the cf-remote `destroy`-function",
    )
    dp.add_argument(
        "--all", help="Destroy all hosts spawned in the clouds", action="store_true"
    )
    dp.add_argument("name", help="Name of the group of hosts to destroy", nargs="?")

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
