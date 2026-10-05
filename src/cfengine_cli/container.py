import glob
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import NamedTuple

from cfengine_cli import paths
from cfengine_cli.report import (
    RunResults,
    run_and_parse,
    print_report,
    run_and_scan_asserts,
    print_assert_totals,
)
from cfengine_cli.utils import UserError

_DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker", "test-agent")
_IMAGE_TAG = "cfengine-cli-test-agent:latest"
_ASSERT_MODULE_DIR = "/var/cfengine/modules/promises"
_PROMISE_AGENT_DECLARATION_RE = re.compile(r"promise\s+agent\s+(\w+)")
_BUNDLE_AGENT_DECLARATION_RE = re.compile(r"bundle\s+agent\s+(\w+)")
_ASSERT_SECTION_RE = re.compile(r"^\s*assert\s*:", re.MULTILINE)


def assert_module_declaration_path() -> str:
    """Path to the `promise agent assert {...}` declaration
    so calling lint on files containing the assert-promise-type will pass"""
    return os.path.join(_DOCKERFILE_DIR, "assert-module", "promise_agent.cf")


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise UserError(
            "`cfengine test` runs the built policy set inside a Docker container -- install Docker to use it."
        )


def _resolve_dockerfile_dir(dockerfile: str | None) -> str:
    """--dockerfile may point at a Dockerfile itself or at the directory
    containing one; returns the directory `docker build` expects. None (the
    default) means the Dockerfile built into cfengine-cli."""
    if dockerfile is None:
        return _DOCKERFILE_DIR
    abs_path = os.path.abspath(dockerfile)
    return os.path.dirname(abs_path) if os.path.isfile(abs_path) else abs_path


def _image_tag_for(dockerfile_dir: str) -> str:
    """The built-in Dockerfile keeps the stable, human-readable tag; any
    other --dockerfile gets its own tag derived from its path, as to not
    conflict with the cached image."""
    if os.path.abspath(dockerfile_dir) == os.path.abspath(_DOCKERFILE_DIR):
        return _IMAGE_TAG
    digest = hashlib.sha256(os.path.abspath(dockerfile_dir).encode()).hexdigest()[:12]
    return f"cfengine-cli-test-agent-custom-{digest}:latest"


def _ensure_image_built(dockerfile_dir: str, image_tag: str, rebuild: bool) -> None:
    cmd = ["docker", "build", "-q", "-t", image_tag]
    if rebuild:
        cmd.append("--no-cache")
    cmd.append(dockerfile_dir)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise UserError("Failed to build the cfengine-test Docker image.")


def _docker_cleanup(*args: str) -> None:
    """Best-effort `docker <args>` for rm/rmi cleanup."""
    subprocess.run(["docker", *args], capture_output=True)


def _run_setup_and_commit(
    mount_args: list, setup_commands: list, image_tag: str
) -> str:
    """Runs setup_commands once in a throwaway container and commits the
    result to an image, so every test starts pre-set-up."""
    container_name = f"cfengine-cli-test-prep-{uuid.uuid4().hex[:12]}"
    script = " && ".join(setup_commands)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--name",
            container_name,
            *mount_args,
            image_tag,
            "sh",
            "-c",
            script,
        ]
    )
    if result.returncode != 0:
        _docker_cleanup("rm", "-f", container_name)
        raise UserError(
            "Failed to set up the test environment (deploy step) -- see output above."
        )

    image_tag = f"cfengine-cli-test-agent-prepped:{uuid.uuid4().hex[:12]}"
    subprocess.run(
        ["docker", "commit", container_name, image_tag], check=True, capture_output=True
    )
    _docker_cleanup("rm", container_name)
    return image_tag


def _remove_image(image_tag: str) -> None:
    _docker_cleanup("rmi", image_tag)


def run_in_container(
    masterfiles_dir: str, dockerfile: str | None = None, rebuild: bool = False
) -> int:
    """
    Runs cf-agent against the given built masterfiles directory inside a
    container. There's no test_*.cf harness here, so there's no per-test
    breakdown -- but if the deployed policy's own promises happen to emit
    [ASSERT] lines (e.g. assert: promises outside the usual test_*.cf
    convention), those are still counted and reported like a test run;
    otherwise this just shows the agent's own output and exit code.

    dockerfile overrides the built-in Dockerfile (e.g. to test against a
    different base distro); rebuild forces a cache-busting rebuild instead
    of reusing whatever's already cached for it.
    """
    require_docker()
    dockerfile_dir = _resolve_dockerfile_dir(dockerfile)
    image_tag = _image_tag_for(dockerfile_dir)
    _ensure_image_built(dockerfile_dir, image_tag, rebuild)

    abs_masterfiles = os.path.abspath(masterfiles_dir)
    cf_agent = paths.bin("cf-agent")
    cmd = [
        "docker",
        "run",
        "--rm",
        *_read_only_mount(abs_masterfiles, "/mnt/masterfiles"),
        image_tag,
        "sh",
        "-c",
        "rm -rf /var/cfengine/inputs "
        "&& rm -rf /var/cfengine/masterfiles "
        "&& cp -r /mnt/masterfiles /var/cfengine/masterfiles "
        f"&& {cf_agent} --bootstrap 127.0.0.1 "
        "--skip-bootstrap-policy-run --skip-bootstrap-service-start "
        f"&& {cf_agent} -Kf /var/cfengine/masterfiles/update.cf "
        f"&& {cf_agent} -KIf /var/cfengine/masterfiles/promises.cf",
    ]
    results = run_and_scan_asserts(cmd)
    if results.passed == 0 and results.failed == 0:
        return results.returncode
    return print_assert_totals(results.passed, results.failed)


def _read_file(path: str) -> str:
    with open(path) as file:
        return file.read()


def _bundle_bodies_declared_in(content: str) -> list:
    """[(name, body), ...] for every `bundle agent <name> { ... }` in
    content, in order, body being the brace-matched text between (and
    including) its own braces."""
    bodies = []
    for match in _BUNDLE_AGENT_DECLARATION_RE.finditer(content):
        name = match.group(1)
        start = content.index("{", match.end())
        depth = 0
        end = start
        for end in range(start, len(content)):
            if content[end] == "{":
                depth += 1
            elif content[end] == "}":
                depth -= 1
                if depth == 0:
                    break
        bodies.append((name, content[start : end + 1]))
    return bodies


def _bundle_names_declared_in(path: str) -> list:
    """Test (reportable) bundles in a file, in order -- a bundle only counts
    as a test if it has its own `assert:` section. A bundle with no
    `assert:` section (e.g. a shared bundle computing some vars) is just
    available to be called via `methods:` from a test bundle, like any other
    bundle in this file's inputs, without being reported as a test of its
    own."""
    names = [
        name
        for name, body in _bundle_bodies_declared_in(_read_file(path))
        if _ASSERT_SECTION_RE.search(body)
    ]
    if not names:
        raise UserError(f"No bundle with an 'assert:' section found in {path}")
    return names


def discover_test_files(tests_dir: str = "tests") -> list:
    search_dir = tests_dir if os.path.isdir(tests_dir) else "."
    return sorted(
        os.path.join(search_dir, fname)
        for fname in os.listdir(search_dir)
        if fname.startswith("test_") and fname.endswith(".cf")
    )


def discover_module_files() -> list:
    return sorted(glob.glob("*/main.cf"))


def _declared_promise_types(cf_file_path: str) -> set:
    return set(_PROMISE_AGENT_DECLARATION_RE.findall(_read_file(cf_file_path)))


def _read_only_mount(host_path: str, container_path: str) -> list:
    return ["-v", f"{host_path}:{container_path}:ro"]


def _expand_directories(paths: list) -> list:
    expanded = []
    for path in paths:
        if not os.path.isdir(path):
            expanded.append(path)
            continue
        for root, dirs, files in os.walk(path, followlinks=True):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            expanded += sorted(
                os.path.join(root, f)
                for f in files
                if f.endswith(".cf") and not f.startswith(".")
            )
    return expanded


class ProjectSetup(NamedTuple):
    mount_args: list
    deploy_commands: list
    post_deploy_commands: list
    init_mount: str | None
    declares_assert: bool


def _project_setup(masterfiles_dir: str | None) -> ProjectSetup:
    """Everything needed to make a cfbs project's masterfiles, custom promise
    types and services/init.cf available in the container; empty when
    there's no masterfiles_dir (a bare, cfbs-less test_*.cf file)."""
    if masterfiles_dir is None:
        return ProjectSetup([], [], [], None, False)

    abs_masterfiles = os.path.abspath(masterfiles_dir)
    deploy_commands = [
        "rm -rf /var/cfengine/masterfiles",
        "cp -r /mnt/project_masterfiles /var/cfengine/masterfiles",
    ]

    post_deploy_commands = []
    if os.path.isdir(os.path.join(abs_masterfiles, "modules")):
        post_deploy_commands.append(
            "cp -r /mnt/project_masterfiles/modules/. /var/cfengine/modules/"
        )

    # Bootstrapping to itself runs the project's own update.cf,
    # This happens once, as part of the shared setup image every test
    # reuses, so it doesn't add per-test cost.
    post_deploy_commands += [
        paths.bin("cf-serverd"),
        "sleep 1",
        f"{paths.bin('cf-agent')} --bootstrap 127.0.0.1",
    ]

    init_mount = None
    declares_assert = False
    project_init_file = os.path.join(abs_masterfiles, "services", "init.cf")
    if os.path.isfile(project_init_file):
        # The prep container's /mnt/project_masterfiles mount is gone by the
        # time per-test containers start from its committed image; this path
        # is what's still there.
        init_mount = "/var/cfengine/masterfiles/services/init.cf"
        declares_assert = "assert" in _declared_promise_types(project_init_file)

    return ProjectSetup(
        mount_args=_read_only_mount(abs_masterfiles, "/mnt/project_masterfiles"),
        deploy_commands=deploy_commands,
        post_deploy_commands=post_deploy_commands,
        init_mount=init_mount,
        declares_assert=declares_assert,
    )


def _run_cf_content(bundle_reports: list, inputs: list) -> str:
    """bundle_reports is this file's [(bundle_name, report_id), ...], one
    pair per bundle agent it declares (see _bundle_names_declared_in);
    bundle_name must match the test file's own declaration, and report_id is
    the unique key everything else is reported under. Each bundle gets its
    own CFTEST-START/DONE pair in the bundlesequence, so several independent
    check bundles in one file still get attributed to their own report row,
    all within this one file's single cf-agent run."""
    inputs_str = ", ".join(f'"{i}"' for i in inputs)
    bundlesequence = []
    bundle_defs = []
    for i, (bundle_name, report_id) in enumerate(bundle_reports):
        start, marker = f"cftest_start_{i}", f"cftest_marker_{i}"
        bundlesequence += [f'"{start}"', f'"{bundle_name}"', f'"{marker}"']
        bundle_defs += [
            f"bundle agent {start}",
            "{",
            "  reports:",
            f'    "[CFTEST-START] {report_id}";',
            "}",
            "",
            f"bundle agent {marker}",
            "{",
            "  reports:",
            f'    "[CFTEST-DONE] {report_id}";',
            "}",
            "",
        ]
    return "\n".join(
        [
            "body common control",
            "{",
            f"  inputs => {{ {inputs_str} }};",
            f'  bundlesequence => {{ {", ".join(bundlesequence)} }};',
            "}",
            "",
        ]
        + bundle_defs
    )


def _inputs_for_test(
    test_mount: str, project: ProjectSetup, library_mounts: list
) -> list:
    inputs = (
        [] if project.declares_assert else [f"{_ASSERT_MODULE_DIR}/promise_agent.cf"]
    )
    if project.init_mount is not None:
        inputs.append(project.init_mount)
    inputs += library_mounts
    inputs.append(test_mount)
    return inputs


def _prepare_test_run(
    runners_dir: str,
    test_file: str,
    bundle_reports: list,
    project: ProjectSetup,
    library_mounts: list,
) -> tuple:
    """Writes this test's run.cf (and fixture, if any) under runners_dir, a
    fresh per-test tempdir. bundle_reports is this file's [(bundle_name,
    report_id), ...] (see _run_cf_content). Returns (mount_args, run_command)."""
    test_mount = f"/mnt/tests/{os.path.basename(test_file)}"
    mount_args = _read_only_mount(test_file, test_mount)

    inputs = _inputs_for_test(test_mount, project, library_mounts)
    with open(os.path.join(runners_dir, "run.cf"), "w") as run_cf:
        run_cf.write(_run_cf_content(bundle_reports, inputs))

    # Fixture is keyed by the test file's name, not its bundle name
    # two files can share a bundle name.
    test_stem = os.path.splitext(os.path.basename(test_file))[0]
    fixture = os.path.join(os.path.dirname(test_file), "fixtures", f"{test_stem}.json")
    if os.path.isfile(fixture):
        shutil.copyfile(fixture, os.path.join(runners_dir, "def.json"))

    run_command = f"{paths.bin('cf-agent')} -KIf /mnt/runners/run.cf"
    return mount_args, run_command


def run_files_in_container(
    files: list,
    masterfiles_dir: str | None = None,
    dockerfile: str | None = None,
    rebuild: bool = False,
) -> int:
    """Runs each test_*.cf file as its own isolated cf-agent run in its own
    throwaway container, parses [ASSERT] PASS/FAIL lines out of the output,
    and prints a pytest-style report, returning 0 only if everything passed.
    A file may declare several bundles; each becomes its own "<file>::<bundle>"
    report row within that one file's single run.

    Every non-test_ file is a shared library added to every test's `inputs`.
    A sibling fixtures/<test file name>.json is auto-loaded as that run's
    `def.json`. masterfiles_dir, if given, is a built cfbs policy set,
    deployed once for every test to share. dockerfile/rebuild are as in
    run_in_container."""
    require_docker()
    dockerfile_dir = _resolve_dockerfile_dir(dockerfile)
    image_tag = _image_tag_for(dockerfile_dir)
    _ensure_image_built(dockerfile_dir, image_tag, rebuild)

    abs_files = [os.path.abspath(f) for f in _expand_directories(files)]
    test_files = [f for f in abs_files if os.path.basename(f).startswith("test_")]
    library_files = [f for f in abs_files if f not in test_files]
    if not test_files:
        raise UserError(
            "No test_*.cf files given to `cfengine test` -- nothing to run. "
            "Pass at least one file whose name starts with 'test_', "
            "e.g. `cfengine test tests/test_foo.cf`."
        )

    # Spaces stripped: report_id round-trips through a "[CFTEST-START] <id>"
    # line matched by \S+ (see report._START_RE/_DONE_RE).
    file_report_ids = [os.path.relpath(f).replace(" ", "_") for f in test_files]
    bundle_reports_by_file = [
        [
            (bundle_name, f"{file_report_id}::{bundle_name}")
            for bundle_name in _bundle_names_declared_in(test_file)
        ]
        for test_file, file_report_id in zip(test_files, file_report_ids)
    ]
    report_ids = [
        report_id
        for bundle_reports in bundle_reports_by_file
        for _bundle_name, report_id in bundle_reports
    ]

    project = _project_setup(masterfiles_dir)
    setup_commands = (
        ["rm -rf /var/cfengine/inputs"]
        + project.deploy_commands
        + project.post_deploy_commands
    )

    library_mount_args = []
    library_mounts = []
    for lib in library_files:
        lib_mount = f"/mnt/lib/{os.path.basename(lib)}"
        library_mount_args += _read_only_mount(lib, lib_mount)
        library_mounts.append(lib_mount)

    prepped_image = _run_setup_and_commit(project.mount_args, setup_commands, image_tag)
    try:
        verdicts = {}
        completed = set()
        marks_by_test = {}

        for test_file, bundle_reports in zip(test_files, bundle_reports_by_file):
            test_names = [report_id for _bundle_name, report_id in bundle_reports]
            with tempfile.TemporaryDirectory(
                prefix="cfengine-test-runners-"
            ) as runners_dir:
                test_mount_args, run_command = _prepare_test_run(
                    runners_dir,
                    test_file,
                    bundle_reports,
                    project,
                    library_mounts,
                )
                mount_args = (
                    library_mount_args
                    + test_mount_args
                    + _read_only_mount(runners_dir, "/mnt/runners")
                )

                one_test_results = run_and_parse(
                    mount_args, run_command, test_names, image=prepped_image
                )

            verdicts.update(one_test_results.verdicts)
            completed.update(one_test_results.completed)
            marks_by_test.update(one_test_results.marks_by_test)
    finally:
        _remove_image(prepped_image)

    return print_report(report_ids, RunResults(verdicts, completed, marks_by_test))
