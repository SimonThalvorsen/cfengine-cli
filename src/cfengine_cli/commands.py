import os
import re
import json
import yaml
from cfengine_cli.profile import profile_cfengine, generate_callstack
from cfengine_cli.dev import dispatch_dev_subcommand
from cfengine_cli.lint import lint_args
from cfengine_cli.version import cfengine_cli_version_string
from cfengine_cli.format import format_paths
from cfengine_cli.utils import UserError
from cfengine_cli.up import validate_config, up_do, resolve_templates
from cfengine_cli.initialize_project import init_policy_module, init_promise_type
from cf_remote.paths import cf_remote_dir
from cfbs.commands import init_command
from pydantic import ValidationError


def help() -> int:
    print("Example usage:")
    print("cfengine run")
    return 0


def version() -> int:
    print(cfengine_cli_version_string())
    return 0


def format(names, line_length, check) -> int:
    return format_paths(names, line_length, check)


def _lint(files, strict, syntax_path) -> int:
    if not files:
        return lint_args(["."], strict, syntax_path)
    return lint_args(files, strict, syntax_path)


def lint(files, strict, syntax_path) -> int:
    errors = _lint(files, strict, syntax_path)
    if errors == 0:
        print("Success, no errors found.")
    elif errors == 1:
        print("Failure, 1 error in total.")
    else:
        print(f"Failure, {errors} errors in total.")
    return errors


def dev(subcommand, args) -> int:
    return dispatch_dev_subcommand(subcommand, args)


def init(args):
    if args.with_input and not args.policy_module:
        raise UserError("--with-input can only be used together with --policy-module")

    if args.policy_module:
        rc = init_policy_module(
            name=None,
            with_input=args.with_input,
            non_interactive=args.non_interactive,
        )
        format(names=[], line_length=80, check=False)
        return rc
    if args.promise_type:
        rc = init_promise_type(name=None)
        format(names=[], line_length=80, check=False)
        return rc

    return init_command()  # --policy-set, cfbs init, default


def profile(args) -> int:
    data = None
    with open(args.profiling_input, "r") as f:
        m = re.search(r"\[[.\s\S]*\]", f.read())
        if m is not None:
            data = json.loads(m.group(0))

    if data is not None and any([args.bundles, args.functions, args.promises]):
        profile_cfengine(data, args)

    if args.flamegraph:
        generate_callstack(data, args.flamegraph)

    return 0


def up(args) -> int:
    content = None
    try:
        with open(args.config, "r") as f:
            content = yaml.safe_load(f)
    except yaml.YAMLError:
        raise UserError("'%s' is not valid yaml" % args.config)
    except FileNotFoundError:
        raise UserError("'%s' doesn't exist" % args.config)

    new_group = resolve_templates(content)
    try:
        new_config = validate_config(new_group)
    except ValidationError as v:
        msgs = []
        for err in v.errors():
            msgs.append(
                f"{err['msg']}. Input '{err['input']}' at location '{err['loc']}'"
            )
        raise UserError("\n".join(msgs))

    if args.validate:
        return 0

    old_group = {}
    try:
        if not args.reset:
            with open(os.path.join(cf_remote_dir(), "old_groups.yaml"), "r") as f:
                old_group = yaml.safe_load(f)
        else:
            os.remove(os.path.join(cf_remote_dir(), "old_groups.yaml"))
    except:
        pass
    if old_group != {}:
        try:
            validate_config(old_group)
        except ValidationError as v:
            msgs = []
            for err in v.errors():
                msgs.append(
                    f"{err['msg']}. Input '{err['input']}' at location '{err['loc']}'"
                )
            raise UserError("\n".join(msgs))

    is_ok = up_do(old_group, new_group, new_config)

    if is_ok:
        with open(os.path.join(cf_remote_dir(), "old_groups.yaml"), "w") as f:
            yaml.dump(new_group, f, default_flow_style=False, sort_keys=False)
    return 0
