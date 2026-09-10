"""Run with a graphical session. Creates temporary data, no real server or network."""
import tempfile
import tkinter as tk
from unittest.mock import patch
from msm.app import App

with tempfile.TemporaryDirectory() as folder:
    root = tk.Tk()
    with patch.object(App, "load_versions"):
        app = App(root, folder)
        app.entries = {"1.20.1": {"id": "1.20.1"}}
        app.mc.set("1.20.1")
        app.create()
        root.update()
        assert app.current["mc"] == "1.20.1"
        assert not app.accepted.get()
        assert app.public_tunnel.get()
        assert app.tabs.index("end") == 4
        assert "motd=" in app.properties.get("1.0", "end")
        assert "online-mode=false" in app.properties.get("1.0", "end")
        assert root.winfo_width() >= 940
        root.destroy()
    app.manager.app_lock.close()
print("GUI smoke: PASS (window, profile, four tabs, settings, EULA unchecked)")
