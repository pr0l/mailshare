# Запросы Pingvin Share X, на которые опирается Mailshare

Записано с Pingvin Share X **v2.1.0**, локальное хранилище (не S3). Это внутренний API веб-интерфейса Pingvin: официально он не описан и может измениться в новых версиях. После обновления Pingvin сверьтесь с этим списком — запишите те же действия в браузере (F12 → Network) и сравните.

## Вход служебной учётной записи (Gateway)

```
POST /api/auth/signIn
Content-Type: application/json

{"username": "mailshare", "password": "..."}      # или {"email": "...", "password": "..."}
```

Ответ ставит куку `access_token` (JWT, живёт 15 минут). Дальше все запросы — с этой кукой.

Gateway входит заново, если до конца токена меньше минуты или если Pingvin ответил 401 или 403. Просроченный токен при создании share Pingvin отклоняет кодом **403**, а не 401.

## Загрузка (Gateway)

1. **Создать share**

   ```
   POST /api/shares
   {"id": "<20 символов A-Za-z0-9>", "expiration": "180-days", "recipients": [], "security": {}, "size": <общий размер в байтах>}
   ```

   Срок — строка `<число>-days`, не больше `share.maxExpiration` в Pingvin, размер — не больше `share.maxSize`. Оба задаются из `.env` (`SHARE_EXPIRATION_DAYS`, `MAX_SHARE_BYTES`) и совпадают у Gateway и Pingvin.

2. **Объявить файл** — для каждого файла перед его первой частью:

   ```
   POST /api/shares/<id>/files/upload-init
   {"id": "<uuid файла>", "name": "<имя>", "totalChunks": <число частей>}
   ```

3. **Отправить части** — строго по порядку, тело — сырые байты:

   ```
   POST /api/shares/<id>/files?id=<uuid файла>&name=<имя>&chunkIndex=<N>&totalChunks=<всего>
   Content-Type: application/octet-stream
   ```

   Размер части должен совпадать с настройкой Pingvin `share.chunkSize` (по умолчанию 10 000 000 байт; Gateway читает её из `GET /api/configs`). Ожидаемый номер части Pingvin вычисляет по размеру уже принятого. На часть не по порядку он отвечает `400` с полем `expectedChunkIndex` — по нему Gateway восстанавливает загрузку после обрыва.

4. **Завершить**

   ```
   POST /api/shares/<id>/complete       → 202
   ```

   После этого ссылка `/s/<id>` начинает работать. Для share из нескольких файлов Pingvin в фоне собирает архив для «скачать всё».

5. **Отменить** — `DELETE /api/shares/<id>` (недогруженные и брошенные загрузки).

## Скачивание (получатель, через PUBLIC_HOST)

Только эти адреса открыты наружу в `mailcow/mailshare-files.conf.template`:

| Запрос | Назначение |
| --- | --- |
| `GET /s/<id>` → `/share/<id>` | Страница со списком файлов |
| `GET /_next/...`, `/img/...`, `/manifest.json`, `/favicon.ico` | Оформление страницы |
| `GET /api/shares/<id>` | Сведения о share |
| `POST /api/shares/<id>/token` (тело `{}`) | Пропуск посетителя: кука `share_<id>_token` |
| `GET /api/shares/<id>/metaData` | Готов ли архив «скачать всё» |
| `GET /api/shares/<id>/files/<uuid>` | Скачать файл |
| `GET /api/shares/<id>/files/zip` | Скачать всё архивом |

`/sw.js` (фоновый помощник Pingvin) намеренно закрыт: он пытается закешировать страницы, которые снаружи недоступны.

## Если включить S3

С S3-хранилищем в v2 файлы передаются напрямую между браузером и бакетом по подписанным ссылкам. Загрузка частями (шаг 3) и скачивание устроены иначе, поэтому Gateway и список адресов в `mailshare-files.conf.template` придётся переделать по новым записанным запросам. Подробности: [документация Pingvin Share X — S3](https://smp46.github.io/pingvin-share-x/setup/s3).
