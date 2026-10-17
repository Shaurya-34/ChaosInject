import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox/hotel" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare")
smoke = load("smoke")


class HotelSetupTests(unittest.TestCase):
    def test_all_services_are_isolated_pinned_and_do_not_restart(self):
        names = prepare.APP_SERVICES | {"consul", "jaeger"}
        names |= {"mongodb-" + x for x in ("geo", "rate", "profile", "recommendation", "reservation", "user", "review", "attractions")}
        names |= {"memcached-" + x for x in ("rate", "profile", "reserve", "review")}
        services = {name: {"image": "unlocked:latest", "restart": "always",
                           "deploy": {"restart_policy": {"condition": "any"}},
                           "ports": ["5000:5000"], "build": ".", "configs": []} for name in names}
        services["review"]["depends_on"] = ["mongodb-review", "memcached-review", "consul"]
        model = {"services": services, "volumes": {"geo": {"name": "upstream_geo"}},
                 "networks": {"default": {"name": "upstream_default"}}}
        lock = json.loads((ROOT / "sandbox/hotel/images.lock.json").read_text())
        output = prepare.configure(model, lock, Path("/tmp/source"))
        self.assertEqual(set(output["services"]), names)
        self.assertEqual(output["volumes"]["geo"]["name"], "hotel-sandbox_geo")
        self.assertEqual(output["networks"]["default"]["name"], "hotel-sandbox_default")
        for name, service in output["services"].items():
            self.assertEqual(service["restart"], "no")
            self.assertNotIn("deploy", service)
            self.assertNotIn(":latest", service["image"])
            self.assertEqual("build" in service, name in {"review", "attractions"})
            if name not in {"review", "attractions"}:
                self.assertIn("@sha256:", service["image"])
            for port in service.get("ports", []):
                self.assertEqual(port["host_ip"], "127.0.0.1")
        self.assertEqual(output["services"]["review"]["depends_on"]["mongodb-review"]["condition"], "service_healthy")
        self.assertEqual(output["services"]["frontend"]["platform"], "linux/amd64")

    def test_fix_rate_builds_rate_from_patched_source(self):
        names = prepare.APP_SERVICES | {"consul", "jaeger"}
        names |= {"mongodb-" + x for x in ("geo", "rate", "profile", "recommendation", "reservation", "user", "review", "attractions")}
        names |= {"memcached-" + x for x in ("rate", "profile", "reserve", "review")}
        model = {"services": {name: {"image": "x"} for name in names}}
        lock = json.loads((ROOT / "sandbox/hotel/images.lock.json").read_text())
        rate = prepare.configure(model, lock, Path("/tmp/source"), fix_rate=True)["services"]["rate"]
        self.assertEqual(rate["build"]["dockerfile"], "Dockerfile.rate-fix")
        self.assertTrue(rate["image"].endswith("-cache-fallback"))
        self.assertNotIn("platform", rate)
        self.assertNotIn("build", prepare.configure(model, lock, Path("/tmp/source"))["services"]["rate"])

    def test_rate_patch_replaces_the_panic_and_refuses_unknown_source(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "services/rate").mkdir(parents=True)
            upstream = "".join(original + "...\n" for original, _ in prepare.RATE_EDITS)
            (source / "services/rate/server.go").write_text(upstream)
            prepare.write_rate_fix(source, "FROM go\nCOPY services/ services/\nRUN build\n")
            patched = (source / "sandbox-patches/rate/server.go").read_text()
            self.assertNotIn("log.Panic", patched)
            self.assertIn("falling back to MongoDB", patched)
            self.assertIn("markCacheDown()", patched)
            self.assertIn("func markCacheDown", (source / "sandbox-patches/rate/cache_breaker.go").read_text())
            self.assertIn("COPY sandbox-patches/rate/ services/rate/", (source / "Dockerfile.rate-fix").read_text())
            (source / "services/rate/server.go").write_text("changed upstream\n")
            with self.assertRaises(SystemExit):
                prepare.write_rate_fix(source, "COPY services/ services/\n")

    def test_empty_search_is_not_a_pass(self):
        with patch.object(smoke, "request", return_value=(200, '{"type":"FeatureCollection","features":[]}')):
            with self.assertRaises(ValueError):
                smoke.features("/hotels", {})

    def test_login_checks_rejection_of_bad_credentials(self):
        with patch.object(smoke, "request", side_effect=[
            (200, '{"message":"Login successfully!"}'),
            (200, '{"message":"Login successfully!"}'),
        ]):
            with self.assertRaisesRegex(ValueError, "Invalid login"):
                smoke.login()


if __name__ == "__main__":
    unittest.main()
