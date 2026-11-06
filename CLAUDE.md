# CFEngine CLI information for LLMs

## Fix the implementation

In general, when the prompter asks you to fix the implementation, this means that they have adjusted the tests already and they want you to fix the implementation.
Typically you should not touch the tests in this case, unless there is something obviously wrong in them, like a typo.
The first step to identify what is necessary should be to run the tests and see which ones are failing.

## Running tests

In general, the main command to run for testing is:

```bash
make check
```

This will run all the test suites.

## Running python tools

This project uses `uv`.
That means that you should not run `python`, `python3`, `pip`, `pip3` directly.
Instead, run the appropriate uv command to ensure we're using the right python and the right dependencies.

## Pointers for the source code

When fixing issues, these are usually the files to look at:

- The implementation of `cfengine format` is in `src/cfengine_cli/format.py`.
- The implementation of `cfengine lint` is in `src/cfengine_cli/lint.py`.

## Syntax trees

When working on the formatter or the linter, it is often useful to look at the syntax tree of the policy file.
There is a `dev` subcommand for this:

```bash
uv run cfengine dev syntax-tree tests/lint/001_hello_world.cf
```

The command above prints the syntax tree for `tests/lint/001_hello_world.cf` to the terminal (standard output).

## Test suites

As mentioned above, the `make check` command runs all the tests.
We have different suites:

- Unit tests in `tests/unit` test individual python functions.
- Formatting tests in `tests/format` test the formatter (`cfengine format`).
- Linting tests in `tests/lint` test the linter.
- Shell tests in `tests/shell` test various subcommands and the tool as a whole in an end-to-end fashion.
