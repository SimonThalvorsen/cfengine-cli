# CFEngine CLI cheatsheet

Quick reference for `cfengine COMMANDS`
For the full command list, run `cfengine help` or `cfengine COMMAND --help`.

## Managing hosts

```bash
# Save one or more hosts under a group name, for reuse in other commands
cfengine save --hosts 192.168.56.90 --role hub --name myhub

# List all saved host-groups
cfengine show

# Show details about a specific saved host (or group)
cfengine show --host myhub

# Open an interactive SSH session to a saved host
cfengine connect --hosts myhub
```

## Managing cfbs modules

These are thin wrappers around the equivalent `cfbs` subcommands, usable from anywhere inside a cfbs project.

```bash
# Search the build-index for a module
cfengine search promise-type-git

# Add module(s) to the current cfbs project
cfengine add promise-type-git

# Set/update input.json for a module that takes input
cfengine input promise-type-git

# Remove module(s) from the current cfbs project
cfengine remove promise-type-git

# Update the current cfbs project (or specific modules)
cfengine update
cfengine update promise-type-git

# Show status of the current cfbs project
cfengine moduleinfo

# Show info about specific module(s) (does not require being inside a project)
cfengine moduleinfo promise-type-git
```

## Building and deploying policy sets

```bash
# Build the policy set from a cfbs project (equivalent to `cfbs build`)
cfengine build

# build -> deploy -> run on the given hub without extra prompts
cfengine build --hub myhub --non-interactive

# Deploy an already-built policy set to a hub
cfengine deploy --hub myhub
```

## Testing policy with `cfengine test`

Runs `test_*.cf` files (bundles that contain their own `assert:` section) in disposable Docker containers and reports a pytest-style pass/fail summary.

```bash
# Auto-discover and run tests/test_*.cf
cfengine test

# Run specific files or directories instead
cfengine test tests/test_foo.cf
cfengine test tests/

# Inside a cfbs project: builds first, then runs tests in that project's built
# masterfiles (custom promise types, services/init.cf), against a minimal
# generated policy, not the project's real bundlesequence; see --full-policy below
cfengine test

# Test against a custom base image (e.g. a different distro), bypassing the build cache
cfengine test --dockerfile ./my-ubuntu.Dockerfile --rebuild


# Inside a cfbs project: run the tests after a normal policy run
cfengine test --full-policy
```

## Running the CFEngine agent

```bash
# Default: cf-agent -KIf update.cf && cf-agent -KIf
# If there is a local installation, this will be used
cfengine run

# Run a specific policy file (resolved locally, or uploaded if not found remotely)
cfengine run /tmp/some_policy.cf

# Run against a specific saved host, instead of being prompted
cfengine run --host myhub

# Chain multiple commands, executed in sequence
cfengine run /tmp/some_policy.cf /tmp/other_policy.cf
```

## Initializing new projects

```bash
# Build project on top of the default masterfiles (default, same as `cfbs init`)
cfengine init --policy-set

# Initialize an example build project for working on a custom promise type (python)
cfengine init --promise-type

# Initialize an exapmlte module project for build.cfengine.com (or internal use)
cfengine init --policy-module

# Initialize an example policy module that takes input data
cfengine init --policy-module --with-input

# Skip interactive prompts
cfengine init --promise-module --non-interactive
```

## Formatting and linting

```bash
cfengine format
cfengine lint
cfengine lint main.cf
```

## Reporting

```bash
# Refresh reporting data on all known hubs/clients (capped at 25 hosts), optional flag to run agent first.
cfengine report [--run-agent]

# Only refresh a specific hub (and its clients, capped at 25), running the agent first
cfengine report --hub myhub --run-agent
```

---

See [README.md](./README.md) for installation and general usage, and [HACKING.md](./HACKING.md) for
contributing/development info.
