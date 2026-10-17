"""Fault types. Each fault has inject(target) and restore(target)."""

import subprocess
import time

DOCKER_TIMEOUT_S = 30  # a hung docker call must not hang the controller
INJECT_TIMEOUT_S = 20  # wait up to this long for netem rule to appear
PUMBA_IMAGE = "gaiaadm/pumba:0.9.0"
TC_IMAGE = "gaiadocker/iproute2:3.3"


class CleanupError(Exception):
    """Raised when fault cleanup / verification fails."""
    pass


class InjectError(Exception):
    """Raised when a fault never becomes active."""
    pass


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
    """Inject network delay using Pumba with graceful netem cleanup and verification.

    Takes delay_ms, jitter_ms, and duration_s from experiment configuration.
    Runs Pumba in background. On restore, uses docker stop (SIGTERM) so Pumba
    removes the tc netem qdisc from the target container, then verifies netem is gone.
    """

    def __init__(self, exp=None):
        exp = exp or {}
        self.delay_ms = exp.get("delay_ms", 100)
        self.jitter_ms = exp.get("jitter_ms", 10)
        # Ensure duration_s matches experiment duration (at least 1s)
        self.duration_s = max(1, int(exp.get("duration_s", 30)))

    kind = "delay"

    def pumba_name(self, target):
        return f"pumba_{self.kind}_{target}"

    def netem_args(self):
        return ["delay", "--time", str(self.delay_ms), "--jitter", str(self.jitter_ms)]

    def _qdisc(self, target):
        res = subprocess.run([
            "docker", "run", "--rm",
            f"--net=container:{target}",
            "--cap-add", "NET_ADMIN",
            "--entrypoint", "",
            TC_IMAGE,
            "tc", "qdisc", "show", "dev", "eth0",
        ], capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
        if res.returncode != 0:
            raise CleanupError(f"tc qdisc show failed on {target}: {res.stderr.strip()}")
        return res.stdout

    def inject(self, target):
        cname = self.pumba_name(target)
        # Remove any lingering pumba container for this target first
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=DOCKER_TIMEOUT_S)
        # Give Pumba safety duration headroom so it stays active until controller restores
        pumba_duration = self.duration_s + 60
        _docker(
            "run", "-d",
            "--name", cname,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            PUMBA_IMAGE,
            "netem",
            "--duration", f"{pumba_duration}s",
            "--tc-image", TC_IMAGE,
            *self.netem_args(),
            target,
        )
        deadline = time.monotonic() + INJECT_TIMEOUT_S
        while "netem" not in self._qdisc(target):
            if time.monotonic() > deadline:
                raise InjectError(f"netem {self.kind} never appeared on {target} within {INJECT_TIMEOUT_S}s")
            time.sleep(1)

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

        # 3. Verify netem cleanup on target container
        out = self._qdisc(target)
        if "netem" in out:
            # If netem is still there, remove it explicitly using _docker
            try:
                _docker(
                    "run", "--rm",
                    f"--net=container:{target}",
                    "--cap-add", "NET_ADMIN",
                    "--entrypoint", "",
                    TC_IMAGE,
                    "tc", "qdisc", "del", "dev", "eth0", "root",
                )
            except Exception as e:
                raise CleanupError(f"Failed to remove netem qdisc from {target}: {e}") from e

            # Verify it is gone
            out2 = self._qdisc(target)
            if "netem" in out2:
                raise CleanupError(f"netem qdisc still present on {target} after removal")


class PacketLoss(NetworkDelay):
    """Drop a percentage of the target's outgoing packets (Pumba netem loss).

    Takes loss_percent and duration_s from the experiment. Inject verification and
    cleanup are the same as NetworkDelay.
    """

    kind = "loss"

    def __init__(self, exp=None):
        super().__init__(exp)
        self.loss_percent = (exp or {}).get("loss_percent", 20)
        if not 0 < self.loss_percent <= 100:
            raise ValueError(f"loss_percent must be in (0, 100], got {self.loss_percent}")

    def netem_args(self):
        return ["loss", "--percent", str(self.loss_percent)]


FAULTS = {
    "container_stop": ContainerStop,
    "network_delay": NetworkDelay,
    "packet_loss": PacketLoss,
}
