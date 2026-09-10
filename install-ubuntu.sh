#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
echo 'Установка Python, Tkinter, Java 21 и публичного туннеля playit.'
sudo apt-get update
sudo apt-get install -y python3 python3-tk openjdk-21-jre-headless curl gnupg

if ! command -v playit >/dev/null 2>&1; then
    curl -SsL https://packages.playit.gg/keys/playit.gpg \
      | gpg --dearmor \
      | sudo tee /usr/share/keyrings/playit.gpg >/dev/null
    sudo chmod 0644 /usr/share/keyrings/playit.gpg
    sudo curl -fsSL -o /etc/apt/sources.list.d/playit.list \
      https://packages.playit.gg/repo-files/playit-debian.list
    sudo apt-get update
    sudo apt-get install -y playit
fi

if getent group playit >/dev/null 2>&1; then
    sudo usermod -aG playit "$USER"
fi
chmod +x run.sh
python3 - <<'PY'
from pathlib import Path
import os
project = Path.cwd()
target = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'applications'
target.mkdir(parents=True, exist_ok=True)
def quoted(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
(target / 'minecraft-server-manager.desktop').write_text(
    '[Desktop Entry]\nType=Application\nName=Minecraft Server Manager\n'
    'Comment=Локальное управление Minecraft сервером\n'
    'Exec=python3 ' + quoted(project / 'run.py') + '\n'
    'Icon=applications-games\nTerminal=false\nCategories=Game;Utility;\n', encoding='utf-8')
print('Установка завершена.')
print('ВАЖНО: выйдите из учётной записи Ubuntu и войдите снова, чтобы применилось членство в группе playit.')
print('После повторного входа: меню приложений → Minecraft Server Manager или bash run.sh')
print('Для Minecraft 26.x потребуется Java 25. Укажите её путь в настройках профиля.')
print('При первом запуске playit привяжите агент по ссылке в журнале приложения.')
PY
