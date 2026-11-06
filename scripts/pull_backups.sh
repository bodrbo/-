#!/bin/sh
# Забирает на ЭТОТ компьютер (Mac) копии базы и загруженные фото/документы с сервера.
# Запускать на своём компьютере, не на сервере:
#
#   scripts/pull_backups.sh ЛОГИН@АДРЕС_СЕРВЕРА [ПАПКА_НАЗНАЧЕНИЯ]
#
# Нужен rsync и вход по SSH (пароль спросит один раз на каждую папку; чтобы не вводить
# его каждый раз — настройте вход по ключу: ssh-copy-id ЛОГИН@АДРЕС_СЕРВЕРА).
# Ничего не удаляется: старые копии базы, которых на сервере уже нет, остаются у вас.
#
# Папка проекта на сервере по умолчанию business.bodrbo.ru/app (REMOTE_ROOT=... — другая).
set -eu

REMOTE="${1:?Укажите ЛОГИН@АДРЕС_СЕРВЕРА}"
DEST="${2:-$HOME/bodrbo-backups}"
REMOTE_ROOT="${REMOTE_ROOT:-business.bodrbo.ru/app}"

mkdir -p "$DEST/db" "$DEST/static"
chmod 700 "$DEST"

echo "Копии базы данных…"
rsync -az --partial --exclude '.lock' --exclude '*.partial' --exclude '.work-*' \
  "$REMOTE:$REMOTE_ROOT/backups/" "$DEST/db/"

for folder in boat_documents work_photos tuning_boats checklist_photos supply_photos diplomas; do
  echo "Файлы: static/$folder…"
  rsync -az --partial "$REMOTE:$REMOTE_ROOT/static/$folder/" "$DEST/static/$folder/" || echo "  (папки нет — пропускаю)"
done

echo "Готово. Всё лежит в $DEST"
ls -1t "$DEST/db" | head -3
