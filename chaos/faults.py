"""Fault types. Each fault has inject(target) and restore(target)."""

import subprocess

DOCKER_TIMEOUT_S = 30  # a hung docker call must not hang the controller
PUMBA_IMAGE = "gaiaadm/pumba:0.9.0"
TC_IMAGE = "gaiadocker/iproute2"


def _docker(*args):
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True,
                   timeout=DOCKER_TIMEOUT_S)


class ContainerStop:
    """Stop a container, then start it again on restore."""

    def __init__(self, exp=None):
        pass

    def inject(self, target):
        _docker("stop", "--time", "1", target)

    def restore(self, target):
        _docker("start", target)


class NetworkDelay:
    """Inject network delay using Pumba with graceful netem cleanup.

    Takes delay_ms, jitter_ms, and duration_s from experiment configuration.
    Runs Pumba in background. On restore, uses docker stop (SIGTERM) so Pumba
    removes the tc netem qdisc from the target container before removal.
    """

    def __init__(self, exp=None):
        exp = exp or {}
        self.delay_ms = exp.get("delay_ms", 100)
        self.jitter_ms = exp.get("jitter_ms", 10)
        # Ensure duration_s matches experiment duration (at least 1s)
        self.duration_s = max(1, int(exp.get("duration_s", 30)))

    def pumba_name(self, target):
        return f"pumba_delay_{target}"

    def inject(self, target):
        cname = self.pumba_name(target)
        # Remove any lingering pumba container for this target first
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=DOCKER_TIMEOUT_S)
        _docker(
            "run", "-d",
            "--name", cname,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            PUMBA_IMAGE,
            "netem",
            "--duration", f"{self.duration_s}s",
            "--tc-image", TC_IMAGE,
            "delay",
            "--time", str(self.delay_ms),
            "--jitter", str(self.jitter_ms),
            target,
        )

    def restore(self, target):
        cname = self.pumba_name(target)
        # 1. Graceful stop sends SIGTERM allowing Pumba to delete tc netem rule
        try:
            _docker("stop", "--timeout", "5", cname)
        except Exception:
            pass
        # 2. Remove stopped Pumba container
        try:
            _docker("rm", "-f", cname)
        except Exception:
            pass


FAULTS = {
    "container_stop": ContainerStop,
    "network_delay": NetworkDelay,
}
