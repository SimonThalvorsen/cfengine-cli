import pytest

import cfengine_cli.container as container
from cfengine_cli.report import DeployResults
from cfengine_cli.utils import UserError


@pytest.fixture
def no_real_docker(monkeypatch):
    """run_files_in_container/run_in_container always start by checking for
    and building the docker image, then setting up and committing a prepped
    image -- stub all of that out so tests don't need a real docker daemon."""
    monkeypatch.setattr(container, "require_docker", lambda: None)
    monkeypatch.setattr(container, "_ensure_image_built", lambda *a, **k: None)
    monkeypatch.setattr(
        container, "_run_setup_and_commit", lambda *a, **k: "prepped-image:latest"
    )
    monkeypatch.setattr(container, "_remove_image", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# require_docker
# ---------------------------------------------------------------------------


def test_require_docker_raises_when_missing(monkeypatch):
    monkeypatch.setattr(container.shutil, "which", lambda name: None)
    with pytest.raises(UserError):
        container.require_docker()


def test_require_docker_passes_when_present(monkeypatch):
    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/docker")
    container.require_docker()  # doesn't raise


# ---------------------------------------------------------------------------
# _resolve_dockerfile_dir / _image_tag_for
# ---------------------------------------------------------------------------


def test_resolve_dockerfile_defaults_to_builtin():
    context_dir, dockerfile_path = container._resolve_dockerfile(None)
    assert context_dir == container._DOCKERFILE_DIR
    assert dockerfile_path == f"{container._DOCKERFILE_DIR}/Dockerfile"


def test_resolve_dockerfile_accepts_a_directory(tmp_path):
    assert container._resolve_dockerfile(str(tmp_path)) == (
        str(tmp_path),
        str(tmp_path / "Dockerfile"),
    )


def test_resolve_dockerfile_keeps_a_non_default_dockerfile_name(tmp_path):
    dockerfile = tmp_path / "Dockerfile.rhel"
    dockerfile.write_text("FROM rockylinux:9\n")
    assert container._resolve_dockerfile(str(dockerfile)) == (
        str(tmp_path),
        str(dockerfile),
    )


def test_image_tag_for_builtin_dockerfile_is_the_stable_tag():
    _context_dir, dockerfile_path = container._resolve_dockerfile(None)
    assert container._image_tag_for(dockerfile_path) == container._IMAGE_TAG


def test_image_tag_for_custom_dockerfile_is_distinct_and_deterministic(tmp_path):
    tag_a = container._image_tag_for(str(tmp_path / "Dockerfile"))
    tag_b = container._image_tag_for(str(tmp_path / "Dockerfile"))
    assert tag_a == tag_b
    assert tag_a != container._IMAGE_TAG
    assert tag_a.startswith("cfengine-cli-test-agent-custom-")


def test_image_tag_for_sibling_dockerfiles_differ(tmp_path):
    tag_default = container._image_tag_for(str(tmp_path / "Dockerfile"))
    tag_rhel = container._image_tag_for(str(tmp_path / "Dockerfile.rhel"))
    assert tag_default != tag_rhel


# ---------------------------------------------------------------------------
# _ensure_image_built
# ---------------------------------------------------------------------------


def _record_runs(monkeypatch, returncode=0):
    seen_cmds = []
    monkeypatch.setattr(
        container.subprocess,
        "run",
        lambda cmd, **k: seen_cmds.append(cmd)
        or container.subprocess.CompletedProcess(cmd, returncode),
    )
    return seen_cmds


def test_ensure_image_built_passes_no_cache_only_when_rebuilding(monkeypatch):
    seen_cmds = _record_runs(monkeypatch)

    container._ensure_image_built(
        "/some/dir", "/some/dir/Dockerfile", "some-tag:latest", rebuild=False
    )
    container._ensure_image_built(
        "/some/dir", "/some/dir/Dockerfile", "some-tag:latest", rebuild=True
    )

    assert "--no-cache" not in seen_cmds[0]
    assert "--no-cache" in seen_cmds[1]


def test_ensure_image_built_builds_the_given_dockerfile(monkeypatch):
    seen_cmds = _record_runs(monkeypatch)

    container._ensure_image_built(
        "/some/dir", "/some/dir/Dockerfile.rhel", "some-tag:latest", rebuild=False
    )

    cmd = seen_cmds[0]
    assert cmd[cmd.index("-f") + 1] == "/some/dir/Dockerfile.rhel"
    assert cmd[-1] == "/some/dir"


def test_ensure_image_built_raises_on_build_failure(monkeypatch):
    _record_runs(monkeypatch, returncode=1)
    with pytest.raises(UserError):
        container._ensure_image_built(
            "/some/dir", "/some/dir/Dockerfile", "some-tag:latest", rebuild=False
        )


# ---------------------------------------------------------------------------
# _run_setup_and_commit
# ---------------------------------------------------------------------------


def test_run_setup_and_commit_raises_when_setup_fails(monkeypatch):
    monkeypatch.setattr(
        container.subprocess,
        "run",
        lambda *a, **k: container.subprocess.CompletedProcess(a, 1),
    )
    monkeypatch.setattr(container, "_docker_cleanup", lambda *a: None)

    with pytest.raises(UserError):
        container._run_setup_and_commit([], ["false"], "some-tag:latest")


def test_run_setup_and_commit_raises_user_error_and_cleans_up_when_commit_fails(
    monkeypatch,
):
    def fake_run(cmd, **k):
        returncode = 1 if cmd[1] == "commit" else 0
        return container.subprocess.CompletedProcess(cmd, returncode, "", "disk full")

    cleaned = []
    monkeypatch.setattr(container.subprocess, "run", fake_run)
    monkeypatch.setattr(container, "_docker_cleanup", lambda *a: cleaned.append(a))

    with pytest.raises(UserError, match="disk full"):
        container._run_setup_and_commit([], ["true"], "some-tag:latest")
    assert cleaned and cleaned[0][0] == "rm"


# ---------------------------------------------------------------------------
# run_in_container
# ---------------------------------------------------------------------------


def test_run_in_container_reports_totals_when_asserts_are_present(
    no_real_docker, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        container,
        "run_and_scan_asserts",
        lambda cmd: DeployResults(returncode=1, passed=2, failed=1),
    )

    rc = container.run_in_container(str(tmp_path))
    out = capsys.readouterr().out

    assert rc == 1
    assert "2 passed" in out
    assert "1 failed" in out


def test_run_in_container_returns_raw_exit_code_without_asserts(
    no_real_docker, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        container,
        "run_and_scan_asserts",
        lambda cmd: DeployResults(returncode=7, passed=0, failed=0),
    )

    assert container.run_in_container(str(tmp_path)) == 7


def test_run_in_container_fails_when_agent_fails_despite_passing_asserts(
    no_real_docker, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        container,
        "run_and_scan_asserts",
        lambda cmd: DeployResults(returncode=2, passed=1, failed=0),
    )

    rc = container.run_in_container(str(tmp_path))
    out = capsys.readouterr().out

    assert rc == 2
    assert "1 passed, 0 failed, 1 errors" in out


def test_run_in_container_fails_when_agent_fell_back_to_failsafe(
    no_real_docker, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        container,
        "run_and_scan_asserts",
        lambda cmd: DeployResults(
            returncode=0, passed=0, failed=0, fell_back_to_failsafe=True
        ),
    )

    rc = container.run_in_container(str(tmp_path))

    assert rc != 0
    assert "failsafe" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _expand_directories
# ---------------------------------------------------------------------------


def test_expand_directories_skips_excluded_and_hidden_dirs(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.cf").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.cf").write_text("")
    (tmp_path / "out" / "masterfiles").mkdir(parents=True)
    (tmp_path / "out" / "masterfiles" / "promises.cf").write_text("")

    expanded = container._expand_directories(
        [str(tmp_path)], exclude_dirs=(str(tmp_path / "out"),)
    )

    assert expanded == [str(tmp_path / "tests" / "test_a.cf")]


# ---------------------------------------------------------------------------
# _bundle_names_declared_in
# ---------------------------------------------------------------------------


def test_bundle_names_declared_in_single_bundle(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text('bundle agent test_foo\n{\n  assert:\n    "x" pass => "true";\n}\n')
    assert container._bundle_names_declared_in(str(path)) == ["test_foo"]


def test_bundle_names_declared_in_excludes_bundles_without_assert_section(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text(
        'bundle agent variables\n{\n  vars:\n    "x" string => "1";\n}\n'
        '\nbundle agent test_foo\n{\n  assert:\n    "x" pass => "true";\n}\n'
    )
    assert container._bundle_names_declared_in(str(path)) == ["test_foo"]


def test_bundle_names_declared_in_ignores_comments_and_strings(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text(
        "# bundle agent commented_out\n"
        'bundle agent helper\n{\n  reports:\n    "assert: { in a string";\n}\n'
        '\nbundle agent test_foo\n{\n  assert:\n    "x" pass => "true";\n}\n'
    )
    assert container._bundle_names_declared_in(str(path)) == ["test_foo"]


def test_bundle_names_declared_in_namespaces_bundles(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text(
        'body file control\n{\n  namespace => "myns";\n}\n'
        '\nbundle agent test_foo\n{\n  assert:\n    "x" pass => "true";\n}\n'
    )
    assert container._bundle_names_declared_in(str(path)) == ["myns:test_foo"]


def test_bundle_names_declared_in_rejects_test_bundle_with_parameters(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text(
        'bundle agent test_foo(arg)\n{\n  assert:\n    "x" pass => "true";\n}\n'
    )
    with pytest.raises(UserError, match="takes parameters"):
        container._bundle_names_declared_in(str(path))


def test_bundle_names_declared_in_raises_user_error_on_unparsable_file(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text("bundle agent test_foo\n")
    with pytest.raises(UserError, match="Could not parse"):
        container._bundle_names_declared_in(str(path))


def test_bundle_names_declared_in_raises_without_declaration(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text("# no bundle here\n")
    with pytest.raises(UserError):
        container._bundle_names_declared_in(str(path))


def test_bundle_names_declared_in_raises_when_no_bundle_has_assert_section(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text('bundle agent variables\n{\n  vars:\n    "x" string => "1";\n}\n')
    with pytest.raises(UserError):
        container._bundle_names_declared_in(str(path))


# ---------------------------------------------------------------------------
# run_files_in_container
# ---------------------------------------------------------------------------


def test_run_files_in_container_raises_without_test_files(no_real_docker, tmp_path):
    lib = tmp_path / "lib.cf"
    lib.write_text("bundle agent lib {}\n")

    with pytest.raises(UserError):
        container.run_files_in_container([str(lib)])


_TEST_X = 'bundle agent test_x\n{\n  assert:\n    "x" pass => "true";\n}\n'


def _capture_run_and_parse(monkeypatch):
    calls = []

    def fake_run_and_parse(mount_args, script, test_names, image):
        calls.append(mount_args)
        return container.RunResults(set(test_names), {n: [] for n in test_names}, [])

    monkeypatch.setattr(container, "run_and_parse", fake_run_and_parse)
    monkeypatch.setattr(container, "print_report", lambda *a: 0)
    return calls


def _library_mount_targets(mount_args):
    return [arg.split(":")[1] for arg in mount_args if ":/mnt/lib/" in arg]


def test_run_files_in_container_mounts_same_named_libraries_apart(
    no_real_docker, monkeypatch, tmp_path
):
    for mod in ("mod_a", "mod_b"):
        (tmp_path / mod).mkdir()
        (tmp_path / mod / "main.cf").write_text(f"bundle agent {mod} {{}}\n")
    test = tmp_path / "test_x.cf"
    test.write_text(_TEST_X)
    calls = _capture_run_and_parse(monkeypatch)

    container.run_files_in_container(
        [
            str(tmp_path / "mod_a" / "main.cf"),
            str(tmp_path / "mod_b" / "main.cf"),
            str(test),
        ]
    )

    targets = _library_mount_targets(calls[0])
    assert len(targets) == 2
    assert len(set(targets)) == 2


def test_run_files_in_container_dedupes_files(no_real_docker, monkeypatch, tmp_path):
    lib = tmp_path / "lib.cf"
    lib.write_text("bundle agent lib {}\n")
    (tmp_path / "test_x.cf").write_text(_TEST_X)
    calls = _capture_run_and_parse(monkeypatch)

    container.run_files_in_container([str(lib), str(tmp_path)])

    assert len(_library_mount_targets(calls[0])) == 1


def test_run_files_in_container_skips_cfbs_build_output(
    no_real_docker, monkeypatch, tmp_path
):
    (tmp_path / "out" / "masterfiles").mkdir(parents=True)
    (tmp_path / "out" / "masterfiles" / "promises.cf").write_text("")
    (tmp_path / "test_x.cf").write_text(_TEST_X)
    monkeypatch.setattr(
        container,
        "_project_setup",
        lambda *a, **k: container.ProjectSetup([], [], [], None, False),
    )
    calls = _capture_run_and_parse(monkeypatch)

    container.run_files_in_container(
        [str(tmp_path)], masterfiles_dir=str(tmp_path / "out" / "masterfiles")
    )

    assert _library_mount_targets(calls[0]) == []


def _agent_runs(project):
    """The policy files setup runs cf-agent on, in order."""
    return [c.rsplit("/", 1)[-1] for c in project.post_deploy_commands if " -KIf " in c]


def test_project_setup_runs_update_then_promises_like_a_first_agent_run(tmp_path):
    project = container._project_setup(str(tmp_path))
    [bootstrap] = [c for c in project.post_deploy_commands if "--bootstrap" in c]
    assert "--skip-bootstrap-policy-run" in bootstrap
    assert "--skip-bootstrap-service-start" in bootstrap
    assert _agent_runs(project) == ["update.cf", "promises.cf"]


def test_project_setup_can_skip_the_policy_run(tmp_path):
    project = container._project_setup(str(tmp_path), run_policy_in_setup=False)
    assert _agent_runs(project) == ["update.cf"]


def test_run_files_in_container_runs_policy_in_setup_only_for_test_only(
    no_real_docker, monkeypatch, tmp_path
):
    (tmp_path / "test_x.cf").write_text(_TEST_X)
    (tmp_path / "out" / "masterfiles").mkdir(parents=True)
    _capture_run_and_parse(monkeypatch)
    seen = []
    real_project_setup = container._project_setup
    monkeypatch.setattr(
        container,
        "_project_setup",
        lambda *a, **k: seen.append(k.get("run_policy_in_setup"))
        or real_project_setup(*a, **k),
    )
    monkeypatch.setattr(container, "_merge_def_json", lambda *a, **k: "{}")

    masterfiles = str(tmp_path / "out" / "masterfiles")
    container.run_files_in_container([str(tmp_path / "test_x.cf")], masterfiles)
    container.run_files_in_container(
        [str(tmp_path / "test_x.cf")], masterfiles, test_only=True
    )

    assert seen == [False, True]


def test_run_cf_content_loads_stdlib_only_if_present():
    content = container._run_cf_content([("test_x", "f.cf::test_x")], ["/mnt/t.cf"])
    assert 'fileexists("/var/cfengine/masterfiles/lib/stdlib.cf")' in content
    assert 'inputs => { "/mnt/t.cf", @(cftest_stdlib.inputs) };' in content


# ---------------------------------------------------------------------------
# prepare_test_masterfiles
# ---------------------------------------------------------------------------


def test_prepare_test_masterfiles_without_project_or_flag(monkeypatch):
    monkeypatch.setattr(container, "is_cfbs_repo", lambda: False)
    assert container.prepare_test_masterfiles(None) == (0, None)


def test_prepare_test_masterfiles_accepts_built_masterfiles(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "is_cfbs_repo", lambda: False)
    (tmp_path / "promises.cf").write_text("")
    assert container.prepare_test_masterfiles(str(tmp_path)) == (
        0,
        str(tmp_path),
    )


def test_prepare_test_masterfiles_rejects_dir_without_promises_cf(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(container, "is_cfbs_repo", lambda: False)
    with pytest.raises(UserError, match="promises.cf"):
        container.prepare_test_masterfiles(str(tmp_path))


def test_prepare_test_masterfiles_rejects_flag_in_cfbs_project(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "is_cfbs_repo", lambda: True)
    with pytest.raises(UserError, match="cfbs project"):
        container.prepare_test_masterfiles(str(tmp_path))
