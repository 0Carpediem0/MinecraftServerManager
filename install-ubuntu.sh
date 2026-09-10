#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
echo 'Установка Python, Tkinter и Java 21 из репозиториев Ubuntu.'
sudo apt-get update
sudo apt-get install -y python3 python3-tk openjdk-21-jre-headless
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
print('Готово. Запуск: меню приложений → Minecraft Server Manager или bash run.sh')
print('Для Minecraft 26.x потребуется Java 25. Укажите её путь в настройках профиля.')
print('Для доступа из интернета установите агент с https://playit.gg/download/linux и один раз привяжите его по ссылке в журнале приложения.')
PY
