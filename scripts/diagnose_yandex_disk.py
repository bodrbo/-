#!/usr/bin/env python3
"""Read-only check of the connection to Яндекс.Диск from THIS machine, step by step.

    venv/bin/python scripts/diagnose_yandex_disk.py

Reads YANDEX_DISK_TOKEN from the environment or from the .env next to app.py and
tells at which step the connection breaks (name lookup, API, upload address,
upload itself). It uploads a 5-byte test file to <app folder>/_diagnostic/ and
deletes it again. The token is never printed.
"""
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://cloud-api.yandex.net/v1/disk"


def token_from_env():
    value = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
    if value:
        return value
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.strip().partition("=")
                if key.strip() == "YANDEX_DISK_TOKEN":
                    return rest.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def main():
    token = token_from_env()
    if not token:
        print("YANDEX_DISK_TOKEN не найден ни в окружении, ни в .env рядом с app.py")
        return 1
    print("Токен найден, символов: %d" % len(token))

    def scrub(error):
        return ("%s: %s" % (error.__class__.__name__, str(error).replace(token, "***")))[:300]

    try:
        import requests
        print("requests %s, Python %s" % (requests.__version__, sys.version.split()[0]))
    except ImportError as error:
        print("Нет библиотеки requests: %s" % error)
        return 1
    headers = {"Authorization": "OAuth " + token}

    def step(title, action):
        try:
            result = action()
            print("[ok]    %s%s" % (title, (" — " + result) if result else ""))
            return True
        except Exception as error:
            print("[ОШИБКА] %s — %s" % (title, scrub(error)))
            return False

    def resolve(host):
        return lambda: ", ".join(sorted({item[4][0] for item in socket.getaddrinfo(host, 443)}))

    step("Адрес cloud-api.yandex.net находится", resolve("cloud-api.yandex.net"))
    holder = {}

    def info():
        response = requests.get(API, headers=headers, timeout=20)
        return "ответ %s" % response.status_code

    if not step("Запрос к API Диска (информация о диске)", info):
        return 1

    def upload_address():
        response = requests.get(
            API + "/resources/upload", headers=headers, timeout=20,
            params={"path": "app:/_diagnostic/test.txt", "overwrite": "true"},
        )
        if response.status_code == 409 or response.status_code == 404:
            requests.put(API + "/resources", headers=headers, timeout=20, params={"path": "app:/_diagnostic"})
            response = requests.get(
                API + "/resources/upload", headers=headers, timeout=20,
                params={"path": "app:/_diagnostic/test.txt", "overwrite": "true"},
            )
        if response.status_code != 200:
            raise RuntimeError("ответ %s" % response.status_code)
        holder["href"] = response.json()["href"]
        holder["host"] = holder["href"].split("/")[2]
        return "сервер загрузки %s" % holder["host"]

    if not step("Получение адреса для загрузки", upload_address):
        return 1
    step("Адрес сервера загрузки находится", resolve(holder["host"]))

    def put_file():
        response = requests.put(holder["href"], data=b"hello", timeout=60)
        return "ответ %s" % response.status_code

    step("Загрузка тестового файла", put_file)

    def cleanup():
        response = requests.delete(
            API + "/resources", headers=headers, timeout=20,
            params={"path": "app:/_diagnostic", "permanently": "true"},
        )
        return "ответ %s" % response.status_code

    step("Удаление тестовой папки", cleanup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
