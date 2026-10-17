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

    def test_fix_cache_builds_the_patched_services_from_source(self):
        names = prepare.APP_SERVICES | {"consul", "jaeger"}
        names |= {"mongodb-" + x for x in ("geo", "rate", "profile", "recommendation", "reservation", "user", "review", "attractions")}
        names |= {"memcached-" + x for x in ("rate", "profile", "reserve", "review")}
        model = {"services": {name: {"image": "x"} for name in names}}
        lock = json.loads((ROOT / "sandbox/hotel/images.lock.json").read_text())
        services = prepare.configure(model, lock, Path("/tmp/source"), fix_cache=True)["services"]
        self.assertEqual(set(prepare.CACHE_FIXES), {"rate", "profile", "reservation"})
        for name in prepare.CACHE_FIXES:
            self.assertEqual(services[name]["build"]["dockerfile"], "Dockerfile.cache-fix")
            self.assertEqual(services[name]["image"].split("/")[1].split(":")[0], name)
            self.assertTrue(services[name]["image"].endswith("-cache-fallback"))
            self.assertNotIn("platform", services[name])
        self.assertNotIn("build", services["search"])
        self.assertNotIn("build", prepare.configure(model, lock, Path("/tmp/source"))["services"]["rate"])

    def test_cache_patch_replaces_the_panic_and_refuses_unknown_source(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            for name, edits in prepare.CACHE_FIXES.items():
                (source / "services" / name).mkdir(parents=True)
                (source / "services" / name / "server.go").write_text("".join((o + "...\n") * n for o, _, n in edits))
            prepare.write_cache_fix(source, "FROM go\nCOPY services/ services/\nRUN build\n")
            dockerfile = (source / "Dockerfile.cache-fix").read_text()
            for name in prepare.CACHE_FIXES:
                patched = (source / "sandbox-patches" / name / "server.go").read_text()
                self.assertNotIn("log.Panic", patched)
                if name == "reservation":  # every client call goes through the wrappers
                    self.assertNotIn("s.MemcClient.Get", patched)
                    self.assertNotIn("s.MemcClient.Set", patched)
                    self.assertIn("cacheGetMulti(s.MemcClient", patched)
                else:
                    self.assertIn("markCacheDown()", patched)
                breaker = (source / "sandbox-patches" / name / "cache_breaker.go").read_text()
                self.assertTrue(breaker.startswith("package " + name + "\n"))
                self.assertIn(f"COPY sandbox-patches/{name}/ services/{name}/", dockerfile)
            (source / "services/profile/server.go").write_text("changed upstream\n")
            with self.assertRaises(SystemExit):
                prepare.write_cache_fix(source, "COPY services/ services/\n")

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
