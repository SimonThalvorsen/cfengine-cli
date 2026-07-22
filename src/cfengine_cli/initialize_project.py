import json
import logging
import os
from typing import Any

from cfbs.validate import validate_config_raise_exceptions, validate_module_name_content
from cfbs.cfbs_config import CFBSConfig
from cfbs.utils import write_json, canonify
from cfengine_cli.utils import UserError
from cfbs.git import git_commit, git_init

GITIGNORE = "out/\n*.tgz\n"


def init_promise_type(name=None, non_interactive=False):
    _require_uninitialized()

    name = name or _prompt_for_name(non_interactive, validate=False)
    module_name = canonify(name).lower()

    files = {
        os.path.join(module_name, "main.cf"): _main_cf_custom_promise_type(
            name, module_name
        ),
        os.path.join(module_name, f"{module_name}.py"): _python_custom_promise_type(
            name
        ),
        ".gitignore": GITIGNORE,
    }

    module = _module_definition(module_name, False)
    module["steps"] = [
        "copy main.cf modules/promises/main.cf",
        f"copy {module_name}.py modules/promises/{module_name}.py",
        "policy_files modules/promises/main.cf",
    ]

    config = _scaffold(
        name=name,
        description=f"Project for developing the '{name}' promise-type.",
        project_type="policy-set",
        provides={module_name: module},
        files=files,
    )

    _add_to_build(config, "masterfiles")
    _add_to_build(config, "library-for-promise-types-in-python", level=logging.warning)

    _add_local_module(module_name, module, display_name=name)

    config.save()
    git_commit(
        f"Initialized a new CFEngine Build project for promise type '{name}'",
        scope=["cfbs.json"] + sorted(files),
    )
    return 0


def init_policy_module(name=None, with_input=False, non_interactive=False):
    _require_uninitialized()

    name = name or _prompt_for_name(non_interactive)
    validate_module_name_content(name)
    # Module name, directory and CFEngine namespace. `name` as the user typed
    # it is kept for prose.
    module_name = canonify(name)

    files = {
        os.path.join(module_name, "main.cf"): (
            _main_cf_with_input(module_name)
            if with_input
            else _main_cf(name, module_name)
        ),
        os.path.join(module_name, "README.md"): _module_readme(
            name, module_name, with_input
        ),
        ".gitignore": GITIGNORE,
    }
    if with_input:
        # Pre-fill
        files[os.path.join(module_name, "input.json")] = _example_input_json(
            module_name
        )

    module = _module_definition(module_name, with_input)

    config = _scaffold(
        name=name,
        description=f"Project for developing the '{name}' policy module.",
        project_type="module",
        provides={module_name: module},
        files=files,
    )

    _add_to_build(config, "masterfiles")

    config.save()
    git_commit(
        f"Initialized a new CFEngine Build project for module '{name}'",
        scope=["cfbs.json"] + sorted(files),
    )

    _add_local_module(module_name, module, display_name=name)

    _print_next_steps(name, module_name, with_input)
    return 0


# ---------------------------------------------------------------------------
# Project scaffolding
# ---------------------------------------------------------------------------


def _require_uninitialized():
    if os.path.exists("cfbs.json"):
        raise UserError("Already initialized - look at 'cfbs.json'")


def _scaffold(name, description, project_type, provides, files):
    """Write the project files and cfbs.json, then set up git.

    Returns the validated CFBSConfig instance for the new project.
    """
    for path, content in files.items():
        _write_file(path, content)

    write_json(
        "cfbs.json",
        {
            "name": name,
            "description": description,
            "type": project_type,
            "git": True,
            "provides": provides,
            "build": [],
        },
    )

    git_init()

    config = CFBSConfig.get_instance()
    validate_config_raise_exceptions(config, empty_build_list_ok=True)
    return config


def _add_to_build(config, module_name, level=logging.error):
    if config.add_command([module_name], "cfbs add", None, None).return_code != 0:
        level(
            f"Failed to add `{module_name}` to build. "
            f"Can be added manually using `cfbs add {module_name}` at a later stage"
        )


def _module_definition(module_name, with_input):
    # The module name is canonified, so it doubles as the CFEngine namespace.
    target = f"services/cfbs/modules/{module_name}/main.cf"
    module = {
        "description": "An example policy module.",
        "subdirectory": module_name,
        "steps": [
            f"copy main.cf {target}",
            f"policy_files {target}",
            f"bundles {module_name}:main",
        ],
    }
    if with_input:
        module["steps"].append("input ./input.json def.json")
        module["input"] = _input_spec(module_name)
    return module


def _add_local_module(module_name, module, display_name=None):

    entry: dict[str, Any] = {"name": f"./{module_name}/"}
    entry.update({k: v for k, v in module.items() if k != "subdirectory"})
    entry["description"] = (
        f"Local copy of '{display_name or module_name}', for building and testing."
    )
    entry["tags"] = ["local"]
    entry["added_by"] = "cfbs add"

    config = CFBSConfig.get_instance()
    config["build"].append(entry)
    config.save()

    git_commit(f"Added module './{module_name}/'", scope=["cfbs.json"])


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _main_cf_custom_promise_type(name, canonified):
    return f"""promise agent {canonified}
{{
  path => "/var/cfengine/modules/promises/{canonified}.py";
  interpreter => "/usr/bin/python3";
}}

bundle agent __main__
{{
  {canonified}:
    "promiser_name" wanted_attribute => "attribute_value";

  reports:
    "Hello from '{name}'";
}}
"""


def _python_custom_promise_type(name):
    return f"""import os
from cfengine_module_library import PromiseModule, ValidationError, Result


class {name}PromiseTypeModule(PromiseModule):
    def __init__(self):
        super().__init__("{name}_promise_module", "0.0.1")

    def validate_promise(self, promiser, attributes, metadata):
        if not promiser == "promiser_name":
            raise ValidationError(f"`{{promiser}}' does not match 'promiser_name'")
        if "wanted_attribute" not in attributes:
            raise ValidationError(f"Attribute 'wanted_attribute' is required")

    def evaluate_promise(self, promiser, attributes, metadata):
        return Result.KEPT


if __name__ == "__main__":
    {name}PromiseTypeModule().start()
"""


def _main_cf(name, namespace):
    return f"""body file control
{{
  namespace => "{namespace}";
}}

bundle agent main
{{
  vars:
    "message" string => "Hello from the '{name}' module";

  reports:
    "$(message)";
}}

body file control
{{
  namespace => "default";
}}

bundle agent __main__
{{
  methods:
    "{namespace}:main";
}}
"""


def _main_cf_with_input(namespace):
    return f"""body file control
{{
  namespace => "{namespace}";
}}

bundle agent main
{{
  vars:
    "keys" slist => getindices("list_variable_name");

  reports:
    "$(variable_name): $(list_variable_name[$(keys)])";
}}

body file control
{{
  namespace => "default";
}}

bundle agent __main__
{{
  methods:
    "{namespace}:main";
}}
"""


def _input_spec(namespace):
    return [
        {
            "type": "string",
            "variable": "variable_name",
            "namespace": namespace,
            "bundle": "main",
            "label": "Variable name",
            "question": "What variable should this module use in policy?",
        },
        {
            "type": "list",
            "variable": "list_variable_name",
            "namespace": namespace,
            "bundle": "main",
            "label": "Name of list-variable",
            "subtype": [
                {
                    "key": "key1",
                    "type": "string",
                    "label": "Key1-label",
                    "question": "Short description",
                    "default": "default-value",
                },
                {
                    "key": "key2",
                    "type": "string",
                    "label": "Key2-label",
                    "question": "Short description",
                    "default": "any",
                },
            ],
            "while": "Do you want to specify more inputs?",
        },
    ]


def _example_input_json(namespace="example"):
    spec = _input_spec(namespace)
    spec[0]["response"] = "Example string"
    spec[1]["response"] = [
        {"key1": "Value1", "key2": "Value2"},
    ]
    return json.dumps(spec, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prompt_for_name(non_interactive, validate=True):
    default = canonify(os.path.basename(os.getcwd()))
    if non_interactive:
        return default
    name = input(f"Name of module [{default}]: ").strip()
    name = name if name else default
    if validate:
        validate_module_name_content(name)
    return name


def _write_file(path, content):
    if os.path.exists(path):
        raise UserError(f"Refusing to overwrite existing file '{path}'")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _module_readme(name, module_name, with_input):
    input_section = (
        f"""
## Module input

This module accepts input, declared under `"input"` in `cfbs.json`. `input.json`
ships with example responses so `cfbs build` works out of the box; replace them:

    cfbs input {module_name}

Responses are converted to augments and merged into `out/masterfiles/def.json`
by the `input ./input.json def.json` build step. Keep `namespace` and `bundle`
in the input spec matching the namespace and bundle in `main.cf`, or the
variables won't resolve.
"""
        if with_input
        else ""
    )
    return f"""# {name}

The module is named `{module_name}` in `cfbs.json` - module names must be
lowercase - while `{name}` is used where it is only read by humans.

## Files

- `main.cf` - the policy. This is where you do your work.
- `../cfbs.json` - `provides` defines the module for consumers
  (`cfbs add <your-repo-url>`); `build` is a local policy set for testing it.

## Try it

    cfbs build
    sudo cfbs install     # on a hub
{input_section}
## Before publishing

1. Update the descriptions in `cfbs.json`.
2. Add `repo` and `by` URLs inside `provides`.
3. Test the consumer path from a scratch directory:
   `cfbs init && cfbs add <your-repo-url>`

## Renaming

Update the `provides` key, the local module's `name` and `steps` in `build`,
`subdirectory`, the directory itself, and the `{module_name}:main` references.
"""


def _print_next_steps(name, module_name, with_input):
    print("")
    print(f"Initialized a project for developing the policy module '{name}'")
    print("")
    print("To build and test a policy set with your module:")
    print("  `cfbs build` and `cf-remote deploy`")
    print("   or `cfengine build`")
    if with_input:
        print("")
        print("To change the module's input:")
        print(f"  cfbs input {module_name}")
    print("")
    print(f"See {module_name}/README.md for what to edit before publishing.")
