import os
from types import SimpleNamespace

import pytest

import cfengine_cli.container as container
from cfengine_cli.report import DeployResults, RunResults
from cfengine_cli.utils import UserError


@pytest.fixture
def no_real_docker(monkeypatch):
    """run_files_in_container/run_in_container always start by checking for
    and building the docker image -- stub both out so tests don't need a
    real docker daemon."""
    monkeypatch.setattr(container, "require_docker", lambda: None)
    monkeypatch.setattr(
        container,
        "_ensure_image_built",
        lambda dockerfile_dir, image_tag, rebuild: None,
    )


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
# _resolve_dockerfile_dir / _image_tag_for / _ensure_image_built
# ---------------------------------------------------------------------------


def test_resolve_dockerfile_dir_none_is_builtin():
    assert container._resolve_dockerfile_dir(None) == container._DOCKERFILE_DIR


def test_resolve_dockerfile_dir_accepts_a_directory(tmp_path):
    assert container._resolve_dockerfile_dir(str(tmp_path)) == str(tmp_path)


def test_resolve_dockerfile_dir_accepts_a_dockerfile_path(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM debian:13-slim\n")
    assert container._resolve_dockerfile_dir(str(dockerfile)) == str(tmp_path)


def test_image_tag_for_builtin_dockerfile_is_the_stable_tag():
    assert container._image_tag_for(container._DOCKERFILE_DIR) == container._IMAGE_TAG


def test_image_tag_for_custom_dockerfile_is_derived_and_distinct(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    tag_a = container._image_tag_for(str(tmp_path))
    tag_b = container._image_tag_for(str(other))

    assert tag_a != container._IMAGE_TAG
    assert tag_a != tag_b  # different dockerfile dirs never collide
    assert container._image_tag_for(str(tmp_path)) == tag_a  # stable for the same dir


def test_ensure_image_built_plain(monkeypatch):
    calls = []
    monkeypatch.setattr(
        container.subprocess,
        "run",
        lambda cmd: calls.append(cmd) or SimpleNamespace(returncode=0),
    )

    container._ensure_image_built("/some/dir", "some:tag", rebuild=False)

    assert calls == [["docker", "build", "-q", "-t", "some:tag", "/some/dir"]]


def test_ensure_image_built_rebuild_adds_no_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(
        container.subprocess,
        "run",
        lambda cmd: calls.append(cmd) or SimpleNamespace(returncode=0),
    )

    container._ensure_image_built("/some/dir", "some:tag", rebuild=True)

    assert calls == [
        ["docker", "build", "-q", "-t", "some:tag", "--no-cache", "/some/dir"]
    ]


def test_ensure_image_built_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        container.subprocess, "run", lambda cmd: SimpleNamespace(returncode=1)
    )
    with pytest.raises(UserError):
        container._ensure_image_built("/some/dir", "some:tag", rebuild=False)


# ---------------------------------------------------------------------------
# _bundle_names_declared_in / _declared_promise_types
# ---------------------------------------------------------------------------


def test_bundle_names_declared_in_single_bundle(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text('bundle agent test_foo\n{\n  assert:\n    "x" pass => "true";\n}\n')
    assert container._bundle_names_declared_in(str(path)) == ["test_foo"]


def test_bundle_names_declared_in_multiple_bundles(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text(
        'bundle agent test_numbers\n{\n  assert:\n    "x" pass => "true";\n}\n'
        '\nbundle agent test_strings\n{\n  assert:\n    "y" pass => "true";\n}\n'
    )
    assert container._bundle_names_declared_in(str(path)) == [
        "test_numbers",
        "test_strings",
    ]


def test_bundle_names_declared_in_raises_without_declaration(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text("# no bundle here\n")
    with pytest.raises(UserError):
        container._bundle_names_declared_in(str(path))


def test_bundle_names_declared_in_excludes_bundles_without_assert_section(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text(
        'bundle agent variables\n{\n  vars:\n    "x" string => "1";\n}\n'
        '\nbundle agent test_foo\n{\n  assert:\n    "x" pass => "true";\n}\n'
    )
    assert container._bundle_names_declared_in(str(path)) == ["test_foo"]


def test_bundle_names_declared_in_raises_when_no_bundle_has_assert_section(tmp_path):
    path = tmp_path / "test_foo.cf"
    path.write_text('bundle agent variables\n{\n  vars:\n    "x" string => "1";\n}\n')
    with pytest.raises(UserError):
        container._bundle_names_declared_in(str(path))


def test_declared_promise_types(tmp_path):
    path = tmp_path / "init.cf"
    path.write_text("promise agent assert\n{\n}\n\npromise agent yaml_matches\n{\n}\n")
    assert container._declared_promise_types(str(path)) == {"assert", "yaml_matches"}


def test_declared_promise_types_empty_when_none_declared(tmp_path):
    path = tmp_path / "init.cf"
    path.write_text("bundle agent foo {}\n")
    assert container._declared_promise_types(str(path)) == set()


# ---------------------------------------------------------------------------
# _expand_directories
# ---------------------------------------------------------------------------


def test_expand_directories_passes_files_through_unchanged(tmp_path):
    f = tmp_path / "test_a.cf"
    f.write_text("")
    assert container._expand_directories([str(f)]) == [str(f)]


def test_expand_directories_finds_cf_files_in_a_folder(tmp_path):
    (tmp_path / "test_a.cf").write_text("")
    (tmp_path / "test_b.cf").write_text("")
    (tmp_path / "not_cf.txt").write_text("")

    found = container._expand_directories([str(tmp_path)])

    assert found == [
        str(tmp_path / "test_a.cf"),
        str(tmp_path / "test_b.cf"),
    ]


def test_expand_directories_recurses_and_skips_hidden(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "test_nested.cf").write_text("")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "test_hidden.cf").write_text("")
    (tmp_path / ".hidden.cf").write_text("")

    found = container._expand_directories([str(tmp_path)])

    assert found == [str(nested / "test_nested.cf")]


# ---------------------------------------------------------------------------
# discover_test_files / discover_module_files
# ---------------------------------------------------------------------------


def test_discover_test_files_prefers_tests_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_b.cf").write_text("")
    (tmp_path / "tests" / "test_a.cf").write_text("")
    (tmp_path / "tests" / "not_a_test.cf").write_text("")
    (tmp_path / "test_top_level.cf").write_text("")  # ignored, tests/ exists

    found = container.discover_test_files()
    assert found == [
        os.path.join("tests", "test_a.cf"),
        os.path.join("tests", "test_b.cf"),
    ]


def test_discover_test_files_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "test_only.cf").write_text("")

    found = container.discover_test_files()
    assert found == [os.path.join(".", "test_only.cf")]


def test_discover_module_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mod_a").mkdir()
    (tmp_path / "mod_a" / "main.cf").write_text("")
    (tmp_path / "mod_b").mkdir()
    (tmp_path / "mod_b" / "main.cf").write_text("")
    (tmp_path / "no_main").mkdir()

    assert sorted(container.discover_module_files()) == [
        os.path.join("mod_a", "main.cf"),
        os.path.join("mod_b", "main.cf"),
    ]


# ---------------------------------------------------------------------------
# _read_only_mount
# ---------------------------------------------------------------------------


def test_read_only_mount():
    assert container._read_only_mount("/host/path", "/container/path") == [
        "-v",
        "/host/path:/container/path:ro",
    ]


# ---------------------------------------------------------------------------
# _project_setup
# ---------------------------------------------------------------------------


def test_project_setup_none_when_no_masterfiles_dir():
    setup = container._project_setup(None)
    assert setup == container.ProjectSetup([], [], [], None, False)


def test_project_setup_bare_masterfiles_dir(tmp_path):
    setup = container._project_setup(str(tmp_path))
    assert setup.init_mount is None
    assert setup.declares_assert is False
    assert "cp -r /mnt/project_masterfiles/modules/." not in " ".join(
        setup.post_deploy_commands
    )
    assert any("--bootstrap 127.0.0.1" in cmd for cmd in setup.post_deploy_commands)
    assert setup.mount_args == container._read_only_mount(
        str(tmp_path), "/mnt/project_masterfiles"
    )


def test_project_setup_copies_modules_dir_when_present(tmp_path):
    (tmp_path / "modules").mkdir()
    setup = container._project_setup(str(tmp_path))
    assert (
        "cp -r /mnt/project_masterfiles/modules/. /var/cfengine/modules/"
        in setup.post_deploy_commands
    )
    assert any("--bootstrap 127.0.0.1" in cmd for cmd in setup.post_deploy_commands)


def test_project_setup_detects_custom_assert_override(tmp_path):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    (services_dir / "init.cf").write_text("promise agent assert\n{\n}\n")

    setup = container._project_setup(str(tmp_path))
    assert setup.init_mount == "/var/cfengine/masterfiles/services/init.cf"
    assert setup.declares_assert is True


def test_project_setup_no_override_without_assert_declaration(tmp_path):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    (services_dir / "init.cf").write_text("bundle agent startup {}\n")

    setup = container._project_setup(str(tmp_path))
    assert setup.init_mount == "/var/cfengine/masterfiles/services/init.cf"
    assert setup.declares_assert is False


# ---------------------------------------------------------------------------
# _run_cf_content / _inputs_for_test / _prepare_test_run
# ---------------------------------------------------------------------------


def test_run_cf_content_includes_markers_and_inputs():
    content = container._run_cf_content(
        [("my_test", "tests/dir_a/test_x.cf")], ["/mnt/a.cf", "/mnt/b.cf"]
    )
    assert '"/mnt/a.cf", "/mnt/b.cf"' in content
    assert '"[CFTEST-START] tests/dir_a/test_x.cf"' in content
    assert '"[CFTEST-DONE] tests/dir_a/test_x.cf"' in content
    assert (
        'bundlesequence => { "cftest_start_0", "my_test", "cftest_marker_0" };'
        in content
    )


def test_run_cf_content_wraps_each_bundle_separately(tmp_path):
    content = container._run_cf_content(
        [
            ("test_numbers", "test_checks.cf::test_numbers"),
            ("test_strings", "test_checks.cf::test_strings"),
        ],
        ["/mnt/x.cf"],
    )
    assert (
        'bundlesequence => { "cftest_start_0", "test_numbers", "cftest_marker_0", '
        '"cftest_start_1", "test_strings", "cftest_marker_1" };' in content
    )
    assert '"[CFTEST-START] test_checks.cf::test_numbers"' in content
    assert '"[CFTEST-DONE] test_checks.cf::test_numbers"' in content
    assert '"[CFTEST-START] test_checks.cf::test_strings"' in content
    assert '"[CFTEST-DONE] test_checks.cf::test_strings"' in content


def test_inputs_for_test_includes_assert_module_by_default():
    project = container.ProjectSetup([], [], [], None, False)
    inputs = container._inputs_for_test("/mnt/tests/x.cf", project, [])
    assert inputs == [
        f"{container._ASSERT_MODULE_DIR}/promise_agent.cf",
        "/mnt/tests/x.cf",
    ]


def test_inputs_for_test_skips_assert_module_when_project_declares_it():
    project = container.ProjectSetup(
        [], [], [], "/mnt/project_masterfiles/services/init.cf", True
    )
    inputs = container._inputs_for_test("/mnt/tests/x.cf", project, ["/mnt/lib/foo.cf"])
    assert inputs == [
        "/mnt/project_masterfiles/services/init.cf",
        "/mnt/lib/foo.cf",
        "/mnt/tests/x.cf",
    ]


def test_prepare_test_run_writes_run_cf_and_mounts(tmp_path):
    test_file = tmp_path / "test_thing.cf"
    test_file.write_text("bundle agent test_thing {}\n")
    runners_dir = tmp_path / "runners"
    runners_dir.mkdir()
    project = container.ProjectSetup([], [], [], None, False)

    mount_args, run_command = container._prepare_test_run(
        str(runners_dir),
        str(test_file),
        [("test_thing", "tests/test_thing.cf")],
        project,
        [],
    )

    assert mount_args == container._read_only_mount(
        str(test_file), "/mnt/tests/test_thing.cf"
    )
    assert run_command == "/var/cfengine/bin/cf-agent -KIf /mnt/runners/run.cf"
    written = (runners_dir / "run.cf").read_text()
    assert "/mnt/tests/test_thing.cf" in written


def test_prepare_test_run_loads_sibling_fixture(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_thing.cf"
    test_file.write_text("bundle agent test_thing {}\n")
    fixtures_dir = tests_dir / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "test_thing.json").write_text('{"foo": "bar"}')
    runners_dir = tmp_path / "runners"
    runners_dir.mkdir()
    project = container.ProjectSetup([], [], [], None, False)

    container._prepare_test_run(
        str(runners_dir),
        str(test_file),
        [("test_thing", "tests/test_thing.cf")],
        project,
        [],
    )

    assert (runners_dir / "def.json").read_text() == '{"foo": "bar"}'


def test_prepare_test_run_fixture_keyed_by_file_not_bundle_name(tmp_path):
    """Two files can now declare the same bundle name, so the fixture
    lookup can't be keyed by bundle name -- both files would load the same
    fixture. It's keyed by the test file's own name instead."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_a.cf"
    test_file.write_text("bundle agent shared_name {}\n")
    fixtures_dir = tests_dir / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "shared_name.json").write_text('{"wrong": "fixture"}')
    (fixtures_dir / "test_a.json").write_text('{"right": "fixture"}')
    runners_dir = tmp_path / "runners"
    runners_dir.mkdir()
    project = container.ProjectSetup([], [], [], None, False)

    container._prepare_test_run(
        str(runners_dir),
        str(test_file),
        [("shared_name", "tests/test_a.cf")],
        project,
        [],
    )

    assert (runners_dir / "def.json").read_text() == '{"right": "fixture"}'


# ---------------------------------------------------------------------------
# _run_setup_and_commit / _remove_image
# ---------------------------------------------------------------------------


def test_run_setup_and_commit_success(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    image_tag = container._run_setup_and_commit(
        [], ["echo hi"], "cfengine-cli-test-agent:latest"
    )

    assert image_tag.startswith("cfengine-cli-test-agent-prepped:")
    assert calls[0][:2] == ["docker", "run"]
    assert calls[1][:2] == ["docker", "commit"]
    assert calls[2][:2] == ["docker", "rm"]


def test_run_setup_and_commit_raises_and_cleans_up_on_failure(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1 if cmd[1] == "run" else 0)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    with pytest.raises(UserError):
        container._run_setup_and_commit(
            [], ["echo hi"], "cfengine-cli-test-agent:latest"
        )

    assert calls[0][:2] == ["docker", "run"]
    container_name = calls[0][3]
    assert calls[1] == ["docker", "rm", "-f", container_name]
    assert not any(c[:2] == ["docker", "commit"] for c in calls)


def test_remove_image_calls_docker_rmi(monkeypatch):
    calls = []
    monkeypatch.setattr(
        container.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
    )

    container._remove_image("some-tag")

    assert calls == [["docker", "rmi", "some-tag"]]


# ---------------------------------------------------------------------------
# run_in_container
# ---------------------------------------------------------------------------


def test_run_in_container_returns_agent_returncode_when_no_asserts(
    no_real_docker, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        container,
        "run_and_scan_asserts",
        lambda cmd: DeployResults(returncode=3, passed=0, failed=0),
    )
    assert container.run_in_container(str(tmp_path)) == 3


def test_run_in_container_reports_assert_totals_when_present(
    no_real_docker, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        container,
        "run_and_scan_asserts",
        lambda cmd: DeployResults(returncode=0, passed=1, failed=1),
    )
    assert container.run_in_container(str(tmp_path)) == 1


# ---------------------------------------------------------------------------
# run_files_in_container
# ---------------------------------------------------------------------------


def test_run_files_in_container_raises_without_test_files(no_real_docker, tmp_path):
    lib_file = tmp_path / "lib.cf"
    lib_file.write_text("bundle agent lib {}\n")
    with pytest.raises(UserError):
        container.run_files_in_container([str(lib_file)])


def test_run_files_in_container_allows_duplicate_bundle_names_across_files(
    no_real_docker, monkeypatch, tmp_path, capsys
):
    """Two files declaring the same bundle name is fine now -- each gets its
    own container -- as long as the report can still tell them apart, which
    it does by file path rather than bundle name."""
    a = tmp_path / "test_a.cf"
    a.write_text('bundle agent same\n{\n  assert:\n    "x" pass => "true";\n}\n')
    b = tmp_path / "test_b.cf"
    b.write_text('bundle agent same\n{\n  assert:\n    "x" pass => "true";\n}\n')

    def fake_run_and_parse(mount_args, script, test_names, image=None):
        name = test_names[0]
        return RunResults(verdicts={}, completed={name}, marks_by_test={name: ["."]})

    monkeypatch.setattr(container, "run_and_parse", fake_run_and_parse)

    rc = container.run_files_in_container([str(a), str(b)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "test_a.cf::same  ." in out
    assert "test_b.cf::same  ." in out


def test_run_files_in_container_reports_each_bundle_in_a_file_separately(
    no_real_docker, monkeypatch, tmp_path, capsys
):
    """One test_*.cf file may declare several independent check bundles --
    each still gets its own CFTEST-START/DONE pair and its own report row,
    all within that one file's single container run."""
    test_file = tmp_path / "test_checks.cf"
    test_file.write_text(
        'bundle agent test_numbers\n{\n  assert:\n    "x" pass => "true";\n}\n'
        '\nbundle agent test_strings\n{\n  assert:\n    "y" pass => "true";\n}\n'
    )

    seen_test_names = []

    def fake_run_and_parse(mount_args, script, test_names, image=None):
        seen_test_names.append(test_names)
        return RunResults(
            verdicts={},
            completed=set(test_names),
            marks_by_test={name: ["."] for name in test_names},
        )

    monkeypatch.setattr(container, "run_and_parse", fake_run_and_parse)

    rc = container.run_files_in_container([str(test_file)])
    out = capsys.readouterr().out

    assert rc == 0
    assert len(seen_test_names) == 1  # one container run for the whole file
    assert len(seen_test_names[0]) == 2  # both bundles passed to that one run
    assert "test_checks.cf::test_numbers  ." in out
    assert "test_checks.cf::test_strings  ." in out


def test_run_files_in_container_sets_up_once_and_reports_per_test(
    no_real_docker, monkeypatch, tmp_path, capsys
):
    test_a = tmp_path / "test_a.cf"
    test_a.write_text('bundle agent test_a\n{\n  assert:\n    "x" pass => "true";\n}\n')
    test_b = tmp_path / "test_b.cf"
    test_b.write_text('bundle agent test_b\n{\n  assert:\n    "x" pass => "true";\n}\n')
    lib = tmp_path / "lib.cf"
    lib.write_text("bundle agent lib {}\n")

    setup_calls = []
    monkeypatch.setattr(
        container,
        "_run_setup_and_commit",
        lambda mount_args, setup_commands, image_tag: setup_calls.append(1)
        or "prepped-image:test",
    )
    removed_images = []
    monkeypatch.setattr(
        container, "_remove_image", lambda tag: removed_images.append(tag)
    )

    def fake_run_and_parse(mount_args, script, test_names, image=None):
        assert image == "prepped-image:test"
        name = test_names[0]
        outcome = "PASS" if "test_a.cf" in name else "FAIL"
        return RunResults(
            verdicts={(name, "check"): (outcome, "" if outcome == "PASS" else "boom")},
            completed={name},
            marks_by_test={name: ["." if outcome == "PASS" else "F"]},
        )

    monkeypatch.setattr(container, "run_and_parse", fake_run_and_parse)

    rc = container.run_files_in_container([str(test_a), str(test_b), str(lib)])
    out = capsys.readouterr().out

    assert len(setup_calls) == 1  # setup only happened once, shared by both tests
    assert removed_images == ["prepped-image:test"]
    assert rc == 1
    assert "test_a.cf::test_a  ." in out
    assert "test_b.cf::test_b  F" in out
    assert "FAIL   " in out and "test_b.cf::test_b::check  ->  boom" in out
