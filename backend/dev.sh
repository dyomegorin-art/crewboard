#!/bin/sh
# Запуск команды в контейнере api без зависимостей: ./dev.sh ruff check .
cd "$(dirname "$0")/.." && docker compose run --rm --no-deps api "$@"
