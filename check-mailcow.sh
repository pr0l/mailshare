#!/usr/bin/env bash
# Устанавливает доработки Mailshare в Mailcow и следит, чтобы они совпадали с эталонами.
# Восстанавливает пропавшее или изменённое и перезапускает нужные контейнеры.
#
# Запускать: при развёртывании, после каждого обновления Mailcow (update.sh),
# после изменения файлов в mailcow/ или адресов в .env.
#
# Эталоны — в папке mailcow/ рядом со скриптом; настройки меняют там.
# Адреса и путь к Mailcow — в .env рядом со скриптом.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MS="$HERE/mailcow"
ENV_FILE="$HERE/.env"

# Значение из .env без выполнения файла (пароль может содержать спецсимволы)
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '\r' | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

PUBLIC_HOST="$(env_get PUBLIC_HOST)"
MC="$(env_get MAILCOW_DIR)"
MC="${MC:-/opt/mailcow-dockerized}"

if [ ! -f "$ENV_FILE" ]; then
  echo "ОШИБКА: нет $ENV_FILE — скопируйте .env.example в .env и заполните" >&2; exit 1
fi
if ! [[ "$PUBLIC_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
  echo "ОШИБКА: в .env не задан или неверен PUBLIC_HOST ('$PUBLIC_HOST')" >&2; exit 1
fi
if [ ! -f "$MC/mailcow.conf" ]; then
  echo "ОШИБКА: Mailcow не найден в $MC — проверьте MAILCOW_DIR в .env" >&2; exit 1
fi
cd "$MC"
restart=()

# 0. Все эталоны на месте?
for f in mailshare-sogo.js site.mailshare.custom mailshare-files.conf.template mailshare-auth.php docker-compose.override.yml; do
  if [ ! -f "$MS/$f" ]; then
    echo "ОШИБКА: нет эталона $MS/$f — восстановление невозможно" >&2; exit 1
  fi
done

# 1. Кнопка в SOGo: наш блок в начале custom-sogo.js должен совпадать с эталоном
JS=data/conf/sogo/custom-sogo.js
START='^// ---- Mailshare: кнопка'
END='^// ---- конец блока Mailshare'
has_start=$(grep -c "$START" "$JS" || true)
has_end=$(grep -c "$END" "$JS" || true)
if [ "$has_start" -gt 1 ] || [ "$has_start" != "$has_end" ]; then
  echo "ОШИБКА: блок Mailshare в $JS повреждён (начал: $has_start, концов: $has_end). Исправьте вручную." >&2
  exit 1
fi
current=$(sed -n "\|$START|,\|$END|p" "$JS")
if [ "$current" != "$(cat "$MS/mailshare-sogo.js")" ]; then
  sed "\|$START|,\|$END|d" "$JS" > "$JS.rest"
  { cat "$MS/mailshare-sogo.js"; echo; cat "$JS.rest"; } > "$JS.new"
  rm -f "$JS.rest"
  mv "$JS.new" "$JS"
  if [ "$has_start" -eq 0 ]; then echo "установлена кнопка в SOGo"; else echo "обновлена кнопка в SOGo"; fi
  restart+=(memcached-mailcow sogo-mailcow)
fi

# 2. Страница загрузки /mailshare/ на почтовом адресе
if ! cmp -s "$MS/site.mailshare.custom" data/conf/nginx/site.mailshare.custom; then
  cp "$MS/site.mailshare.custom" data/conf/nginx/site.mailshare.custom
  echo "установлена настройка /mailshare/"
  restart+=(nginx-mailcow)
fi

# 3. Публичный сайт скачивания: шаблон + адрес из .env
rendered="$(mktemp)"
sed "s/{{PUBLIC_HOST}}/$PUBLIC_HOST/g" "$MS/mailshare-files.conf.template" > "$rendered"
if ! cmp -s "$rendered" data/conf/nginx/mailshare-files.conf; then
  cp "$rendered" data/conf/nginx/mailshare-files.conf
  chmod 644 data/conf/nginx/mailshare-files.conf
  echo "установлен сайт $PUBLIC_HOST"
  restart+=(nginx-mailcow)
fi
rm -f "$rendered"

# 4. Проверка входа в почту
if ! cmp -s "$MS/mailshare-auth.php" data/web/mailshare-auth.php; then
  cp "$MS/mailshare-auth.php" data/web/mailshare-auth.php
  echo "установлена проверка входа"
fi

# 5. Подключение почтового Nginx к сети mailshare
if ! grep -q mailshare docker-compose.override.yml 2>/dev/null; then
  echo "ВНИМАНИЕ: в $MC/docker-compose.override.yml нет сети mailshare."
  echo "Образец: $MS/docker-compose.override.yml"
fi

# 6. Публичный адрес в сертификате
if ! grep -E "^ADDITIONAL_SAN=" mailcow.conf | grep -q "$PUBLIC_HOST"; then
  echo "ВНИМАНИЕ: $PUBLIC_HOST нет в ADDITIONAL_SAN в mailcow.conf — сертификат может его не покрывать."
fi

if [ ${#restart[@]} -gt 0 ]; then
  docker compose restart $(printf '%s\n' "${restart[@]}" | sort -u)
else
  echo "всё на месте"
fi
