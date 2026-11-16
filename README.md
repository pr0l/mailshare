# Mailshare — отправка больших файлов из почты Mailcow

Сотрудник нажимает в окне письма SOGo кнопку с облаком, загружает файлы и нажимает «Вставить в письмо». В конец письма добавляется блок со ссылкой на файлы, к письму прикрепляется `download-files.pdf` с той же ссылкой. Получатель скачивает файлы по ссылке без регистрации.

В примерах ниже `mail.example.com` — адрес вашей почты Mailcow (`MAIL_HOST`), `files.example.com` — публичный адрес для скачивания (`PUBLIC_HOST`). Оба задаются в `.env`.

- Загружать могут только сотрудники с действующей сессией почты Mailcow.
- Скачивать может любой, у кого есть ссылка.
- Лимиты по умолчанию: до 300 МБ за отправку, до 100 файлов, до 10 ГБ в сутки на сотрудника, срок ссылки 180 дней.

Инструкция для сотрудников — [docs/user-guide.md](docs/user-guide.md).

## Как устроено

```
Браузер сотрудника                         Получатель
   │  https://MAIL_HOST/mailshare/            │  https://PUBLIC_HOST/s/<код>
   ▼                                          ▼
Nginx Mailcow ── auth_request ──► mailshare-auth.php (проверка сессии Mailcow)
   │  /mailshare/  (только с сессией)         │  /s/, /share/, скачивание (всем)
   ▼                                          ▼
Gateway (mailshare-gateway:8000) ──────► Pingvin Share X (mailshare-pingvin:3000)
   служебная учётная запись Pingvin            хранит файлы и раздаёт их
```

| Часть | Где работает | Что делает |
| --- | --- | --- |
| Pingvin Share X v2.1.0 | контейнер `mailshare-pingvin` | Хранит файлы, отдаёт страницу скачивания |
| Gateway | `gateway/`, контейнер `mailshare-gateway` | Страница загрузки, передача файлов в Pingvin частями, PDF-вложение, лимиты, журнал |
| Проверка входа | `data/web/mailshare-auth.php` в Mailcow | Отвечает Nginx, вошёл ли сотрудник в почту |
| Nginx: `/mailshare/` | `data/conf/nginx/site.mailshare.custom` в Mailcow | Страница загрузки за проверкой входа |
| Nginx: `PUBLIC_HOST` | `data/conf/nginx/mailshare-files.conf` в Mailcow | Публичное скачивание; вход, админка и загрузка закрыты |
| Кнопка в SOGo | блок в начале `data/conf/sogo/custom-sogo.js` | Окно загрузки поверх письма, вставка блока и PDF в письмо |
| Сеть `mailshare` | Docker | Связывает Nginx Mailcow, Gateway и Pingvin. Наружу Gateway и Pingvin не открыты |

Gateway узнаёт пользователя только из заголовка `X-Mailshare-User`, который ставит Nginx после проверки сессии. Поэтому у Gateway нет опубликованных портов.

## Состав репозитория

```
├── docker-compose.yml         Pingvin + Gateway, сеть mailshare
├── .env.example               образец .env: адреса, путь к Mailcow, служебная учётка Pingvin
├── check-mailcow.sh           устанавливает доработки в Mailcow и следит за ними
├── gateway/                   код Gateway
│   ├── app.py
│   ├── static/index.html      страница загрузки (отдельная и окно внутри письма)
│   ├── Dockerfile
│   └── requirements.txt
├── mailcow/                   эталоны файлов для Mailcow (их читает check-mailcow.sh)
│   ├── docker-compose.override.yml
│   ├── site.mailshare.custom
│   ├── mailshare-files.conf.template   сайт скачивания; адрес подставляется из .env
│   ├── mailshare-auth.php
│   └── mailshare-sogo.js      блок для custom-sogo.js
└── docs/
    ├── user-guide.md          инструкция для сотрудников
    └── pingvin-api.md         запросы Pingvin, на которые опирается Gateway
```

На сервере репозиторий обычно лежит в `/opt/mailshare` (путь может быть любым). Папка `mailcow/` — эталоны: настройки Mailcow меняют здесь, а `check-mailcow.sh` переносит их в Mailcow, подставив адреса из `.env`.

Не хранятся в репозитории (см. `.gitignore`): `.env`, `pingvin/` (файлы и база Pingvin), `gateway-data/` (журнал отправок), `backup/`.

## Развёртывание на новом сервере

Нужно: работающий Mailcow (проверено с SOGo 5.12.12), Docker Compose **2.23 или новее** (`docker compose version`), DNS-запись `PUBLIC_HOST` на IP сервера.

**1. Код и настройки**

```bash
git clone <репозиторий> /opt/mailshare
cd /opt/mailshare
cp .env.example .env && chmod 600 .env
mkdir -p pingvin/data pingvin/images gateway-data
chown -R 1000:1000 pingvin gateway-data
```

В `.env` заполните адреса (`MAIL_HOST`, `PUBLIC_HOST`, `MAILCOW_DIR`), при необходимости лимиты, и первого администратора Pingvin: `PINGVIN_INIT_ADMIN=true`, `PINGVIN_ADMIN_EMAIL`, `PINGVIN_ADMIN_PASSWORD` (например, из `openssl rand -base64 24`).

**2. Запуск Pingvin**

```bash
docker compose up -d pingvin
```

Настройки Pingvin (адрес, язык, лимиты, закрытая регистрация) он получает из `docker-compose.yml` и `.env`, руками их выставлять не нужно. При первом запуске создаётся администратор из `.env`.

**3. Служебная учётная запись** — через SSH-туннель `ssh -L 3000:127.0.0.1:3000 root@сервер`, в браузере `http://localhost:3000`:

1. Войдите администратором из `.env`. Затем в `.env` поставьте `PINGVIN_INIT_ADMIN=false` и сотрите `PINGVIN_ADMIN_PASSWORD`.
2. В разделе пользователей создайте пользователя **без** прав администратора, например `mailshare`, с паролем из `openssl rand -base64 32`.
3. Впишите его в `.env`: `PINGVIN_USER`, `PINGVIN_PASSWORD`.

**4. Запуск Gateway**

```bash
docker compose up -d --build
docker compose ps        # оба контейнера healthy / running
```

**5. Подключение к Mailcow**

1. В `MAILCOW_DIR/mailcow.conf` добавьте `PUBLIC_HOST` в `ADDITIONAL_SAN` (через запятую), чтобы Mailcow выпустил для него сертификат.
2. Скопируйте `mailcow/docker-compose.override.yml` в каталог Mailcow. Если там уже есть такой файл — объедините вручную.
3. Примените и установите доработки:

```bash
cd /opt/mailcow-dockerized                                    # ваш MAILCOW_DIR
cp data/conf/sogo/custom-sogo.js data/conf/sogo/custom-sogo.js.bak
docker compose up -d
/opt/mailshare/check-mailcow.sh      # ставит кнопку в SOGo, настройки Nginx и проверку входа
docker compose exec nginx-mailcow nginx -t
```

**6. Автопроверка после обновлений Mailcow** — каждую ночь:

```bash
echo '17 4 * * * root /opt/mailshare/check-mailcow.sh >> /var/log/mailshare-check.log 2>&1' > /etc/cron.d/mailshare-check
```

**7. Проверка** — раздел «Проверка после изменений» ниже.

## Настройки

Все настройки — в `.env`; образец с пояснениями — `.env.example`. Лимиты общие: одно значение получают и Gateway, и Pingvin, поэтому разойтись они не могут.

| Переменная | По умолчанию | Значение |
| --- | --- | --- |
| `MAIL_HOST` | — | Адрес почты; Gateway принимает загрузки только со страниц этого адреса |
| `PUBLIC_HOST` | — | Адрес скачивания: ссылки, сайт в Nginx, App URL в Pingvin |
| `MAILCOW_DIR` | `/opt/mailcow-dockerized` | Куда `check-mailcow.sh` ставит доработки |
| `MAX_SHARE_BYTES` | 300000000 (300 МБ) | Лимит одной отправки, все файлы вместе |
| `SHARE_EXPIRATION_DAYS` | 180 | Срок жизни ссылки |
| `FILE_RETENTION_DAYS` | 30 | Сколько дней Pingvin хранит файлы после истечения ссылки (доступны только администратору). Учитывайте при расчёте места |
| `DAILY_QUOTA_GB` | 10 | Суточный объём на сотрудника |
| `MAX_ACTIVE_UPLOADS` | 3 | Одновременных отправок на сотрудника |
| `MAX_FILES_PER_SHARE` | 100 | Файлов в одной отправке |
| `PDF_NAME` | `download-files.pdf` | Имя PDF-вложения |
| `DISPLAY_TZ_OFFSET` | 3 | Часовой пояс дат в блоке и PDF (часов от UTC) |
| `PINGVIN_APP_NAME` | Email file storage | Название на странице скачивания |
| `PINGVIN_USER`, `PINGVIN_PASSWORD` | — | Служебная учётка Pingvin для Gateway |
| `PINGVIN_INIT_ADMIN`, `PINGVIN_ADMIN_EMAIL`, `PINGVIN_ADMIN_PASSWORD` | false | Первый администратор Pingvin; действует только на пустой базе |

Применить изменения:

```bash
docker compose up -d                 # Gateway и Pingvin получат новые значения
./check-mailcow.sh                   # если меняли PUBLIC_HOST или MAILCOW_DIR
```

Настройки Pingvin, перечисленные в блоке `configs` в `docker-compose.yml`, в веб-интерфейсе Pingvin заблокированы — меняйте их там или через `.env`. Остальные (оформление, пароли пользователей и т. п.) по-прежнему настраиваются в интерфейсе.

При смене адреса ещё нужно поменять `ADDITIONAL_SAN` в `mailcow.conf` и DNS. Ссылки в `docs/user-guide.md` — примеры, замените их на свои и поправьте лимиты перед раздачей сотрудникам.

## Обслуживание

**После обновления Mailcow** (`update.sh`):

```bash
/opt/mailshare/check-mailcow.sh
```

Скрипт сверяет все доработки с эталонами в `mailcow/` и восстанавливает пропавшее или изменённое. Затем вручную отправьте себе письмо через кнопку с облаком: скрипт не заметит, если новая версия SOGo изменила устройство окна письма. Признак проблемы — в окне загрузки сообщение «Карточку вставить не удалось» или «PDF прикрепить не удалось». Диагностика: в окне письма F12 → Console → `mailshareDiag()`.

**Обновление Pingvin.** Версия образа закреплена (`v2.1.0`). Перед сменой прочитайте [примечания к релизу](https://github.com/smp46/pingvin-share-x/releases), затем проверьте загрузку и скачивание. Gateway опирается на внутренний API Pingvin ([docs/pingvin-api.md](docs/pingvin-api.md)) — если он изменится, загрузки получат отказ, причина будет в `docker compose logs gateway`.

**Изменения кода.** После правки `gateway/` — `docker compose up -d --build gateway`. После правки файлов в `mailcow/` или адресов в `.env` — `./check-mailcow.sh`.

**Журналы.**

```bash
cd /opt/mailshare
docker compose logs --tail=50 gateway     # кто, что и когда загрузил; ошибки Pingvin
docker compose logs --tail=50 pingvin
```

Журнал отправок также в `gateway-data/gateway.db` (таблицы `batches` и `batch_files`).

**Место на диске.** Файлы хранятся `SHARE_EXPIRATION_DAYS` + `FILE_RETENTION_DAYS` дней (по умолчанию 180 + 30). Отправка из нескольких файлов занимает примерно вдвое больше своего размера: Pingvin хранит ещё и архив для «скачать всё». Следите за `df -h /opt/mailshare/pingvin/data`.

**Резервное копирование.** Достаточно репозитория, `.env` и `pingvin/data/pingvin-share.db` (база Pingvin). Загруженные файлы временные, их можно не копировать.

**Отключить ссылку досрочно.** В админке Pingvin (через SSH-туннель) найдите share по коду из ссылки и удалите его. Код и автора можно найти в журнале Gateway.

## Проверка после изменений

1. `https://MAIL_HOST/mailshare/` в приватном окне без входа → переброс на вход в почту.
2. Почта → «Написать» → кнопка с облаком → загрузить 2–3 файла → «Вставить в письмо». В конце письма — блок со ссылкой, во вложениях — `download-files.pdf`. В консоли браузера: `mailshare: вставка в письмо: ck5-конец письма, PDF: да`.
3. Ссылку открыть в приватном окне: файлы скачиваются по одному и архивом.
4. С сервера — запрещённое снаружи закрыто:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -X POST https://PUBLIC_HOST/api/auth/signIn   # 404
   curl -s -o /dev/null -w "%{http_code}\n" -X POST https://PUBLIC_HOST/api/shares        # 404
   ```
5. `/opt/mailshare/check-mailcow.sh` → «всё на месте».

## Переезд на другой сервер

1. Развернуть по разделу «Развёртывание», шаги 1, 2, 4–6, перенеся `pingvin/data` со старого сервера до шага 2 — вместе с базой переедут пользователи и ещё не истёкшие файлы. Шаг 3 не нужен, `PINGVIN_INIT_ADMIN` оставьте `false`.
2. Перенести `.env` (если путь к Mailcow другой — поправить `MAILCOW_DIR`).
3. Направить DNS `PUBLIC_HOST` на новый сервер.

## Если что-то сломалось

| Признак | Где смотреть |
| --- | --- |
| «Хранилище не приняло файлы» | `docker compose logs gateway \| grep pingvin` — ответ Pingvin. Частая причина — неверный `PINGVIN_USER` или `PINGVIN_PASSWORD` в `.env` |
| Страница загрузки — 502 | `docker compose ps`: Gateway не запущен |
| Кнопки нет в SOGo | `./check-mailcow.sh`, затем Ctrl+F5. В консоли должно быть `mailshare: кнопка подключена` |
| Блок или PDF не вставляются | `mailshareDiag()` в консоли окна письма |
| Ссылка не открывается | `docker compose logs nginx-mailcow` в каталоге Mailcow; запрос мог не попасть в список разрешённых в `mailcow/mailshare-files.conf.template` |
