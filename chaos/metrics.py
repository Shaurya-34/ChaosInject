"""Prometheus metrics for one controller run, pushed to a Pushgateway (CHAOS_PUSHGATEWAY,
default http://127.0.0.1:9091) once a second from a background thread.

The controller is a short batch job, so it pushes instead of being scraped; Prometheus scrapes
the Pushgateway (sandbox/observability). Observability only: the controller's own probes make
every safety decision; nothing here can stop, start or change a run. If the Pushgateway is down,
the run continues without metrics. Standard library only.
"""

import threading
import time
import urllib.request

PHASES = ("baseline", "fault", "recovery", "done")


def _esc(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


class Metrics:
    def __init__(self, exp):
        self._lock = threading.Lock()
        self.labels = {"experiment": exp.get("name"), "target": exp.get("target"), "fault": exp.get("fault")}
        self.phase = "baseline"
        self.fault_active = 0
        self.last = None
        self.counts = {}  # (phase, result) -> probes
        self.stop_reason = None
        self.recovered = None
        self.recovery_s = None

    def sample(self, phase, sample):
        with self._lock:
            self.phase = phase
            self.last = sample
            key = (phase, "ok" if sample.get("ok") else "fail")
            self.counts[key] = self.counts.get(key, 0) + 1

    def set_fault(self, active):
        with self._lock:
            self.fault_active = int(active)
            if active:
                self.phase = "fault"

    def finish(self, record):
        with self._lock:
            self.phase = "done"
            self.fault_active = 0
            self.stop_reason = record.get("stop_reason")
            self.recovered = record.get("recovered")
            self.recovery_s = record.get("recovery_s")

    def render(self):
        with self._lock:
            base = ",".join(f'{k}="{_esc(v)}"' for k, v in self.labels.items())
            lines = []

            def gauge(name, help_, value, extra=""):
                lines.append(f"# HELP {name} {help_}\n# TYPE {name} gauge")
                labels = base + ("," + extra if extra else "")
                lines.append(f"{name}{{{labels}}} {value}")

            gauge("chaos_fault_active", "1 while the fault is injected.", self.fault_active)
            lines.append("# HELP chaos_phase Current controller phase (one-hot).\n# TYPE chaos_phase gauge")
            for p in PHASES:
                lines.append(f'chaos_phase{{{base},phase="{p}"}} {int(self.phase == p)}')
            if self.last is not None:
                gauge("chaos_probe_latency_ms", "Latency of the latest probe.", self.last.get("latency_ms"))
                gauge("chaos_probe_ok", "1 if the latest probe got HTTP status < 500.", int(bool(self.last.get("ok"))))
                status = self.last.get("status")
                gauge("chaos_probe_status", "HTTP status of the latest probe; -1 on timeout or error.",
                      -1 if status is None else status)
            lines.append("# HELP chaos_probes_total Probes by phase and result.\n# TYPE chaos_probes_total counter")
            for (phase, result), n in sorted(self.counts.items()):
                lines.append(f'chaos_probes_total{{{base},phase="{phase}",result="{result}"}} {n}')
            if self.phase == "done":
                gauge("chaos_safety_stop", "1 if a safety rule ended the fault early.",
                      int(str(self.stop_reason or "").startswith("safety:")))
                gauge("chaos_recovered", "1 if the app recovered after restore.", int(bool(self.recovered)))
                if self.recovery_s is not None:
                    gauge("chaos_recovery_seconds", "Seconds from restore to the first ok probe.", self.recovery_s)
                gauge("chaos_stop_info", "Why the fault phase ended.", 1, f'reason="{_esc(self.stop_reason)}"')
            return "\n".join(lines) + "\n"


class Pusher:
    """Pushes metrics.render() to the Pushgateway every `interval` seconds until stop()."""

    def __init__(self, metrics, gateway, interval=1.0, timeout=0.5):
        self.metrics = metrics
        self.url = gateway.rstrip("/") + "/metrics/job/chaos_controller"
        self.interval, self.timeout = interval, timeout
        self.pushes = self.failures = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _request(self, method, body=None):
        req = urllib.request.Request(self.url, data=body, method=method,
                                     headers={"Content-Type": "text/plain; version=0.0.4"})
        urllib.request.urlopen(req, timeout=self.timeout).close()

    def push(self):
        try:
            self._request("PUT", self.metrics.render().encode())
            self.pushes += 1
        except Exception:
            self.failures += 1

    def _loop(self):
        while not self._stop.is_set():
            self.push()
            self._stop.wait(self.interval)

    def start(self):
        self._thread.start()
        return self

    def stop(self, linger=0.0):
        """Push the final state, keep it up for `linger` seconds, then delete it from the gateway."""
        self._stop.set()
        self._thread.join(timeout=self.timeout + 1)
        self.push()
        time.sleep(linger)  # long enough for Prometheus to scrape the final state
        try:
            self._request("DELETE")  # don't leave a finished run's values looking current
        except Exception:
            pass
