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


def test_resolve_dockerfile_dir_defaults_to_builtin():
    assert container._resolve_dockerfile_dir(None) == container._DOCKERFILE_DIR


def test_resolve_dockerfile_dir_accepts_a_directory(tmp_path):
    assert container._resolve_dockerfile_dir(str(tmp_path)) == str(tmp_path)


def test_resolve_dockerfile_dir_accepts_a_dockerfile_path(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM debian:13-slim\n")
    assert container._resolve_dockerfile_dir(str(dockerfile)) == str(tmp_path)


def test_image_tag_for_builtin_dockerfile_is_the_stable_tag():
    assert container._image_tag_for(container._DOCKERFILE_DIR) == container._IMAGE_TAG


def test_image_tag_for_custom_dockerfile_is_distinct_and_deterministic(tmp_path):
    tag_a = container._image_tag_for(str(tmp_path))
    tag_b = container._image_tag_for(str(tmp_path))
    assert tag_a == tag_b
    assert tag_a != container._IMAGE_TAG
    assert tag_a.startswith("cfengine-cli-test-agent-custom-")


# ---------------------------------------------------------------------------
# _ensure_image_built
# ---------------------------------------------------------------------------


def test_ensure_image_built_passes_no_cache_only_when_rebuilding(monkeypatch):
    seen_cmds = []
    monkeypatch.setattr(
        container.subprocess,
        "run",
        lambda cmd: seen_cmds.append(cmd)
        or container.subprocess.CompletedProcess(cmd, 0),
    )

    container._ensure_image_built("/some/dir", "some-tag:latest", rebuild=False)
    container._ensure_image_built("/some/dir", "some-tag:latest", rebuild=True)

    assert "--no-cache" not in seen_cmds[0]
    assert "--no-cache" in seen_cmds[1]


def test_ensure_image_built_raises_on_build_failure(monkeypatch):
    monkeypatch.setattr(
        container.subprocess,
        "run",
        lambda cmd: container.subprocess.CompletedProcess(cmd, 1),
    )
    with pytest.raises(UserError):
        container._ensure_image_built("/some/dir", "some-tag:latest", rebuild=False)


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
