import os
import shutil
import subprocess

from cfengine_cli.utils import UserError

_DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker", "test-agent")
_IMAGE_TAG = "cfengine-cli-test-agent:latest"


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise UserError(
            "`cfengine test` runs the built policy set inside a Docker container -- install Docker to use it."
        )


def _ensure_image_built() -> None:
    result = subprocess.run(
        ["docker", "build", "-q", "-t", _IMAGE_TAG, _DOCKERFILE_DIR]
    )
    if result.returncode != 0:
        raise UserError("Failed to build the cfengine-test Docker image.")


def run_in_container(masterfiles_dir: str) -> int:
    """
    Runs cf-agent against the given built masterfiles directory inside a container
    """
    require_docker()
    _ensure_image_built()

    abs_masterfiles = os.path.abspath(masterfiles_dir)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{abs_masterfiles}:/mnt/masterfiles:ro",
            _IMAGE_TAG,
            "sh",
            "-c",
            "rm -rf /var/cfengine/inputs "
            "&& cp -r /mnt/masterfiles /var/cfengine/inputs "
            "&& /var/cfengine/bin/cf-agent -KIf update.cf "
            "&& /var/cfengine/bin/cf-agent -KI",
        ]
    )
    return result.returncode
