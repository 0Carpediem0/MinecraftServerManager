import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msm.core import Catalog, Manager, ManagerError, Mods, Network, encode_properties, safe_name


class FakeNetwork:
    def __init__(self):
        self.responses = {}
        self.files = {}
        self.calls = []

    def json(self, url, **params):
        self.calls.append((url, params))
        return self.responses[url]

    def bytes(self, url):
        return self.files[url]

    def download(self, url, target, hashes=None, log=lambda _: None):
        Path(target).write_bytes(self.files[url])


def jar_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("fabric.mod.json", "{}")
    return out.getvalue()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.net = FakeNetwork()
        self.log = []
        self.m = Manager(self.tmp.name, self.net, self.log.append)
        self.p = self.m.create("Тест", "1.20.1", "Fabric")
        self.mods = Mods(self.m)

    def tearDown(self):
        if self.m.process and self.m.process.poll() is None:
            self.m.process.kill()
            self.m.process.wait()
        self.tmp.cleanup()

    def mod(self, pid, deps=None, version="v1", server="required", game="1.20.1", loader="fabric"):
        from msm.core import MODRINTH
        data = jar_bytes()
        f = {"filename": pid + ".jar", "url": "https://example.test/" + pid, "hashes": {"sha512": hashlib.sha512(data).hexdigest()}, "primary": True}
        v = dict(id=pid + version, project_id=pid, version_number=version, version_type="release", date_published="2025-01-01", game_versions=[game], loaders=[loader], files=[f], dependencies=deps or [])
        self.net.responses[MODRINTH + "/project/" + pid] = dict(id=pid, title=pid, server_side=server, project_type="mod")
        self.net.responses[MODRINTH + "/project/" + pid + "/version"] = [v]
        self.net.responses[MODRINTH + "/version/" + v["id"]] = v
        self.net.files[f["url"]] = data
        return v

    def test_versions_sorted_by_date_not_name(self):
        from msm.core import MANIFEST
        self.net.responses[MANIFEST] = {"versions": [dict(id="1.9", type="release", releaseTime="2016"), dict(id="1.10", type="release", releaseTime="2017"), dict(id="26.3-rc-1", type="snapshot", releaseTime="2026")]}
        self.assertEqual([v["id"] for v in self.m.catalog.versions()], ["1.10", "1.9"])
        self.assertEqual(self.m.catalog.versions(True)[0]["id"], "26.3-rc-1")

    def test_profiles_isolated(self):
        p2 = self.m.create("Тест", "1.20.1", "Vanilla")
        self.assertNotEqual(self.m.folder(p2), self.m.folder(self.p))
        self.assertEqual(len(self.m.profiles()), 2)

    def test_backup_contains_world_and_profile(self):
        world = self.m.folder(self.p) / "world"
        world.mkdir()
        (world / "level.dat").write_bytes(b"world")
        backup = self.m.backup(self.p)
        with zipfile.ZipFile(backup) as z:
            self.assertEqual(z.read("world/level.dat"), b"world")
            self.assertIn("profile.json", z.namelist())

    def test_eula_never_implicit(self):
        self.p.update(installed=True, java_major=17, launch={"jar": "server.jar"})
        self.assertFalse(self.m.eula(self.p))
        with self.assertRaisesRegex(ManagerError, "EULA"):
            self.m.run(self.p)

    def test_synthetic_explicit_eula_toggle(self):
        self.m.accept_eula(self.p, True)
        self.assertTrue(self.m.eula(self.p))
        self.m.accept_eula(self.p, False)
        self.assertFalse(self.m.eula(self.p))

    def test_properties_backup_before_write(self):
        f = self.m.folder(self.p) / "server.properties"
        f.write_text("motd=old")
        self.m.settings(self.p, "java", 3, "motd=Привет\n")
        self.assertIn("\\u041f", f.read_text())
        backup = next((self.m.root / "backups" / self.p["id"]).glob("*.zip"))
        with zipfile.ZipFile(backup) as z:
            self.assertEqual(z.read("server.properties"), b"motd=old")

    def test_no_shell_in_launch_command(self):
        self.p["launch"] = {"jar": "a b.jar"}
        self.assertEqual(self.m.command(self.p), ["java", "-Xms512M", "-Xmx4G", "-jar", "a b.jar", "nogui"])

    def test_real_subprocess_logs_stop_and_running_mutation_guard(self):
        self.p.update(installed=True, java_major=17)
        self.m.accept_eula(self.p, True)  # Synthetic process only; no Minecraft EULA acceptance.
        command = [sys.executable, "-u", "-c", "import sys; print('Done (0.1s)!'); s=sys.stdin.readline(); print('saved' if s=='stop\\n' else 'bad');"]
        with patch("msm.core.check_java"), patch.object(self.m, "command", return_value=command):
            self.m.run(self.p)
        limit = time.monotonic() + 5
        while self.m.state != "Работает" and time.monotonic() < limit:
            time.sleep(0.02)
        self.assertEqual(self.m.state, "Работает")
        with self.assertRaises(ManagerError):
            self.m.backup(self.p)
        with self.assertRaises(ManagerError):
            self.m.settings(self.p, "java", 2, "")
        self.m.stop()
        self.m.process.wait(timeout=5)
        while self.m.running_id and time.monotonic() < limit:
            time.sleep(0.02)
        self.assertIn("saved", self.log)
        self.assertEqual(self.m.state, "Остановлен")
        self.assertIn("saved", (self.m.folder(self.p) / "manager-console.log").read_text())

    def test_dependency_recursion(self):
        self.mod("api")
        self.mod("mod", [dict(dependency_type="required", project_id="api", version_id=None)])
        self.assertEqual({x["project_id"] for x in self.mods.plan(self.p, "mod")}, {"api", "mod"})

    def test_client_only_rejected_even_as_dependency(self):
        self.mod("client", server="unsupported")
        self.mod("mod", [dict(dependency_type="required", project_id="client", version_id=None)])
        with self.assertRaisesRegex(ManagerError, "серверной"):
            self.mods.plan(self.p, "mod")

    def test_pinned_dependency_wrong_mc_rejected(self):
        v = self.mod("api", game="1.21")
        self.mod("mod", [dict(dependency_type="required", project_id="api", version_id=v["id"])])
        with self.assertRaisesRegex(ManagerError, "несовместима"):
            self.mods.plan(self.p, "mod")

    def test_wrong_loader_rejected(self):
        self.mod("mod", loader="forge")
        with self.assertRaisesRegex(ManagerError, "несовместима"):
            self.mods.plan(self.p, "mod")

    def test_incompatibility_rejected(self):
        self.mod("api")
        self.mod("mod", [dict(dependency_type="required", project_id="api", version_id=None), dict(dependency_type="incompatible", project_id="api", version_id=None)])
        with self.assertRaisesRegex(ManagerError, "несовместимость"):
            self.mods.plan(self.p, "mod")

    def test_optional_dependency_not_installed(self):
        self.mod("mod", [dict(dependency_type="optional", project_id="missing", version_id=None)])
        self.assertEqual(len(self.mods.plan(self.p, "mod")), 1)

    def test_external_dependency_rejected(self):
        self.mod("mod", [dict(dependency_type="required", project_id=None, version_id=None, file_name="external.jar")])
        with self.assertRaisesRegex(ManagerError, "внешнюю"):
            self.mods.plan(self.p, "mod")

    def test_dependency_cycle_terminates(self):
        self.mod("a", [dict(dependency_type="required", project_id="b", version_id=None)])
        self.mod("b", [dict(dependency_type="required", project_id="a", version_id=None)])
        self.assertEqual(len(self.mods.plan(self.p, "a")), 2)

    def test_mod_install_remove_and_dependency_guard(self):
        self.p["installed"] = True
        self.mod("api")
        self.mod("mod", [dict(dependency_type="required", project_id="api", version_id=None)])
        self.mods.install(self.p, self.mods.plan(self.p, "mod"))
        self.assertEqual(len(self.mods.lock(self.p)), 2)
        with self.assertRaisesRegex(ManagerError, "требуется"):
            self.mods.remove(self.p, "api")
        self.mods.remove(self.p, "mod")
        self.mods.remove(self.p, "api")
        self.assertEqual(self.mods.lock(self.p), {})

    def test_failed_mod_download_leaves_no_partial_install(self):
        self.p["installed"] = True
        self.mod("api")
        self.mod("mod", [dict(dependency_type="required", project_id="api", version_id=None)])
        plan = self.mods.plan(self.p, "mod")
        del self.net.files["https://example.test/api"]
        with self.assertRaises(KeyError):
            self.mods.install(self.p, plan)
        self.assertEqual(list((self.m.folder(self.p) / "mods").iterdir()), [])
        self.assertEqual(self.mods.lock(self.p), {})

    def test_preexisting_mod_not_overwritten(self):
        self.p["installed"] = True
        self.mod("mod")
        folder = self.m.folder(self.p) / "mods"
        folder.mkdir()
        (folder / "mod.jar").write_bytes(b"manual")
        with self.assertRaisesRegex(ManagerError, "уже существует"):
            self.mods.install(self.p, self.mods.plan(self.p, "mod"))
        self.assertEqual((folder / "mod.jar").read_bytes(), b"manual")

    def test_path_traversal_rejected(self):
        for name in ("../evil.jar", "a/b.jar", "a\\b.jar", "..", "C:evil.jar"):
            with self.assertRaises(ManagerError):
                safe_name(name)

    def test_download_hash_mismatch_removes_partial(self):
        target = Path(self.tmp.name) / "bad.jar"
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"bad")):
            with self.assertRaisesRegex(ManagerError, "сумма"):
                Network().download("https://example.test/file", target, {"sha1": "0" * 40})
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name("bad.jar.part").exists())

    def test_java_requirement_blocks_wrong_runtime(self):
        from msm.core import check_java
        with patch("msm.core.java_major", return_value=8):
            with self.assertRaisesRegex(ManagerError, "Java 25"):
                check_java("java", 25)

    def test_install_checksum_and_commit(self):
        self.p["loader"] = "Vanilla"
        blob = b"server fixture"
        meta = dict(javaVersion={"majorVersion": 17}, downloads={"server": dict(url="https://example.test/server", sha1=hashlib.sha1(blob).hexdigest())})
        raw = json.dumps(meta).encode()
        self.net.files["https://example.test/meta"] = raw
        self.net.files["https://example.test/server"] = blob
        entry = dict(id="1.20.1", url="https://example.test/meta", sha1=hashlib.sha1(raw).hexdigest())
        with patch("msm.core.check_java"):
            self.m.install(self.p, entry)
        self.assertTrue(self.p["installed"])
        self.assertFalse(self.m.eula(self.p))
        self.assertEqual((self.m.folder(self.p) / "server.jar").read_bytes(), blob)
        with self.assertRaisesRegex(ManagerError, "уже установлен"):
            self.m.install(self.p, entry)

    def test_forge_old_version_is_explicitly_unsupported(self):
        with self.assertRaisesRegex(ManagerError, "1.17"):
            self.m.catalog.loader_build("Forge", "1.12.2")


if __name__ == "__main__":
    unittest.main()
