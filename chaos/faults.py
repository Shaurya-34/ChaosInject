"""Fault types. Each fault has inject(target) and restore(target)."""

import subprocess

DOCKER_TIMEOUT_S = 30  # a hung docker call must not hang the controller


def _docker(*args):
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True,
                   timeout=DOCKER_TIMEOUT_S)


class ContainerStop:
    """Stop a container, then start it again on restore."""

    def inject(self, target):
        _docker("stop", "--time", "1", target)

    def restore(self, target):
        _docker("start", target)


FAULTS = {
    "container_stop": ContainerStop,
}
