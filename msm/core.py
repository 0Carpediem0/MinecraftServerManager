from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC = "https://meta.fabricmc.net/v2"
FORGE = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEO = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
MODRINTH = "https://api.modrinth.com/v2"
UA = "MinecraftServerManager/0.1.0 (local desktop app)"


class ManagerError(Exception):
    pass


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def safe_name(name):
    if not name or name in (".", "..") or re.search(r'[\\/:\x00-\x1f]', name):
        raise ManagerError("Недопустимое имя файла в ответе каталога.")
    return name


class Network:
    def bytes(self, url):
        if not url.startswith("https://"):
            raise ManagerError("Загрузка разрешена только по HTTPS.")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
                return r.read()
        except (OSError, urllib.error.URLError) as e:
            raise ManagerError(f"Не удалось обратиться к {urllib.parse.urlparse(url).netloc}: {e}") from e

    def json(self, url, **params):
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return json.loads(self.bytes(url))
        except (ValueError, UnicodeError) as e:
            raise ManagerError("Сервис вернул некорректные данные. Повторите позже.") from e

    def download(self, url, target, hashes=None, log=lambda s: None):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        if not url.startswith("https://"):
            raise ManagerError("Загрузка разрешена только по HTTPS.")
        digests = {k: hashlib.new(k) for k in (hashes or {}) if k in ("sha1", "sha256", "sha512")}
        log(f"Загрузка: {target.name}")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r, part.open("wb") as out:
                count = 0
                report = 0
                while True:
                    block = r.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
                    count += len(block)
                    for d in digests.values():
                        d.update(block)
                    if count - report >= 16 * 1024 * 1024:
                        log(f"  {count // (1024 * 1024)} МБ")
                        report = count
            for k, d in digests.items():
                if d.hexdigest().lower() != hashes[k].lower():
                    raise ManagerError(f"Контрольная сумма {target.name} не совпала. Файл не установлен.")
            part.replace(target)
        except Exception as e:
            part.unlink(missing_ok=True)
            if isinstance(e, ManagerError):
                raise
            raise ManagerError(f"Ошибка загрузки {target.name}: {e}") from e


def numeric_version(v):
    return tuple(int(n) for n in re.findall(r"\d+", v))


class Catalog:
    def __init__(self, net=None):
        self.net = net or Network()

    def versions(self, snapshots=False):
        rows = self.net.json(MANIFEST)["versions"]
        return sorted((v for v in rows if v["type"] == "release" or snapshots and v["type"] == "snapshot"),
                      key=lambda v: v["releaseTime"], reverse=True)

    def metadata(self, entry):
        data = self.net.bytes(entry["url"])
        if entry.get("sha1") and hashlib.sha1(data).hexdigest() != entry["sha1"]:
            raise ManagerError("Контрольная сумма манифеста Minecraft не совпала.")
        meta = json.loads(data)
        if "server" not in meta.get("downloads", {}):
            raise ManagerError("Для этой версии Mojang не предоставляет серверный JAR.")
        return meta

    def loader_build(self, loader, mc):
        if loader == "Vanilla":
            return ""
        if loader == "Fabric":
            rows = self.net.json(f"{FABRIC}/versions/loader/{urllib.parse.quote(mc)}")
            stable = [r for r in rows if r["loader"].get("stable")]
            if not stable:
                raise ManagerError(f"Нет стабильного Fabric для Minecraft {mc}.")
            return stable[0]["loader"]["version"]
        if loader == "Forge":
            if numeric_version(mc) < (1, 17) or not mc.startswith("1."):
                raise ManagerError("Первая версия менеджера поддерживает Forge для Minecraft 1.17–1.21.x. Старые схемы установки пока не поддерживаются.")
            promos = self.net.json(FORGE)["promos"]
            build = promos.get(mc + "-recommended") or promos.get(mc + "-latest")
            if not build:
                raise ManagerError(f"Официальный Forge для Minecraft {mc} не найден.")
            return mc + "-" + build
        if loader == "NeoForge":
            # 1.20.2 -> 20.2.*, 1.21 -> 21.0.*; new numbering 26.1.2 -> 26.1.2.*.
            if mc.startswith("1."):
                bits = mc.split(".")
                if numeric_version(mc) < (1, 20, 2):
                    raise ManagerError("NeoForge поддерживается с Minecraft 1.20.2.")
                prefix = bits[1] + "." + (bits[2] if len(bits) > 2 else "0") + "."
            elif re.fullmatch(r"\d{2}\.\d+(?:\.\d+)?", mc):
                prefix = mc + (".0." if len(mc.split(".")) == 2 else ".")
            else:
                raise ManagerError("NeoForge для этой версии Minecraft не поддерживается.")
            root = ET.fromstring(self.net.bytes(NEO + "/maven-metadata.xml"))
            choices = [e.text for e in root.findall("./versioning/versions/version") if e.text and e.text.startswith(prefix) and re.fullmatch(r"\d+(?:\.\d+)+", e.text)]
            if not choices:
                raise ManagerError(f"Стабильный NeoForge для Minecraft {mc} не найден. Beta-сборки автоматически не устанавливаются.")
            return max(choices, key=numeric_version)
        raise ManagerError("Неизвестный тип сервера.")


def java_major(java):
    try:
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ManagerError("Java не найдена. Установите подходящую Java и укажите путь к bin/java в настройках.") from e
    m = re.search(r'version\s+"(?:1\.)?(\d+)', r.stderr + r.stdout)
    if r.returncode or not m:
        raise ManagerError("Не удалось определить версию Java. Укажите исполняемый файл java.")
    return int(m.group(1))


def check_java(java, required):
    got = java_major(java)
    if got != required:
        raise ManagerError(f"Нужна Java {required}; выбранная Java — {got}. Укажите путь к Java {required} (например, /usr/lib/jvm/.../bin/java).")


class Manager:
    def __init__(self, root=None, net=None, log=None):
        self.root = Path(root or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "minecraft-server-manager")
        self.root.mkdir(parents=True, exist_ok=True)
        self.net = net or Network()
        self.catalog = Catalog(self.net)
        self.log = log or (lambda s: None)
        self.process = None
        self.tunnel_process = None
        self.running_id = None
        self.state = "Остановлен"
        self.guard = threading.RLock()
        self.app_lock = None

    def acquire_lock(self):
        self.app_lock = (self.root / "manager.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.app_lock.seek(0)
                self.app_lock.write(b"0")
                self.app_lock.flush()
                self.app_lock.seek(0)
                msvcrt.locking(self.app_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.app_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.app_lock.close()
            self.app_lock = None
            raise ManagerError("Менеджер уже открыт для этой папки данных.") from e

    def profiles(self):
        result = []
        for path in sorted((self.root / "profiles").glob("*/profile.json")):
            try:
                result.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                self.log(f"Не удалось прочитать {path}: {e}")
        return result

    def folder(self, p):
        pid = p["id"]
        if not re.fullmatch(r"[0-9a-f]{32}", pid):
            raise ManagerError("Некорректный идентификатор профиля.")
        return self.root / "profiles" / pid

    def save(self, p):
        atomic_json(self.folder(p) / "profile.json", p)

    def create(self, name, mc, loader, java="java", ram=4):
        if not name.strip():
            raise ManagerError("Введите название профиля.")
        if loader not in ("Vanilla", "Fabric", "Forge", "NeoForge"):
            raise ManagerError("Неизвестный загрузчик.")
        p = dict(id=uuid.uuid4().hex, name=name.strip(), mc=mc, loader=loader, java=java,
                 ram=int(ram), installed=False, java_major=None, build="",
                 public_tunnel=False, tunnel_command="playit")
        self.folder(p).mkdir(parents=True)
        self.save(p)
        return p

    def idle(self, p):
        if self.process and self.process.poll() is None and self.running_id == p["id"]:
            raise ManagerError("Сначала остановите сервер и дождитесь завершения сохранения мира.")

    def backup(self, p, reason="manual"):
        self.idle(p)
        folder = self.folder(p)
        dest = self.root / "backups" / p["id"]
        dest.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + reason + ".zip"
        archive = dest / name
        self.log("Создание резервной копии всего профиля…")
        try:
            with zipfile.ZipFile(archive.with_suffix(".part"), "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                for f in folder.rglob("*"):
                    if f.is_symlink():
                        raise ManagerError("В профиле обнаружена символическая ссылка. Резервное копирование прервано.")
                    if f.is_file():
                        z.write(f, f.relative_to(folder))
            archive.with_suffix(".part").replace(archive)
        except Exception:
            archive.with_suffix(".part").unlink(missing_ok=True)
            raise
        self.log(f"Резервная копия: {archive}")
        return archive

    def install(self, p, entry):
        with self.guard:
            self.idle(p)
            if p["installed"]:
                raise ManagerError("Сервер уже установлен. Для другой версии создайте новый профиль: миры нельзя безопасно понижать.")
            if entry["id"] != p["mc"]:
                raise ManagerError("Версия манифеста не совпадает с профилем.")
            meta = self.catalog.metadata(entry)
            required = meta.get("javaVersion", {}).get("majorVersion", 8)
            p["java_major"] = required
            self.save(p)
            check_java(p["java"], required)
            build = self.catalog.loader_build(p["loader"], p["mc"])
            self.backup(p, "before-install")
            folder = self.folder(p)
            stage = folder / ".install"
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir()
            launch = {}
            try:
                if p["loader"] == "Vanilla":
                    info = meta["downloads"]["server"]
                    self.net.download(info["url"], stage / "server.jar", {"sha1": info["sha1"]}, self.log)
                    launch = {"jar": "server.jar"}
                elif p["loader"] == "Fabric":
                    installers = self.net.json(FABRIC + "/versions/installer")
                    installer = next((x for x in installers if x.get("stable")), None)
                    if not installer:
                        raise ManagerError("Стабильный установщик Fabric недоступен.")
                    url = f"{FABRIC}/versions/loader/{p['mc']}/{build}/{installer['version']}/server/jar"
                    self.net.download(url, stage / "fabric-server-launch.jar", log=self.log)
                    launch = {"jar": "fabric-server-launch.jar"}
                else:
                    if p["loader"] == "Forge":
                        base = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{build}/forge-{build}-installer.jar"
                    else:
                        base = f"{NEO}/{build}/neoforge-{build}-installer.jar"
                    expected = self.net.bytes(base + ".sha1").decode().strip().split()[0]
                    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected):
                        raise ManagerError("Не удалось получить контрольную сумму установщика.")
                    jar = stage / "installer.jar"
                    self.net.download(base, jar, {"sha1": expected}, self.log)
                    with zipfile.ZipFile(jar) as z:
                        config = json.loads(z.read("install_profile.json"))
                        if config.get("minecraft") != p["mc"]:
                            raise ManagerError("Установщик предназначен для другой версии Minecraft. Установка отменена.")
                    self.log(f"Официальный установщик {p['loader']} {build}…")
                    proc = subprocess.Popen([p["java"], "-jar", "installer.jar", "--installServer"], cwd=stage,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                    for line in proc.stdout:
                        self.log(line.rstrip())
                    if proc.wait() != 0:
                        raise ManagerError("Установщик завершился с ошибкой. Подробности в журнале; повторная установка допустима.")
                    linux_args = list(stage.glob("libraries/**/unix_args.txt"))
                    if len(linux_args) != 1:
                        raise ManagerError("Установщик не создал ожидаемый unix_args.txt. Эта схема запуска пока не поддерживается.")
                    launch = {"args": linux_args[0].relative_to(stage).as_posix()}
                    win = linux_args[0].with_name("win_args.txt")
                    if win.exists():
                        launch["windows_args"] = win.relative_to(stage).as_posix()
                    jar.unlink()
                # Commit only a successful install. Keep profile configuration out of installer control.
                for child in stage.iterdir():
                    if child.name in ("profile.json", "eula.txt", "server.properties", "mods", "mods.lock.json"):
                        continue
                    dest = folder / child.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(dest)
                        else:
                            dest.unlink()
                    shutil.move(str(child), dest)
                p.update(installed=True, build=build, launch=launch)
                self.save(p)
                self.log("Установка завершена. Примите EULA, затем нажмите Run.")
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    def accept_eula(self, p, accepted):
        self.idle(p)
        (self.folder(p) / "eula.txt").write_text("# Accepted explicitly in Minecraft Server Manager\neula=" + str(bool(accepted)).lower() + "\n", encoding="utf-8")

    def eula(self, p):
        f = self.folder(p) / "eula.txt"
        return f.exists() and any(line.strip().lower() == "eula=true" for line in f.read_text(encoding="utf-8").splitlines())

    def command(self, p):
        ram = int(p["ram"])
        if not 1 <= ram <= 128:
            raise ManagerError("Укажите объём RAM от 1 до 128 ГБ.")
        cmd = [p["java"], "-Xms512M", f"-Xmx{ram}G"]
        launch = p.get("launch", {})
        if "jar" in launch:
            cmd += ["-jar", launch["jar"]]
        elif "args" in launch:
            key = "windows_args" if os.name == "nt" else "args"
            if key not in launch:
                raise ManagerError("Этот профиль установлен для Ubuntu. Запустите его на Ubuntu.")
            cmd += ["@" + launch[key]]
        else:
            raise ManagerError("Профиль не содержит команды запуска. Установите сервер.")
        return cmd + ["nogui"]

    def run(self, p):
        with self.guard:
            if self.process and self.process.poll() is None:
                raise ManagerError("В этой версии приложения одновременно работает один сервер. Сначала остановите текущий.")
            if not p.get("installed"):
                raise ManagerError("Сначала установите сервер.")
            if not self.eula(p):
                raise ManagerError("Прочитайте EULA и явно примите её перед запуском.")
            check_java(p["java"], p["java_major"])
            folder = self.folder(p)
            self.process = subprocess.Popen(self.command(p), cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
            self.running_id = p["id"]
            self.state = "Запускается"
            threading.Thread(target=self._read, args=(self.process, folder), daemon=True).start()
            if p.get("public_tunnel"):
                command = p.get("tunnel_command", "playit").strip()
                if not command:
                    self.stop()
                    raise ManagerError("Укажите команду запуска туннеля playit.")
                try:
                    self.tunnel_process = subprocess.Popen([command], cwd=folder, stdout=subprocess.PIPE,
                                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                                           errors="replace", bufsize=1)
                    threading.Thread(target=self._read_tunnel, args=(self.tunnel_process,), daemon=True).start()
                    self.log("Публичный туннель запускается. Адрес подключения и ссылка первой настройки появятся ниже.")
                except OSError as e:
                    self.stop()
                    raise ManagerError("Не удалось запустить playit. Установите агент на Ubuntu или отключите публичный туннель.") from e

    def _read_tunnel(self, proc):
        try:
            for line in proc.stdout:
                self.log("[Туннель] " + line.rstrip())
        finally:
            proc.stdout.close()
            code = proc.wait()
            if self.tunnel_process is proc:
                self.tunnel_process = None
            self.log(f"Публичный туннель завершён: {code}")

    def _read(self, proc, folder):
        try:
            with (folder / "manager-console.log").open("a", encoding="utf-8") as out:
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    self.log(line.rstrip())
                    if 'Done (' in line and self.state == "Запускается":
                        self.state = "Работает"
        finally:
            code = proc.wait()
            proc.stdout.close()
            proc.stdin.close()
            if self.process is proc:
                self.state = "Остановлен" if code == 0 else f"Ошибка (код {code})"
                self.running_id = None
            if self.tunnel_process and self.tunnel_process.poll() is None:
                self.tunnel_process.terminate()
            self.log(f"Процесс сервера завершён: {code}")

    def stop(self):
        with self.guard:
            if self.process and self.process.poll() is None:
                try:
                    self.process.stdin.write("stop\n")
                    self.process.stdin.flush()
                    self.state = "Сохраняет мир и останавливается"
                    self.log("Отправлена команда stop. Ожидаем сохранения мира; принудительное завершение не выполняется.")
                except (OSError, BrokenPipeError) as e:
                    raise ManagerError("Сервер уже завершает работу или закрыл консоль.") from e

    def settings(self, p, java, ram, properties, public_tunnel=None, tunnel_command=None):
        with self.guard:
            self.idle(p)
            if not java.strip() or not 1 <= int(ram) <= 128:
                raise ManagerError("Укажите Java и RAM от 1 до 128 ГБ.")
            self.backup(p, "settings")
            folder = self.folder(p)
            f = folder / "server.properties"
            tmp = f.with_suffix(".tmp")
            tmp.write_text(encode_properties(properties), encoding="ascii")
            tmp.replace(f)
            p.update(java=java.strip(), ram=int(ram))
            if public_tunnel is not None:
                p["public_tunnel"] = bool(public_tunnel)
            if tunnel_command is not None:
                p["tunnel_command"] = tunnel_command.strip() or "playit"
            self.save(p)


def encode_properties(text):
    # Java Properties historically uses ISO-8859-1; Unicode escapes work on old and new servers.
    out = []
    for c in text:
        if ord(c) < 128:
            out.append(c)
        else:
            units = c.encode("utf-16-be")
            out.extend("\\u" + units[i:i+2].hex() for i in range(0, len(units), 2))
    return "".join(out)


class Mods:
    def __init__(self, manager):
        self.m = manager
        self.net = manager.net

    def search(self, p, query):
        if p["loader"] == "Vanilla":
            raise ManagerError("Vanilla не загружает моды. Создайте профиль Fabric, Forge или NeoForge.")
        facets = [["project_type:mod"], ["versions:" + p["mc"]], ["categories:" + p["loader"].lower()],
                  ["server_side:required", "server_side:optional"]]
        return self.net.json(MODRINTH + "/search", query=query, facets=json.dumps(facets), limit=30, index="downloads")["hits"]

    def lock(self, p):
        path = self.m.folder(p) / "mods.lock.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def plan(self, p, project_id):
        if p["loader"] == "Vanilla":
            raise ManagerError("Vanilla не поддерживает моды.")
        selected = {}
        pinned = {}
        project_cache = {}

        def project(pid):
            if pid not in project_cache:
                project_cache[pid] = self.net.json(MODRINTH + "/project/" + urllib.parse.quote(pid))
            row = project_cache[pid]
            if row.get("server_side") not in ("required", "optional") or row.get("project_type") != "mod":
                raise ManagerError(f"{row.get('title', pid)}: поддержка серверной стороны не подтверждена каталогом.")
            return row

        def visit(pid=None, vid=None):
            if vid:
                version = self.net.json(MODRINTH + "/version/" + urllib.parse.quote(vid))
                if pid and pid != version["project_id"]:
                    raise ManagerError("Каталог вернул зависимость другого проекта.")
                pid = version["project_id"]
            else:
                row = project(pid)
                pid = row["id"]
                versions = self.net.json(MODRINTH + "/project/" + pid + "/version",
                                         loaders=json.dumps([p["loader"].lower()]), game_versions=json.dumps([p["mc"]]))
                versions = [v for v in versions if v["version_type"] == "release"]
                if not versions:
                    raise ManagerError(f"{row['title']}: нет стабильного файла для {p['mc']} / {p['loader']}.")
                version = max(versions, key=lambda v: v["date_published"])
            row = project(pid)
            if p["mc"] not in version["game_versions"] or p["loader"].lower() not in version["loaders"]:
                raise ManagerError(f"{row['title']}: обязательная зависимость несовместима с версией игры или загрузчиком.")
            if vid:
                if pid in pinned and pinned[pid] != vid:
                    raise ManagerError(f"{row['title']}: зависимости требуют разные версии. Автоматическая установка остановлена.")
                pinned[pid] = vid
            if pid in selected:
                if vid and selected[pid]["version_id"] != vid:
                    raise ManagerError(f"{row['title']}: конфликт требований к версии зависимости. Нужен другой выпуск мода.")
                return
            files = [f for f in version["files"] if f["filename"].lower().endswith(".jar")]
            primary = [f for f in files if f.get("primary")]
            if not primary and len(files) != 1:
                raise ManagerError(f"{row['title']}: невозможно однозначно выбрать серверный JAR.")
            if not files:
                raise ManagerError(f"{row['title']}: JAR-файл не найден.")
            f = (primary or files)[0]
            safe_name(f["filename"])
            if not f.get("hashes", {}).get("sha512"):
                raise ManagerError("Каталог не предоставил SHA-512 мода.")
            selected[pid] = dict(project_id=pid, title=row["title"], version_id=version["id"],
                                 version=version["version_number"], file=f, dependencies=version["dependencies"])
            if len(selected) > 100:
                raise ManagerError("Слишком много зависимостей: установка остановлена.")
            for dep in version["dependencies"]:
                if dep["dependency_type"] == "required":
                    if not dep.get("project_id") and not dep.get("version_id"):
                        raise ManagerError(f"{row['title']}: внешнюю зависимость {dep.get('file_name')} нельзя установить автоматически.")
                    visit(dep.get("project_id"), dep.get("version_id"))

        visit(project_id)
        installed = self.lock(p)
        for pid, item in selected.items():
            if pid in installed and installed[pid]["version_id"] != item["version_id"]:
                raise ManagerError(f"{item['title']}: уже установлена другая версия. Автоматическая замена отключена, чтобы не нарушить зависимости.")
        combined = {**installed, **selected}
        for item in combined.values():
            for d in item.get("dependencies", []):
                if d["dependency_type"] != "incompatible":
                    continue
                if d.get("version_id"):
                    conflict = any(x["version_id"] == d["version_id"] for x in combined.values())
                else:
                    conflict = d.get("project_id") in combined
                if conflict:
                    raise ManagerError(f"{item['title']}: каталог указывает несовместимость с одним из выбранных/установленных модов.")
        return [item for pid, item in selected.items() if pid not in installed]

    def install(self, p, plan):
        with self.m.guard:
            self.m.idle(p)
            if not p.get("installed"):
                raise ManagerError("Сначала установите сервер.")
            if not plan:
                return
            folder = self.m.folder(p)
            mods = folder / "mods"
            mods.mkdir(exist_ok=True)
            lock = self.lock(p)
            names = set()
            for item in plan:
                name = safe_name(item["file"]["filename"])
                if name in names or (mods / name).exists() or item["project_id"] in lock:
                    raise ManagerError(f"Файл/мод уже существует: {name}. Повторите поиск.")
                names.add(name)
            self.m.backup(p, "before-mods")
            stage = folder / (".mods-" + uuid.uuid4().hex)
            stage.mkdir()
            moved = []
            try:
                for item in plan:
                    f = item["file"]
                    target = stage / f["filename"]
                    self.net.download(f["url"], target, f["hashes"], self.m.log)
                    if not zipfile.is_zipfile(target):
                        raise ManagerError(f"{f['filename']} не является JAR/ZIP.")
                for item in plan:
                    name = item["file"]["filename"]
                    (stage / name).replace(mods / name)
                    moved.append(mods / name)
                    lock[item["project_id"]] = item
                atomic_json(folder / "mods.lock.json", lock)
            except Exception:
                for f in moved:
                    f.unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(stage)
            self.m.log(f"Установлено модов с зависимостями: {len(plan)}. Совместимость по каталогу проверена; конфликты во время игры всё ещё возможны.")

    def remove(self, p, pid):
        with self.m.guard:
            self.m.idle(p)
            lock = self.lock(p)
            if pid not in lock:
                raise ManagerError("Мод не найден.")
            victim = lock[pid]
            for key, item in lock.items():
                if key != pid and any(d["dependency_type"] == "required" and
                                      (d.get("project_id") == pid or d.get("version_id") == victim["version_id"])
                                      for d in item.get("dependencies", [])):
                    raise ManagerError(f"Этот мод требуется для {item['title']}. Сначала удалите зависимый мод.")
            self.m.backup(p, "remove-mod")
            target = self.m.folder(p) / "mods" / safe_name(victim["file"]["filename"])
            hold = target.with_suffix(".removed")
            if target.exists():
                target.replace(hold)
            del lock[pid]
            try:
                atomic_json(self.m.folder(p) / "mods.lock.json", lock)
            except Exception:
                if hold.exists():
                    hold.replace(target)
                raise
            hold.unlink(missing_ok=True)
