Waterius test build 1.1.5

# Waterius 1.1.0 — проверка мастера

## Что изменено

- Если `/api/channel/` возвращает каналы, интеграция не создаёт новые счётчики Waterius и предлагает сопоставить существующие каналы с `sensor.*` / `input_number.*` / `input_number.*` Home Assistant.
- Если каналов нет, запускается мастер первоначальной настройки: типы счётчиков → тарифность электричества → serial + `sensor.*` / `input_number.*` → проверка текущих значений → создание Universal Source → первое показание.
- Для Universal Source используется отдельный `key`. Account API token больше не подставляется вместо него.
- После настройки отправка выполняется встроенным scheduler интеграции; отдельная HA automation не требуется.
- `send_all`, `send_configured_reading` и `send_all_to_waterius` отправляют текущие значения сопоставленных HA-сенсоров.
- Очевидные мгновенные датчики (`L/min`, `m³/h`, `W`, `kW`) отклоняются. Литры переводятся в m³, Wh/MWh — в kWh.

## Установка тестовой версии

Скопировать содержимое папки `waterius` в:

`/config/custom_components/waterius/`

Затем перезапустить Home Assistant.

## Тест 1 — новый пустой аккаунт

1. Удалить старую запись интеграции Waterius только если тестируется именно новый аккаунт. Не удалять файлы старой версии до сохранения копии.
2. Добавить интеграцию Waterius.
3. Ввести API token нового аккаунта.
4. Убедиться, что мастер сообщает об отсутствии каналов и предлагает выбрать типы счётчиков.
5. Выбрать нужные типы.
6. Для электричества выбрать 1/2/3 тарифа.
7. Ввести serial и выбрать накопительные `sensor.*` / `input_number.*`.
8. На экране проверки убедиться, что значения и единицы корректны.
9. Продолжить.
10. Для пустого аккаунта мастер предложит открыть `https://account.waterius.ru/devices/add/home-assistant`, нажать «Создать устройство» и вставить показанный Waterius `key`. После первой отправки каналы счётчиков создаются автоматически.
11. После завершения проверить, что в Waterius появились каналы и показания.
12. Нажать кнопку «Отправить показания сейчас» и убедиться, что Waterius получает свежие значения HA.

## Тест 2 — аккаунт с существующими каналами

1. Добавить интеграцию с token существующего аккаунта.
2. Мастер должен сразу показать найденные каналы по одному.
3. Сопоставить каждому нужному каналу `sensor.*` / `input_number.*`; ненужные для отправки из HA оставить пустыми.
4. Завершить настройку.
5. Нажать «Отправить показания сейчас».
6. Проверить Waterius и журнал HA.

## Логи

Для диагностики временно включить:

```yaml
logger:
  logs:
    custom_components.waterius: debug
```

## Важное ограничение

Мастер создаёт/сопоставляет счётчики и доставляет показания в Waterius. Настройка конкретной УК/ресурсоснабжающей организации (`export`) в этой версии не создаётся автоматически: текущая интеграция умеет читать export, но публично подтверждённого write API для его безопасного создания мы пока не используем.


## 1.1.5

Первичная настройка пустого аккаунта переведена на актуальный официальный путь Waterius: `https://account.waterius.ru/devices/add/home-assistant`. Пользователь создаёт устройство Home Assistant, сразу копирует показанный `key`; первая отправка из интеграции создаёт каналы счётчиков автоматически. Устаревший `/api/source/add_universal` больше не используется.


## 1.1.9
- Universal payload now includes `name`, matching the current Wiren Board Waterius client.
- Removed obsolete `https://waterius.ru/cloud` fallback; only `https://uc.waterius.ru` is used.
- Successful universal POST diagnostics now include HTTP status.


## 1.1.9
- Bootstrap confirmation now polls account API for up to 30 seconds every 2 seconds.
- After an accepted HTTP 2xx, retry screen only rechecks API and does not resend the payload.
- Expected serial numbers must be present before bootstrap is considered complete.


## 1.1.9 bootstrap retry
For a freshly created Waterius Home Assistant device, bootstrap now retries the same payload with the same key once automatically when HTTP 200 is followed by incomplete placeholder channel metadata. The verification button performs one more same-key POST and polls API for up to 30 seconds.
