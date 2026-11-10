import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import NamedTuple

from cfbs.commands import build_command
from cfbs.utils import is_cfbs_repo, merge_json, read_json

from cfengine_cli import paths
from cfengine_cli.report import (
    RunResults,
    run_and_parse,
    print_report,
    run_and_scan_asserts,
    print_assert_totals,
)
from cfengine_cli.lint import _parse_policy_file
from cfengine_cli.utils import UserError

_DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker", "test-agent")
_IMAGE_TAG = "cfengine-cli-test-agent:latest"
_ASSERT_MODULE_DIR = "/var/cfengine/modules/promises"
_STDLIB = "/var/cfengine/masterfiles/lib/stdlib.cf"
_PROMISE_AGENT_DECLARATION_RE = re.compile(r"promise\s+agent\s+(\w+)")


def assert_module_declaration_path() -> str:
    """Path to the `promise agent assert {...}` declaration
    so calling lint on files containing the assert-promise-type will pass"""
    return os.path.join(_DOCKERFILE_DIR, "assert-module", "promise_agent.cf")


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise UserError(
            "`cfengine test` runs the built policy set inside a Docker container -- install Docker to use it."
        )


def _resolve_dockerfile(dockerfile: str | None) -> tuple:
    """--dockerfile may point at a Dockerfile itself or at the directory
    containing one; returns (build_context_dir, dockerfile_path) for
    `docker build -f <dockerfile_path> <build_context_dir>`, so a
    non-default name like Dockerfile.rhel is really the one that's built.
    None => use the built-in Dockerfile."""
    if dockerfile is None:
        return _DOCKERFILE_DIR, os.path.join(_DOCKERFILE_DIR, "Dockerfile")
    abs_path = os.path.abspath(dockerfile)
    if os.path.isfile(abs_path):
        return os.path.dirname(abs_path), abs_path
    return abs_path, os.path.join(abs_path, "Dockerfile")


def _image_tag_for(dockerfile_path: str) -> str:
    """The built-in Dockerfile keeps the stable, human-readable tag; any
    other --dockerfile gets its own tag derived from its path, as to not
    conflict with the cached image (or with a sibling Dockerfile.<x> in
    the same directory)."""
    abs_path = os.path.abspath(dockerfile_path)
    if abs_path == os.path.abspath(os.path.join(_DOCKERFILE_DIR, "Dockerfile")):
        return _IMAGE_TAG
    digest = hashlib.sha256(abs_path.encode()).hexdigest()[:12]
    return f"cfengine-cli-test-agent-custom-{digest}:latest"


def _ensure_image_built(
    context_dir: str, dockerfile_path: str, image_tag: str, rebuild: bool
) -> None:
    cmd = ["docker", "build", "-q", "-t", image_tag, "-f", dockerfile_path]
    if rebuild:
        cmd.append("--no-cache")
    cmd.append(context_dir)
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
    commit = subprocess.run(
        ["docker", "commit", container_name, image_tag],
        capture_output=True,
        text=True,
    )
    _docker_cleanup("rm", "-f", container_name)
    if commit.returncode != 0:
        raise UserError(
            f"Failed to commit the prepared test container: {commit.stderr.strip()}"
        )
    return image_tag


def _remove_image(image_tag: str) -> None:
    _docker_cleanup("rmi", image_tag)


def prepare_test_masterfiles(masterfiles: str | None = None) -> tuple:
    """(rc, masterfiles_dir) for `cfengine test`: a cfbs project is built and
    tested against its own out/masterfiles (whatever MPF version its
    cfbs.json pins); outside one, --masterfiles names a built masterfiles
    directory to use instead; otherwise there's none (None)."""
    if is_cfbs_repo():
        if masterfiles is not None:
            raise UserError(
                "--masterfiles is for tests outside a cfbs project -- a cfbs "
                "project is tested against its own build (out/masterfiles), "
                "with the masterfiles version its cfbs.json pins."
            )
        # Build first so any custom promise types / modules this
        # project uses are shipped
        rc = build_command()
        return rc, "out/masterfiles"
    if masterfiles is None:
        return 0, None
    if not os.path.isfile(os.path.join(masterfiles, "promises.cf")):
        raise UserError(
            f"--masterfiles '{masterfiles}' has no promises.cf -- point it at a "
            "built masterfiles directory, e.g. out/masterfiles from `cfbs "
            "build`, or an installed /var/cfengine/masterfiles."
        )
    return 0, masterfiles


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
    context_dir, dockerfile_path = _resolve_dockerfile(dockerfile)
    image_tag = _image_tag_for(dockerfile_path)
    _ensure_image_built(context_dir, dockerfile_path, image_tag, rebuild)

    # promises.cf is run explicitly below, so the bootstrap's own run would
    # only duplicate it.
    project = _project_setup(masterfiles_dir, run_policy_in_setup=False)
    setup_commands = (
        ["rm -rf /var/cfengine/inputs"]
        + project.deploy_commands
        + project.post_deploy_commands
    )
    prepped_image = _run_setup_and_commit(project.mount_args, setup_commands, image_tag)
    try:
        cmd = [
            "docker",
            "run",
            "--rm",
            prepped_image,
            "sh",
            "-c",
            f"{paths.bin('cf-agent')} -KIf /var/cfengine/masterfiles/promises.cf",
        ]
        results = run_and_scan_asserts(cmd)
    finally:
        _remove_image(prepped_image)

    # A failing assert alone doesn't make cf-agent exit non-zero, so a
    # non-zero exit means the agent run itself broke (e.g. aborted partway
    # through) -- that must fail the run even if every assert seen passed.
    # Falling back to failsafe is a broken run too, despite exiting 0.
    if results.fell_back_to_failsafe:
        agent_rc = results.returncode or 1
        agent_problem = (
            "cf-agent ran its built-in failsafe policy instead of the "
            "project's -- the policy failed validation, see the cf-promises "
            "errors above"
        )
    else:
        agent_rc = results.returncode
        agent_problem = (
            f"cf-agent exited with code {agent_rc} -- check output above for "
            "a cf-agent error"
        )

    if results.passed == 0 and results.failed == 0:
        if agent_rc != 0:
            print(agent_problem)
        return agent_rc
    rc = print_assert_totals(
        results.passed, results.failed, errors=1 if agent_rc != 0 else 0
    )
    if agent_rc != 0:
        print(agent_problem)
        return agent_rc
    return rc


def _read_file(path: str) -> str:
    with open(path) as file:
        return file.read()


def _node_text(node) -> str:
    return node.text.decode()


def _file_control_namespace(body_block) -> str | None:
    """The namespace a `body file control` block sets, if any."""
    kinds = {child.type: _node_text(child) for child in body_block.children}
    if (kinds.get("body_block_type"), kinds.get("body_block_name")) != (
        "file",
        "control",
    ):
        return None
    for body in body_block.children:
        if body.type != "body_block_body":
            continue
        for attribute in body.children:
            if attribute.type != "attribute":
                continue
            name, *rest = attribute.children
            if _node_text(name) == "namespace" and rest:
                return _node_text(rest[-1]).strip("\"'")
    return None


def _has_assert_section(bundle_block) -> bool:
    for body in bundle_block.children:
        if body.type != "bundle_block_body":
            continue
        for section in body.children:
            if section.type != "bundle_section":
                continue
            guard = section.children[0] if section.children else None
            if guard is not None and guard.type == "promise_guard":
                if _node_text(guard).rstrip(":").strip() == "assert":
                    return True
    return False


def _bundle_names_declared_in(path: str) -> list:
    """Test (reportable) bundles in a file, in order -- a bundle only counts
    as a test if it has its own `assert:` section. A bundle with no
    `assert:` section (e.g. a shared bundle computing some vars) is just
    available to be called via `methods:` from a test bundle, like any other
    bundle in this file's inputs, without being reported as a test of its
    own.

    Uses the same syntax tree as `cfengine lint`, so comments and strings
    can't fake a bundle or a section. A bundle after a `body file control`
    namespace is returned namespaced (`ns:name`), as the bundlesequence
    needs it."""
    tree, _lines, _data = _parse_policy_file(path)
    if tree.root_node.has_error:
        raise UserError(f"Could not parse {path} -- check it with `cfengine lint`")

    names = []
    namespace = "default"
    for node in tree.root_node.children:
        if node.type == "body_block":
            namespace = _file_control_namespace(node) or namespace
            continue
        if node.type != "bundle_block":
            continue
        kinds = {child.type: child for child in node.children}
        if _node_text(kinds["bundle_block_type"]) != "agent":
            continue
        if not _has_assert_section(node):
            continue
        name = _node_text(kinds["bundle_block_name"])
        if "parameter_list" in kinds:
            raise UserError(
                f"Test bundle '{name}' in {path} takes parameters -- a bundle "
                "with an assert: section is run directly, so it can't take "
                "any. Move the asserts into a bundle without parameters, or "
                "call this one from such a bundle via methods:."
            )
        names.append(name if namespace == "default" else f"{namespace}:{name}")

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


def _expand_directories(paths: list, exclude_dirs: tuple = ()) -> list:
    """Files are kept as given; directories are walked for .cf files,
    skipping hidden directories and anything under exclude_dirs (e.g. a cfbs
    project's build output, whose promises.cf/lib/*.cf are not libraries to
    add to every test's inputs)."""
    excluded = {os.path.abspath(d) for d in exclude_dirs}
    expanded = []
    for path in paths:
        if not os.path.isdir(path):
            expanded.append(path)
            continue
        for root, dirs, files in os.walk(path, followlinks=True):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and os.path.abspath(os.path.join(root, d)) not in excluded
            ]
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


def _project_setup(
    masterfiles_dir: str | None, run_policy_in_setup: bool = True
) -> ProjectSetup:
    """Everything needed to make a cfbs project's masterfiles, custom promise
    types and services/init.cf available in the container; empty when
    there's no masterfiles_dir (a bare, cfbs-less test_*.cf file).

    run_policy_in_setup runs promises.cf once at the end of setup, so tests
    can assert on the state the project's policy converged to. Callers that
    run promises.cf themselves afterwards turn it off, as the policy would
    otherwise run twice."""
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

    # Bootstrap to itself, then do what a host's first agent run does:
    # update.cf, then (if wanted) promises.cf. Bootstrapping only runs the
    # built-in failsafe policy, and update.cf is what installs modules into
    # $(sys.workdir) (e.g. rendering the vendored package modules), so the
    # bootstrap's own policy run is always skipped -- it would run before
    # them. All of this happens once, in the shared setup image every test
    # starts from, so it adds no per-test cost. The scheduler (cf-execd) is
    # never needed: the container is committed right after this step.
    cf_agent = paths.bin("cf-agent")
    post_deploy_commands += [
        paths.bin("cf-serverd"),
        "sleep 1",
        f"{cf_agent} --bootstrap 127.0.0.1 "
        "--skip-bootstrap-service-start --skip-bootstrap-policy-run",
        f"{cf_agent} -KIf /var/cfengine/inputs/update.cf",
    ]
    if run_policy_in_setup:
        post_deploy_commands.append(f"{cf_agent} -KIf /var/cfengine/inputs/promises.cf")

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


def _bundlesequence_and_defs(bundle_reports: list) -> tuple:
    """bundle_reports is this file's [(bundle_name, report_id), ...], one
    pair per bundle agent it declares (see _bundle_names_declared_in).
    Returns (bundlesequence_names, bundle_def_lines): the cftest_start/
    marker bundle names in bundlesequence order (interleaved with the real
    test bundle names), and their own `bundle agent` definitions. Each
    bundle gets its own CFTEST-START/DONE pair, so several independent
    check bundles in one file still get attributed their own report row."""
    bundlesequence = []
    bundle_defs = []
    for i, (bundle_name, report_id) in enumerate(bundle_reports):
        start, marker = f"cftest_start_{i}", f"cftest_marker_{i}"
        bundlesequence += [start, bundle_name, marker]
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
    return bundlesequence, bundle_defs


def _run_cf_content(bundle_reports: list, inputs: list) -> str:
    """A standalone run.cf: its own `body common control` declaring exactly
    this file's inputs and bundlesequence -- the test bundle(s) are all that
    run, nothing from a real project's own policy. Used for --test-only,
    and for tests outside a cfbs project; see _bundle_defs_content for the
    full-policy default."""
    inputs_str = ", ".join([f'"{i}"' for i in inputs] + ["@(cftest_stdlib.inputs)"])
    bundlesequence, bundle_defs = _bundlesequence_and_defs(bundle_reports)
    bundlesequence_str = ", ".join(f'"{name}"' for name in bundlesequence)
    return "\n".join(
        [
            # The standard library of whichever masterfiles is deployed: the
            # project's own (cfbs project / --masterfiles) or the one the
            # CFEngine package installed. Checked at run time, since a
            # custom --dockerfile image may have none.
            "bundle common cftest_stdlib",
            "{",
            "  classes:",
            f'    "have_stdlib" expression => fileexists("{_STDLIB}");',
            "",
            "  vars:",
            "    have_stdlib::",
            f'      "inputs" slist => {{ "{_STDLIB}" }};',
            "    !have_stdlib::",
            '      "inputs" slist => { };',
            "}",
            "",
            "body common control",
            "{",
            f"  inputs => {{ {inputs_str} }};",
            f"  bundlesequence => {{ {bundlesequence_str} }};",
            "}",
            "",
        ]
        + bundle_defs
    )


def _bundle_defs_content(bundle_reports: list) -> str:
    """Just the cftest_start/marker `bundle agent` definitions, no `body
    common control` of its own -- for full-policy mode (the default in a
    cfbs project), where the real project's promises.cf supplies the control
    body and bundlesequence."""
    _bundlesequence, bundle_defs = _bundlesequence_and_defs(bundle_reports)
    return "\n".join(bundle_defs)


def _merge_def_json(
    masterfiles_dir: str,
    fixture_path: str | None,
    extra_inputs: list,
    extra_bundlesequence: list,
) -> str:
    """Full-policy mode runs the project's own real promises.cf as the
    entry point, instead of a from-scratch run.cf -- so the test's own
    inputs and bundlesequence additions have to be merged into def.json
    instead of declared directly.

    The fixture is merged in the way `cfbs build` merges a module's def.json
    contributions: objects merge, single values are overwritten, lists are
    appended. So a fixture can never drop the project's generated wiring
    (its modules' inputs and bundlesequence entries) -- but it can't shrink
    a list the project sets either; use a differently named variable."""
    merged = read_json(os.path.join(masterfiles_dir, "def.json")) or {}

    if fixture_path is not None:
        merged = merge_json(merged, read_json(fixture_path) or {})

    extra = {
        "inputs": extra_inputs,
        "vars": {"control_common_bundlesequence_end": extra_bundlesequence},
    }
    merged = merge_json(merged, extra)

    return json.dumps(merged, indent=2)


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
    full_policy: bool = False,
    masterfiles_dir: str | None = None,
) -> tuple:
    """Writes this test's run.cf (and fixture, if any) under runners_dir, a
    fresh per-test tempdir. bundle_reports is this file's [(bundle_name,
    report_id), ...] (see _bundlesequence_and_defs). Returns (mount_args,
    run_command).

    Run after the project's own real bundlesequence has converged for real, rather
    than only ever seeing whatever state the one-time setup/bootstrap step left behind.
    """
    test_mount = f"/mnt/tests/{os.path.basename(test_file)}"
    mount_args = _read_only_mount(test_file, test_mount)

    inputs = _inputs_for_test(test_mount, project, library_mounts)

    # Fixtures are keyed by the test file's name, not its bundle name --
    # two files can share a bundle name.
    test_stem = os.path.splitext(os.path.basename(test_file))[0]
    fixture = os.path.join(os.path.dirname(test_file), "fixtures", f"{test_stem}.json")
    fixture_path = fixture if os.path.isfile(fixture) else None

    if full_policy:
        assert masterfiles_dir is not None  # enforced by run_files_in_container
        wrapper_mount = "/mnt/runners/cftest_wrapper.cf"
        with open(os.path.join(runners_dir, "cftest_wrapper.cf"), "w") as wrapper_cf:
            wrapper_cf.write(_bundle_defs_content(bundle_reports))

        bundlesequence, _bundle_defs = _bundlesequence_and_defs(bundle_reports)
        def_json = os.path.join(runners_dir, "def.json")
        with open(def_json, "w") as def_json_file:
            def_json_file.write(
                _merge_def_json(
                    masterfiles_dir,
                    fixture_path,
                    inputs + [wrapper_mount],
                    bundlesequence,
                )
            )
        # Augments (def.json) are loaded from next to the entry file being
        # parsed -- /var/cfengine/masterfiles/promises.cf here -- so the
        # merged def.json has to override that exact path, on top of the
        # whole-directory /mnt/runners mount below that makes the wrapper
        # file (and this same def.json) reachable at all.
        mount_args += _read_only_mount(def_json, "/var/cfengine/masterfiles/def.json")
        run_command = (
            f"{paths.bin('cf-agent')} -KIf /var/cfengine/masterfiles/promises.cf"
        )
        return mount_args, run_command

    with open(os.path.join(runners_dir, "run.cf"), "w") as run_cf:
        run_cf.write(_run_cf_content(bundle_reports, inputs))
    if fixture_path is not None:
        shutil.copyfile(fixture_path, os.path.join(runners_dir, "def.json"))

    run_command = f"{paths.bin('cf-agent')} -KIf /mnt/runners/run.cf"
    return mount_args, run_command


def run_files_in_container(
    files: list,
    masterfiles_dir: str | None = None,
    dockerfile: str | None = None,
    rebuild: bool = False,
    test_only: bool = False,
) -> int:
    """Runs each test_*.cf file as its own isolated agent-run in its own
    container, parses [ASSERT] PASS/FAIL lines out of the output,
    and prints a pytest-style report, returning 0 only if everything passed.
    A file may declare several bundles; each becomes its own "<file>::<bundle>"
    report row within that one file's run.

    Every non-test_ file is a shared library added to every test's `inputs`.
    A sibling fixtures/<test file name>.json is auto-loaded as that run's
    `def.json`. masterfiles_dir, if given, is a built cfbs policy set,
    deployed once for every test to share.

    In a cfbs project (masterfiles_dir given), each test runs at the end of
    the project's own real promises.cf (extended via augments, see
    _prepare_test_run), so it sees the state a full agent run leaves behind,
    with its own fixture applied. test_only runs just the test bundles from a
    minimal generated policy instead; the project's policy then only ran
    once, during the shared setup step and without any fixture, so every
    test sees that snapshot. Without a cfbs project there's no real policy
    to run, so tests always run that way."""
    require_docker()
    full_policy = masterfiles_dir is not None and not test_only
    context_dir, dockerfile_path = _resolve_dockerfile(dockerfile)
    image_tag = _image_tag_for(dockerfile_path)
    _ensure_image_built(context_dir, dockerfile_path, image_tag, rebuild)

    # cfbs builds into out/ (masterfiles_dir is out/masterfiles); none of it
    # is the user's own test or library code.
    exclude_dirs = (
        (os.path.dirname(os.path.abspath(masterfiles_dir)),)
        if masterfiles_dir is not None
        else ()
    )
    # dict.fromkeys: dedupe while keeping order -- the same file can be both
    # auto-discovered (a module's main.cf) and inside a directory argument.
    abs_files = list(
        dict.fromkeys(
            os.path.abspath(f) for f in _expand_directories(files, exclude_dirs)
        )
    )
    test_files = [f for f in abs_files if os.path.basename(f).startswith("test_")]
    library_files = [f for f in abs_files if f not in test_files]
    if not test_files:
        raise UserError(
            "No test_*.cf files given to `cfengine test` -- nothing to run. "
            "Pass at least one file whose name starts with 'test_', "
            "e.g. `cfengine test tests/test_foo.cf`."
        )

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

    # Full-policy mode runs promises.cf again in every test's own
    # container; --test-only relies on the bootstrap's run instead.
    project = _project_setup(masterfiles_dir, run_policy_in_setup=not full_policy)
    setup_commands = (
        ["rm -rf /var/cfengine/inputs"]
        + project.deploy_commands
        + project.post_deploy_commands
    )

    library_mount_args = []
    library_mounts = []
    for i, lib in enumerate(library_files):
        # Indexed directory, as several libraries can share a basename --
        # every module's file is main.cf.
        lib_mount = f"/mnt/lib/{i}/{os.path.basename(lib)}"
        library_mount_args += _read_only_mount(lib, lib_mount)
        library_mounts.append(lib_mount)

    prepped_image = _run_setup_and_commit(project.mount_args, setup_commands, image_tag)
    try:
        results = RunResults(set(), {}, [])

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
                    full_policy=full_policy,
                    masterfiles_dir=(
                        os.path.abspath(masterfiles_dir)
                        if masterfiles_dir is not None
                        else None
                    ),
                )
                mount_args = (
                    library_mount_args
                    + test_mount_args
                    + _read_only_mount(runners_dir, "/mnt/runners")
                )

                one_test_results = run_and_parse(
                    mount_args, run_command, test_names, image=prepped_image
                )

            results.completed.update(one_test_results.completed)
            results.marks_by_test.update(one_test_results.marks_by_test)
            results.failures.extend(one_test_results.failures)
    finally:
        _remove_image(prepped_image)

    return print_report(report_ids, results)
