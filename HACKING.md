# Developing / maintaining the CFEngine CLI

This document aims to have relevant information for people contributing to and maintaining the CFEngine CLI.
It is not necessary for users of the tool to know about these processes.
For general user information, see the [README](./README.md).

## Code formatting

We use automated code formatters to ensure consistent code style / indentation.
Please format Python code with [black](https://pypi.org/project/black/), and YAML and markdown files with [Prettier](https://prettier.io/).
For simplicity's sake, we don't have a custom configuration, we use the tool's defaults.

If your editor does not do this automatically, you can run these tools from the command line:

```bash
make format
```

## Installing from source

For developers working on CFEngine CLI, you can install it globally using pipx:

```bash
make install
```

This is optional — `make check` and `uv run` work without a global install.

## Running commands without installing

You can run commands without installing globally, using `uv`:

```bash
uv run cfengine format
```

## Running tests

Use the makefile command to run all linting and tests:

```bash
make check
```

Running individual test suites:

```bash
uv run pytest
uv run bash tests/run-lint-tests.sh
uv run bash tests/run-format-tests.sh
uv run bash tests/run-shell-tests.sh
```

## Releasing new versions

Releases are [automated using a GH Action](https://github.com/cfengine/cfengine-cli/blob/main/.github/workflows/pypi-publish.yml).
We create tags and releases through the GitHub UI.
Go to the releases section in GitHub:

https://github.com/cfengine/cfengine-cli/releases

Determine the new version number according to SemVer:

- Look at what is the latest released version.
- Increase the first number if you are releasing breaking changes.
- Or, increase the second number if there are new features (but no breaking changes).
- Otherwise, increase the last number (only backwards compatible bugfixes).

Sometimes, it's not 100% clear, so use your best judgement.

Once you know the version number, proceed with making the release:

1. Enter the version number into the **Choose a tag** dropdown.
2. Click the **Create new tag on publish** alternative.
3. Target should say **main**, don't change it.
4. Put exactly the same version number into the **Release title** field.
5. Click **Generate release notes**.
   - In general, it's not necessary to edit the release notes.
   - Sometimes you might want to add a link to a release blog post.
   - Or, if there is a lot of noise, for example changes to docs and tests which don't affect users, you can remove those.
6. Leave the checkboxes below as is (latest release checked, pre-release unchecked).
7. Click **Publish release**.
8. Watch the release happen:
   - In GitHub Actions: https://github.com/cfengine/cfengine-cli/actions
   - And if all is well, the new version should appear on PyPI: https://pypi.org/project/cfengine/

If it does not work, click the failing GitHub Actions and try to understand what went wrong.

### Releasing using GitHub CLI (gh)

The process is virtually identical to using the GUI.
Once you've determined what the version number should be, use the `gh release create`, and answer the prompts in a similar way to above:

```bash
gh release create
```

Go through the prompts:

```
? Choose a tag Create a new tag
? Tag name 0.2.0
? Title (optional) 0.2.0
? Release notes Write using generated notes as template
? Is this a prerelease? No
? Submit? Publish release
https://github.com/cfengine/cfengine-cli/releases/tag/0.2.0
```

Then check that everything went well:

https://github.com/cfengine/cfengine-cli/actions

https://pypi.org/project/cfengine/

## Rotating secrets

The GH Action shown above requires a PyPI token to have access to publish new versions to PyPI.
Sometimes, this needs to be rotated.
[Log in](https://pypi.org/account/login/) to PyPI (need account access) and go to account settings:

https://pypi.org/manage/account/

Delete the old `cfengine-cli` token and generate a new one with the same name and correct scope (only access to `cfengine` project).
Copy the token and paste it into the GitHub Secret named `PYPI_PASSWORD`.
`PYPI_USERNAME` should be there already, you don't have to edit it, it is simply `__token__`.
Don't store the token anywhere else - we generate new tokens if necessary.

## Not implemented yet / TODOs

- `cfengine run`
  - The command could automatically detect that you have CFEngine installed on a remote hub, and run it there instead (using `cf-remote`).
  - Handle when `cf-agent` is not installed, help users install.
  - Prompt / help users do what they meant (i.e. build and deploy and run).
- Missing commands:
  - `cfengine install` - Install CFEngine packages / binaries (Wrapping `cf-remote install`).
