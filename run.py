"""
Single-command launcher — starts the bot and the userbot pool together as
two child processes, so you only ever run one file:

    python run.py

They stay separate PROCESSES underneath (run_bot.py / run_userbot.py) —
that split exists so a crash or a stuck ffmpeg job in the userbot pool
can't take the payment/UI bot down with it, and vice versa. This launcher
doesn't remove that; it just saves you from opening two terminals: it starts
both, restarts whichever one exits (crash, or a transient network drop) after
a short delay, and stops both cleanly on Ctrl+C.

For Docker, docker-compose.yml already runs run_bot.py and run_userbot.py as
two real, independent containers — this launcher is for local, non-Docker
use. Running run_bot.py / run_userbot.py yourself in separate terminals
still works exactly as before, if you want that instead.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

RESTART_DELAY_SECONDS = 3
BASE_DIR = Path(__file__).resolve().parent

CHILDREN = {
    "bot": "run_bot.py",
    "userbot": "run_userbot.py",
}


def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(BASE_DIR / script)], cwd=str(BASE_DIR))


def main() -> None:
    procs: dict[str, subprocess.Popen] = {name: _spawn(script) for name, script in CHILDREN.items()}
    print(f"Запущено: {', '.join(f'{n} (pid {p.pid})' for n, p in procs.items())}. Ctrl+C — остановить оба.")

    try:
        while True:
            for name, script in CHILDREN.items():
                proc = procs[name]
                ret = proc.poll()
                if ret is not None:
                    print(f"[{name}] завершился с кодом {ret}, перезапуск через {RESTART_DELAY_SECONDS} сек…")
                    time.sleep(RESTART_DELAY_SECONDS)
                    procs[name] = _spawn(script)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстанавливаю оба процесса…")
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Готово.")


if __name__ == "__main__":
    main()
