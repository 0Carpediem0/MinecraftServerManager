from __future__ import annotations

import argparse
import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import Manager, ManagerError, Mods

DEFAULT_PROPERTIES = """# Настройки сервера. Пустой server-ip разрешает подключения из локальной сети.
motd=Мой Minecraft сервер
server-port=25565
server-ip=
online-mode=false
enforce-secure-profile=false
white-list=false
max-players=10
difficulty=normal
gamemode=survival
view-distance=8
enable-rcon=false
enable-query=false
"""


class App:
    def __init__(self, root, data=None):
        self.root = root
        self.root.title("Minecraft Server Manager")
        self.root.geometry("1120x820")
        self.root.minsize(940, 700)
        self.events = queue.Queue()
        self.manager = Manager(data, log=lambda text: self.events.put(("log", text)))
        self.manager.acquire_lock()
        self.mods = Mods(self.manager)
        self.busy = False
        self.closing = False
        self.current = None
        self.entries = {}
        self.hits = []
        self.installed = []
        self.actions = []
        self.new_controls = []

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 7))
        style.configure("Title.TLabel", font=("Sans", 19, "bold"))
        style.configure("Hint.TLabel", foreground="#546477")
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Minecraft • Серверы", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Локальное управление сервером на этом компьютере", style="Hint.TLabel").pack(anchor="w", pady=(2, 12))
        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 16, 0))
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=4)
        ttk.Label(left, text="ПРОФИЛИ").pack(anchor="w", pady=(0, 6))
        self.profile_list = tk.Listbox(left, width=26, height=13, exportselection=False, borderwidth=0, font=("Sans", 11))
        self.profile_list.pack(fill="both", expand=True)
        self.profile_list.bind("<<ListboxSelect>>", self.select_profile)
        new = ttk.LabelFrame(left, text="Новый сервер", padding=10)
        new.pack(fill="x", pady=(12, 0))
        self.name = tk.StringVar(value="Мой сервер")
        self.mc = tk.StringVar()
        self.loader = tk.StringVar(value="Vanilla")
        self.snapshots = tk.BooleanVar(value=False)
        for label, var in (("Название", self.name),):
            ttk.Label(new, text=label).pack(anchor="w")
            entry = ttk.Entry(new, textvariable=var)
            entry.pack(fill="x", pady=(2, 8))
            self.new_controls.append(entry)
        ttk.Label(new, text="Версия Minecraft · новые сверху").pack(anchor="w")
        self.version_box = ttk.Combobox(new, textvariable=self.mc, state="readonly", width=24)
        self.version_box.pack(fill="x", pady=(2, 6))
        self.new_controls.append(self.version_box)
        self.snapshot_check = ttk.Checkbutton(new, text="Показывать снапшоты", variable=self.snapshots, command=self.load_versions)
        self.snapshot_check.pack(anchor="w")
        self.new_controls.append(self.snapshot_check)
        self.button(new, "Обновить версии", self.load_versions).pack(fill="x", pady=6)
        ttk.Label(new, text="Тип сервера").pack(anchor="w")
        loader_box = ttk.Combobox(new, textvariable=self.loader, values=("Vanilla", "Fabric", "Forge", "NeoForge"), state="readonly")
        loader_box.pack(fill="x", pady=(2, 8))
        self.new_controls.append(loader_box)
        self.button(new, "Создать профиль", self.create).pack(fill="x")
        self.button(left, "Папка данных", lambda: self.open_folder(self.manager.root)).pack(fill="x", pady=(10, 0))

        self.title = tk.StringVar(value="Создайте или выберите профиль")
        self.status = tk.StringVar(value="Остановлен")
        ttk.Label(right, textvariable=self.title, font=("Sans", 14, "bold")).pack(anchor="w")
        ttk.Label(right, textvariable=self.status, style="Hint.TLabel").pack(anchor="w", pady=5)
        toolbar = ttk.Frame(right)
        toolbar.pack(fill="x", pady=(4, 8))
        self.button(toolbar, "Установить", self.install).pack(side="left")
        self.button(toolbar, "▶ Run", self.run).pack(side="left", padx=6)
        self.stop_button = ttk.Button(toolbar, text="■ Stop", command=self.stop)
        self.stop_button.pack(side="left")
        self.button(toolbar, "Резервная копия", self.backup).pack(side="left", padx=6)
        eula_row = ttk.Frame(right)
        eula_row.pack(fill="x", pady=(0, 10))
        self.accepted = tk.BooleanVar()
        self.eula_check = ttk.Checkbutton(eula_row, text="Я прочитал(а) и принимаю Minecraft EULA", variable=self.accepted, command=self.eula)
        self.eula_check.pack(side="left")
        self.actions.append(self.eula_check)
        ttk.Button(eula_row, text="Открыть EULA ↗", command=lambda: webbrowser.open("https://www.minecraft.net/eula")).pack(side="right")

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        console = ttk.Frame(self.tabs, padding=8)
        settings = ttk.Frame(self.tabs, padding=12)
        mods = ttk.Frame(self.tabs, padding=12)
        help_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(console, text="Живой журнал")
        self.tabs.add(settings, text="Настройки")
        self.tabs.add(mods, text="Моды • Modrinth")
        self.tabs.add(help_tab, text="Помощь")
        self.console = self.text_area(console, height=18, bg="#14212e", fg="#dce8f3", insertbackground="white", font=("Monospace", 10))
        self.console.configure(state="disabled")
        self.java = tk.StringVar(value="java")
        self.ram = tk.IntVar(value=4)
        self.public_tunnel = tk.BooleanVar(value=True)
        self.tunnel_command = tk.StringVar(value="playit")
        ttk.Label(settings, text="Java: команда или полный путь к bin/java").pack(anchor="w")
        java_row = ttk.Frame(settings)
        java_row.pack(fill="x", pady=(4, 12))
        self.java_entry = ttk.Entry(java_row, textvariable=self.java)
        self.java_entry.pack(side="left", fill="x", expand=True)
        self.actions.append(self.java_entry)
        self.button(java_row, "Выбрать…", self.choose_java).pack(side="right", padx=(6, 0))
        ram_row = ttk.Frame(settings)
        ram_row.pack(fill="x")
        ttk.Label(ram_row, text="Максимальная RAM, ГБ:").pack(side="left")
        self.ram_spin = ttk.Spinbox(ram_row, from_=1, to=128, textvariable=self.ram, width=6)
        self.ram_spin.pack(side="left", padx=8)
        self.actions.append(self.ram_spin)
        ttk.Label(settings, text="Оставьте не менее 2–4 ГБ памяти для Ubuntu. Изменения применяются после Run.", style="Hint.TLabel", wraplength=600).pack(anchor="w", pady=8)
        tunnel_row = ttk.Frame(settings)
        tunnel_row.pack(fill="x", pady=(2, 8))
        self.tunnel_check = ttk.Checkbutton(tunnel_row, text="Публичный доступ через playit (без проброса порта)", variable=self.public_tunnel)
        self.tunnel_check.pack(side="left")
        self.actions.append(self.tunnel_check)
        ttk.Label(settings, text="Команда агента playit:").pack(anchor="w")
        self.tunnel_entry = ttk.Entry(settings, textvariable=self.tunnel_command)
        self.tunnel_entry.pack(fill="x", pady=(2, 6))
        self.actions.append(self.tunnel_entry)
        ttk.Label(settings, text="При первом запуске ссылка привязки туннеля появится в журнале. В playit выберите Minecraft Java и локальный адрес 127.0.0.1:25565.", style="Hint.TLabel", wraplength=620).pack(anchor="w", pady=(0, 8))
        ttk.Label(settings, text="server.properties · перед сохранением создаётся полная резервная копия").pack(anchor="w", pady=(8, 4))
        self.properties = self.text_area(settings, height=12, font=("Monospace", 10), undo=True)
        self.button(settings, "Сохранить настройки", self.save_settings).pack(anchor="e", pady=(8, 0))

        search = ttk.Frame(mods)
        search.pack(fill="x")
        self.query = tk.StringVar()
        ttk.Entry(search, textvariable=self.query).pack(side="left", fill="x", expand=True)
        self.button(search, "Найти", self.search_mods).pack(side="right", padx=(8, 0))
        ttk.Label(mods, text="Фильтры: версия профиля + загрузчик + серверная сторона. Только стабильные файлы.", wraplength=620, style="Hint.TLabel").pack(anchor="w", pady=8)
        self.results = tk.Listbox(mods, height=7, exportselection=False)
        self.results.pack(fill="both", expand=True)
        self.button(mods, "Установить выбранный мод…", self.plan_mod).pack(anchor="e", pady=8)
        ttk.Label(mods, text="Установленные через приложение").pack(anchor="w")
        self.installed_list = tk.Listbox(mods, height=5, exportselection=False)
        self.installed_list.pack(fill="both", expand=True, pady=4)
        self.button(mods, "Удалить выбранный мод…", self.remove_mod).pack(anchor="e")
        ttk.Label(mods, text="Каталог не гарантирует отсутствие конфликтов. Файлы, добавленные вручную, не входят в проверку зависимостей.", wraplength=620, style="Hint.TLabel").pack(anchor="w", pady=(8, 0))
        help_text = (
            "1. Обновите версии и создайте профиль. Версия и тип закреплены за профилем.\n\n"
            "2. Укажите Java и RAM в настройках, сохраните. Нажмите «Установить». Если Java не подходит, приложение сообщит нужную версию.\n\n"
            "3. Прочитайте EULA по ссылке и примите её своей отметкой. Нажмите Run. Сообщение Done в журнале означает готовность сервера.\n\n"
            "4. По умолчанию профиль использует online-mode=false и запускает агент playit. Скопируйте внешний адрес из журнала друзьям. При первом запуске откройте ссылку привязки и создайте Minecraft Java tunnel на 127.0.0.1:25565.\n\n"
            "5. Для модов остановите сервер. Выберите мод и проверьте список обязательных зависимостей перед установкой. Моды, требуемые клиенту, нужно отдельно установить на игровых ПК.\n\n"
            "Stop отправляет серверу команду сохранения и остановки. Закрытие приложения ожидает завершения сервера. Если мод завис, смотрите журнал; автоматического убийства процесса нет.\n\n"
            "Резервные копии находятся в папке данных/backups. Они включают весь профиль, включая мир и настройки. Восстановление вручную описано в README.\n\n"
            "Режим online-mode=false не проверяет учётную запись Minecraft. Любой игрок может выбрать чужое имя, поэтому не выдавайте права оператора по имени на публичном сервере без отдельного мода аутентификации.\n\n"
            "В первой версии одновременно работает один сервер. Forge: Minecraft 1.17–1.21.x; NeoForge: стабильные сборки от 1.20.2; Fabric: при наличии стабильного загрузчика. Если установщик недоступен, вы получите объяснение."
        )
        help_widget = self.text_area(help_tab, wrap="word", font=("Sans", 11))
        help_widget.insert("1.0", help_text)
        help_widget.configure(state="disabled")
        self.footer = tk.StringVar(value="Готово")
        ttk.Label(outer, textvariable=self.footer, style="Hint.TLabel").pack(anchor="w", pady=(10, 0))
        self.refresh_profiles()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self.poll)
        self.root.after(200, self.load_versions)

    def text_area(self, parent, **options):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap=options.pop("wrap", "none"), **options)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        scroll.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        text.pack(fill="both", expand=True)
        return text

    def button(self, parent, label, action):
        button = ttk.Button(parent, text=label, command=lambda: self.safe(action))
        self.actions.append(button)
        return button

    def safe(self, action):
        try:
            action()
        except Exception as e:
            self.error(e)

    def error(self, e):
        self.append_log("Ошибка: " + str(e))
        messagebox.showerror("Не удалось выполнить действие", str(e), parent=self.root)

    def task(self, label, action, done=lambda result: None):
        if self.busy:
            return
        self.busy = True
        self.footer.set(label)
        self.set_busy(True)

        def worker():
            try:
                self.events.put(("done", (done, action())))
            except Exception as e:
                self.events.put(("error", e))
        threading.Thread(target=worker, daemon=True).start()

    def set_busy(self, busy):
        for control in self.actions + self.new_controls:
            control.configure(state="disabled" if busy else ("readonly" if isinstance(control, ttk.Combobox) else "normal"))
        self.profile_list.configure(state="disabled" if busy else "normal")

    def append_log(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", text + "\n")
        if int(self.console.index("end-1c").split(".")[0]) > 3500:
            self.console.delete("1.0", "501.0")
        self.console.see("end")
        self.console.configure(state="disabled")

    def poll(self):
        for _ in range(300):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(payload)
            else:
                self.busy = False
                self.set_busy(False)
                self.footer.set("Готово")
                if kind == "error":
                    self.error(payload)
                else:
                    callback, result = payload
                    self.safe(lambda: callback(result))
        active = self.manager.running_id
        profile_state = self.manager.state if active == (self.current or {}).get("id") or not active else "Остановлен (работает другой профиль)"
        self.status.set(profile_state)
        self.stop_button.configure(state="normal" if self.manager.process and self.manager.process.poll() is None else "disabled")
        if self.closing and not self.busy and not (self.manager.process and self.manager.process.poll() is None):
            self.root.destroy()
            return
        self.root.after(80, self.poll)

    def load_versions(self):
        snapshots = self.snapshots.get()
        def done(rows):
            self.entries = {v["id"]: v for v in rows}
            self.version_box.configure(values=list(self.entries))
            if rows and self.mc.get() not in self.entries:
                self.mc.set(rows[0]["id"])
            self.footer.set(f"Версий доступно: {len(rows)}. Новые сверху.")
        self.task("Загрузка официального списка версий…", lambda: self.manager.catalog.versions(snapshots), done)

    def refresh_profiles(self, select=None):
        self.profiles = self.manager.profiles()
        self.profile_list.delete(0, "end")
        for p in self.profiles:
            self.profile_list.insert("end", f"{p['name']}\n".strip() + f" · {p['mc']} / {p['loader']}")
        target = select or (self.current or {}).get("id")
        for i, p in enumerate(self.profiles):
            if p["id"] == target:
                self.profile_list.selection_set(i)
                self.select_profile()
                break

    def select_profile(self, event=None):
        indices = self.profile_list.curselection()
        if not indices or self.busy:
            return
        self.current = self.profiles[indices[0]]
        p = self.current
        self.title.set(f"{p['name']}  •  {p['mc']} / {p['loader']}" + (f" {p['build']}" if p.get("build") else ""))
        self.java.set(p["java"])
        self.ram.set(p["ram"])
        self.public_tunnel.set(p.get("public_tunnel", False))
        self.tunnel_command.set(p.get("tunnel_command", "playit"))
        self.accepted.set(self.manager.eula(p))
        f = self.manager.folder(p) / "server.properties"
        self.properties.delete("1.0", "end")
        self.properties.insert("1.0", f.read_text(encoding="latin-1") if f.exists() else DEFAULT_PROPERTIES)
        self.hits = []
        self.results.delete(0, "end")
        self.show_installed()
        self.footer.set("Установлен" if p["installed"] else "Профиль создан. Укажите Java, сохраните настройки и установите сервер.")

    def profile(self):
        if not self.current:
            raise ManagerError("Создайте или выберите профиль слева.")
        return self.current

    def create(self):
        if self.mc.get() not in self.entries:
            raise ManagerError("Сначала загрузите список версий.")
        p = self.manager.create(self.name.get(), self.mc.get(), self.loader.get())
        self.manager.settings(p, p["java"], p["ram"], DEFAULT_PROPERTIES, True, "playit")
        self.refresh_profiles(p["id"])

    def install(self):
        p = self.profile()
        if p["mc"] not in self.entries:
            raise ManagerError("Обновите список версий; для снапшота включите их показ.")
        self.task("Установка сервера; это может занять несколько минут…", lambda: self.manager.install(p, self.entries[p["mc"]]), lambda _: self.refresh_profiles(p["id"]))

    def run(self):
        p = self.profile()
        self.task("Проверка Java и запуск…", lambda: self.manager.run(p))

    def stop(self):
        self.safe(self.manager.stop)

    def eula(self):
        try:
            self.manager.accept_eula(self.profile(), self.accepted.get())
        except Exception as e:
            self.accepted.set(self.manager.eula(self.current) if self.current else False)
            self.error(e)

    def backup(self):
        p = self.profile()
        self.task("Резервное копирование…", lambda: self.manager.backup(p), lambda path: messagebox.showinfo("Резервная копия создана", str(path)))

    def choose_java(self):
        path = filedialog.askopenfilename(title="Выберите исполняемый файл java")
        if path:
            self.java.set(path)

    def save_settings(self):
        p = self.profile()
        java, ram, props = self.java.get(), self.ram.get(), self.properties.get("1.0", "end-1c")
        public, tunnel = self.public_tunnel.get(), self.tunnel_command.get()
        self.task("Сохранение с резервной копией…", lambda: self.manager.settings(p, java, ram, props, public, tunnel), lambda _: self.refresh_profiles(p["id"]))

    def search_mods(self):
        p, query = self.profile(), self.query.get()
        def done(hits):
            self.hits = hits
            self.results.delete(0, "end")
            for h in hits:
                self.results.insert("end", h["title"] + " — " + h.get("description", "")[:110])
            self.footer.set(f"Найдено: {len(hits)}. Показаны первые 30; уточните запрос для поиска других модов.")
        self.task("Поиск совместимых серверных модов…", lambda: self.mods.search(p, query), done)

    def plan_mod(self):
        p = self.profile()
        self.manager.idle(p)
        if not p["installed"]:
            raise ManagerError("Сначала установите сервер.")
        selection = self.results.curselection()
        if not selection:
            raise ManagerError("Выберите мод из результатов поиска.")
        hit = self.hits[selection[0]]
        def confirm(plan):
            if not plan:
                messagebox.showinfo("Моды", "Этот мод и его зависимости уже установлены.")
                return
            names = "\n".join(f"• {x['title']} — {x['version']}" for x in plan)
            if messagebox.askokcancel("Установка мода и обязательных зависимостей", names + "\n\nБудет создана резервная копия. Совместимость проверена по каталогу, но конфликты возможны. Установить?", parent=self.root):
                self.task("Загрузка модов и зависимостей…", lambda: self.mods.install(p, plan), lambda _: self.show_installed())
        self.task("Проверка зависимостей и несовместимостей…", lambda: self.mods.plan(p, hit["project_id"]), confirm)

    def show_installed(self):
        self.installed = list(self.mods.lock(self.current).values()) if self.current else []
        self.installed_list.delete(0, "end")
        for item in self.installed:
            self.installed_list.insert("end", f"{item['title']} — {item['version']}")

    def remove_mod(self):
        p = self.profile()
        selection = self.installed_list.curselection()
        if not selection:
            raise ManagerError("Выберите установленный мод.")
        item = self.installed[selection[0]]
        if messagebox.askokcancel("Удаление мода", f"Удалить {item['title']}? Будет создана резервная копия. Мир с удалённым контентом мода может измениться."):
            self.task("Резервная копия и удаление мода…", lambda: self.mods.remove(p, item["project_id"]), lambda _: self.show_installed())

    def open_folder(self, path):
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        if self.busy:
            messagebox.showinfo("Операция выполняется", "Дождитесь завершения установки или резервного копирования перед закрытием.")
            return
        if self.manager.process and self.manager.process.poll() is None:
            if messagebox.askokcancel("Остановить сервер?", "Приложение отправит stop и закроется после сохранения мира. Продолжить?"):
                self.manager.stop()
                self.closing = True
                self.set_busy(True)
                self.footer.set("Ожидание сохранения мира и завершения сервера…")
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Локальный менеджер Minecraft Java Edition")
    parser.add_argument("--data-dir", type=Path, help="Папка профилей и резервных копий")
    args = parser.parse_args()
    root = tk.Tk()
    try:
        App(root, args.data_dir)
    except Exception as e:
        messagebox.showerror("Minecraft Server Manager", str(e))
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
