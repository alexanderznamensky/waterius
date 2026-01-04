[README.md](https://github.com/user-attachments/files/24423947/README.md)
# Waterius Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub Release](https://img.shields.io/github/release/alexanderznamensky/waterius.svg)](https://github.com/alexanderznamensky/waterius/releases)
[![License](https://img.shields.io/github/license/alexanderznamensky/waterius.svg)](LICENSE)

Интеграция для Home Assistant, которая позволяет получать показания счётчиков воды и электроэнергии из сервиса [account.waterius.ru](https://account.waterius.ru).

*Home Assistant integration for retrieving water and electricity meter readings from the [account.waterius.ru](https://account.waterius.ru) service.*

---

## Возможности / Features

- 📊 **Автоматическое получение показаний** счётчиков воды (ХВС, ГВС) и электроэнергии (T1, T2, T3)
- 🔄 **Настраиваемый интервал обновления** (от 10 минут)
- 📅 **Диагностические сенсоры** с информацией о сроках оплаты
- 📤 **Отправка показаний** в управляющую компанию через сервисы Home Assistant
- 🔘 **Кнопка принудительного обновления** данных
- 🌐 **Полная поддержка русского языка** в интерфейсе

*Automatic meter reading retrieval • Configurable update interval • Diagnostic sensors • Manual reading submission • Forced update button • Full Russian language support*

---

## Установка / Installation

### Через HACS (рекомендуется / Recommended)

1. Откройте **HACS** в Home Assistant
2. Нажмите на три точки в правом верхнем углу и выберите **Custom repositories**
3. Добавьте репозиторий:
   - **URL**: `https://github.com/alexanderznamensky/waterius`
   - **Category**: `Integration`
4. Найдите **Waterius** в HACS и нажмите **Download**
5. Перезапустите Home Assistant

### Вручную / Manual Installation

1. Скопируйте папку `custom_components/waterius` в директорию `custom_components` вашей установки Home Assistant
2. Перезапустите Home Assistant

---

## Настройка / Configuration

### Получение токена / Getting Your Token

1. Для получение токена откройте страницу: https://account.waterius.ru/api/user/token/
2. Скопируйте ваш **API Token**

### Добавление интеграции / Adding the Integration

1. Перейдите в **Настройки** → **Устройства и службы** → **Добавить интеграцию**
2. Найдите **Waterius**
3. Введите:
   - **Название** (по умолчанию: "Waterius")
   - **Token** (полученный на предыдущем шаге)
   - **Интервал обновления** в минутах (по умолчанию: 15)
4. Нажмите **Отправить**

*Settings → Devices & Services → Add Integration → Search for "Waterius" → Enter your token*

---

## Сущности / Entities

После настройки интеграция создаёт следующие сущности:

### Сенсоры счётчиков / Meter Sensors

Для каждого счётчика создаётся сенсор с показаниями:

- **ХВС** (Холодное водоснабжение) - `m³`
- **ГВС** (Горячее водоснабжение) - `m³`
- **Электроэнергия T1** (Пик) - `kWh`
- **Электроэнергия T2** (Ночь) - `kWh`
- **Электроэнергия T3** (Полупик) - `kWh`

**Атрибуты сенсоров:**
- Серийный номер счётчика
- Статус отчёта в УК
- Дата поверки
- Значение в предыдущем периоде
- Значение в текущем периоде
- Время последней передачи данных

### Диагностические сенсоры / Diagnostic Sensors

- **Срок оплаты** - дата, до которой необходимо оплатить услуги (с классом устройства `timestamp`)

**Атрибуты:**
- Название устройства
- Управляющая компания (УК)
- Лицевой счёт
- Дата отправки показаний
- Телефон пользователя
- Дней до оплаты

### Сводный сенсор / Summary Sensor

- **Summary** - общее количество источников данных

**Атрибуты:**
- Количество источников
- Количество каналов
- Количество экспортов

### Кнопка / Button

- **Update now** - принудительное обновление данных со всех счётчиков

---

## Сервисы / Services (пока не реализовано)

### `waterius.send_reading`

Отправляет показание по конкретному каналу на account.waterius.ru.

**Параметры:**
- `channel_id` (обязательный): ID канала (можно найти в атрибутах сенсора)
- `value` (обязательный): Показание счётчика

**Пример использования:**
```yaml
service: waterius.send_reading
data:
  channel_id: 55170
  value: 162.5
```

### `waterius.send_all`

Отправляет текущие показания по всем каналам, которые есть в интеграции.

**Пример использования:**
```yaml
service: waterius.send_all
```

---

## Устранение неполадок / Troubleshooting

### Ошибка авторизации / Authentication Error

- Проверьте правильность токена в настройках интеграции
- Убедитесь, что токен активен в личном кабинете account.waterius.ru

### Данные не обновляются / Data Not Updating

- Проверьте подключение к интернету
- Увеличьте интервал обновления в опциях интеграции
- Проверьте логи Home Assistant на наличие ошибок: **Настройки** → **Система** → **Логи**

### Отсутствуют некоторые счётчики / Missing Meters

- Убедитесь, что счётчики добавлены и настроены в личном кабинете account.waterius.ru
- Попробуйте нажать кнопку **Update now**

---

## Поддержка / Support

- 🐛 **Сообщить об ошибке**: [GitHub Issues](https://github.com/alexanderznamensky/waterius/issues)
- 💡 **Предложить улучшение**: [GitHub Discussions](https://github.com/alexanderznamensky/waterius/discussions)
- 📖 **Документация Waterius**: [account.waterius.ru](https://account.waterius.ru)

---

## Благодарности / Credits

Разработчик: [@alexanderznamensky](https://github.com/alexanderznamensky)

---

## Лицензия / License

Этот проект распространяется под лицензией MIT. Подробности в файле [LICENSE](LICENSE).

---

**⭐ Если вам нравится эта интеграция, поставьте звёздочку на GitHub!**

*If you like this integration, please give it a star on GitHub!*
