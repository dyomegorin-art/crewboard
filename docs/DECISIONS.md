# Журнал решений

Короткие записи: **что решили → почему**. Решения, которых не было в ТЗ, принимались автономно.

## Что откуда взято

Код переиспользован из двух моих публичных репозиториев; стиль (ruff, mypy strict, единый формат ошибок, комментарии на русском там, где они объясняют «почему») сохранён.

| Источник | Что взято | Что изменено |
|---|---|---|
| tutorbot `pyproject.toml` | зависимости, ruff / mypy strict / pytest-asyncio / coverage | + `httpx`, `aiosmtplib`; `tests/*` без E501 |
| tutorbot `app/config.py`, `db.py`, `redis.py` | настройки, async-движок, Redis-клиент | `validate_startup()` из repairdesk (fail closed); naming convention для ограничений |
| tutorbot `app/errors.py`, `logging.py`, middleware `request_id` | без изменений | `AppError.details` (сумма доплаты в 409); `/health` не пишется в лог |
| tutorbot `app/security.py` | argon2 через `asyncio.to_thread`, JWT | остался только access-токен (+ короткий SSE-билет и state для OAuth); refresh — непрозрачная строка, хэш в БД |
| tutorbot `app/api/deps.py` | `get_current_user` | `require_role` заменён на `require(permission)` по воркспейсу |
| tutorbot `app/api/auth.py` | регистрация с гонкой через `IntegrityError`, логин с rate limit | refresh-cookie, ротация, reuse detection, сброс пароля, OAuth, демо-вход |
| tutorbot `app/ratelimit.py` | без изменений | — |
| tutorbot `app/locks.py` | `acquire`/`release` | `slot_lock` → `redis_lock` (общий). В биллинге не используется: места сериализует `SELECT … FOR UPDATE` |
| tutorbot `app/worker.py` | каркас arq с cron | задачи: outbox (3 с), биллинг-тик (1 мин), очистка демо (1 ч) |
| tutorbot `docker-compose.yml`, CI, `tests/conftest.py` | паттерн `x-app`, healthchecks, джоб backend, тестовая БД + Redis db=1 | + web, mailpit; TRUNCATE всех таблиц динамически; фабрики пользователей без argon2 |
| repairdesk `intake.py` | сырое тело коммитится до разбора; UNIQUE + `payload_fingerprint`; дубль / конфликт | порт на async → `billing/webhooks.py`; провайдер-независимый |
| repairdesk `worker.py` | outbox: `SKIP LOCKED`, backoff 30→900 с, `PermanentCommandError`, `FAILED` + `SystemIssue` | порт на async → `app/outbox.py`; реестр обработчиков, `dedupe_key`, инжектируемый `now` |
| repairdesk `audit.py` | функция `audit(...)` | → `services/audit.py`, пишет в `activity_log` воркспейса |
| repairdesk `auth.py` | `PERMISSIONS`, `require(permission)`, `ACCESS_DENIED` | заголовок `X-Synthetic-Identity` **не** взят: настоящая аутентификация |
| repairdesk `config.validate_startup` | проверка опасных конфигураций | dev-эндпоинты + staging/production → приложение не стартует |
| repairdesk `frontend/src/api.ts` | класс `ApiError` | клиент с access-токеном в памяти и single-flight refresh |
| repairdesk `README.md` | структура | — |

## Окружение разработки

- Тесты и линтеры гоняются в контейнере (`make test`, `make lint`): на хосте нет Python 3.12, Postgres и Redis.
- Порты Postgres и Redis наружу **не публикуются** (на машине разработчика они обычно заняты); порты api / web / mailpit настраиваются `API_PORT`, `WEB_PORT`, `MAILPIT_PORT`.
- `BACKEND_BASE_IMAGE` позволяет собрать образ api на уже имеющемся образе с теми же зависимостями (по умолчанию `python:3.12-slim`).
- Контейнер `web` ставит зависимости при старте (`npm install`), `node_modules` лежат в томе.
- Playwright e2e идёт с хоста против поднятого compose: браузеры не нужно тащить в образы.

## Данные

- Первичные ключи — `BIGINT` (identity); `family_id` refresh-токенов — UUID. Деньги — целые копейки, время — `timestamptz` UTC.
- Уникальность email — функциональный индекс `lower(email)`; адреса хранятся в нижнем регистре.
- Добавлено к схеме из ТЗ (всё нужно для требований ТЗ):
  - `projects.key` и `projects.task_counter` — номера задач `PRJ-12` и атомарный счётчик;
  - `project_guests` — гость видит только проекты, куда добавлен;
  - `invitations.invoice_id` — приглашение «ожидает оплаты места»;
  - `invoices.billing_period`, `seats`, `attempts` — что применить при оплате и сколько повторов dunning сделано;
  - `invoices.kind = card_update`; `subscriptions.pending_period`, `trial_reminder_sent_at`;
  - `outbox_commands.dedupe_key UNIQUE` — повторная постановка renewal / retry не создаёт вторую команду.
- Составные внешние ключи: `tasks(workspace_id, project_id|status_id|assignee_id)`, `comments`, `statuses`, `project_guests`. Исполнитель — `ON DELETE SET NULL (assignee_id)` (PostgreSQL ≥ 15): при удалении участника задачи остаются без исполнителя на уровне БД.
- Позиции карточек — целые с шагом 1024; вставка между соседями — среднее; зазор исчерпан → колонка перенумеровывается в той же транзакции.

## Аутентификация

- Регистрация сразу логинит пользователя (баннер «подтвердите email»). Письма идут через outbox; ссылка с токеном лежит в payload команды (для ретраев), в таблице токенов — только SHA-256.
- Access JWT 15 минут; refresh — случайная строка, httpOnly cookie на путь `/api/auth`, `SameSite=Lax`, `Secure` вне local. Сессия живёт 30 дней со скользящей ротацией.
- CSRF: cookie-эндпоинты (`refresh`, `logout`) требуют непустой `X-Requested-With`; кросс-сайтовая форма не может поставить кастомный заголовок без CORS-preflight.
- Повторное использование отозванного refresh → отзыв всего семейства, 401 `refresh_token_reused`; отзыв коммитится **до** исключения.
- Сброс пароля: 30 минут, одноразовый, отзывает все refresh-токены; ответ одинаков для любого email. Демо-пользователям письма не уходят (у них нет пароля).
- Вход через GitHub: `state` — подписанный JWT в cookie на `/api/auth/github`; совпадение с подтверждённым локальным email → привязка, с неподтверждённым → ошибка (иначе захват чужого адреса).
- Часы: сервисы получают `now` параметром. В local/test с `DEV_ENDPOINTS` время = реальное + сдвиг из Redis (общий для api и воркера); в тестах — `FakeClock`. Токены и срок refresh проверяются по тем же часам.
- Rate limit: логин, регистрация, сброс, refresh, демо, вебхуки (fail-open, как в tutorbot).

## Воркспейсы и права

- Не участник получает **404** на любой маршрут `/api/w/{id}/…`; участник без права — **403** и запись `access_denied` в `activity_log` (entity `permission`, в ленте видна только owner / admin).
- Права по ролям — словарь `PERMISSIONS` в `api/deps.py`. `require(permission, write=True)` добавляет проверку `read_only` → 402 (`ensure_writable` — единая функция).
- Смена роли между гостем и платной ролью не поддерживается (нужна доплата) — приглашайте заново.
- Принятие приглашения по ссылке из письма подтверждает email приглашённого (ящиком владеет тот, кто получил письмо).
- SSE: access-токен меняется на билет на 60 секунд (`POST …/events/ticket`), билет кладётся в query — утёкший из логов бесполезен через минуту. Гостям события не отправляются (проверка доступа к проекту по событию дороже пользы).

## Биллинг

- Все цены, лимиты и расписание dunning — в `billing/plans.py`; проверки — в `billing/limits.py` с кодами `plan_limit_*` (HTTP 402).
- **Автомат** (`billing/state.py`): явная таблица переходов, недопустимый → `InvalidTransition` + `SystemIssue`. `canceled` = «бывший Pro на Free»; новые Free-воркспейсы — `active` с `plan = free`.
- **Статус меняется только по вебхуку.** Страница возврата опрашивает `…/billing/invoices/{id}/status`.
- Протокол провайдера разделён на `verify_webhook` и `parse_webhook` (в ТЗ — один `parse_webhook`): неподлинный вебхук отклоняется до сохранения, а повторный запрос к API YooKassa выполняется уже после коммита сырого тела.
- Недопустимый переход в вебхуке — ответ 200 `rejected_invalid_transition` и `SystemIssue` (повтор не поможет, провайдер не должен долбить нас ретраями).
- Доплата за место: `цена × оставшиеся_секунды / секунды_периода` (`Decimal`, `ROUND_HALF_UP`). Период — календарный (`add_months` с обрезкой дня), поэтому длина периода плавает 28–31 день.
- Места: спрос = занятые + активные приглашения платных ролей; ёмкость = `max(seats, seats неоплаченных доплат)`. Приглашение сверх ёмкости создаёт proration-счёт и `CHARGE_INVOICE`; место попадает в `subscription.seats` только после вебхука. Конкуренцию решает `FOR UPDATE` на подписке (тест на параллельные приглашения).
- Invite без подтверждённой суммы → 409 `payment_confirmation_required` с суммой в `details`.
- **Trial**: один раз на воркспейс (смотрим `billing.trial_started` в логе); места растут вместе с командой. Карта в trial привязывается проверочным платежом 1 ₽ (`card_update`; в настоящей системе он возвращается). Конец trial: есть карта → списание, нет → Free.
- **Продление**: период подписки сдвигается только оплатой; счёт `renewal` защищён частичным UNIQUE `(workspace_id, period_start)`, команда — `dedupe_key`. Идемпотентный ключ списания: `renewal:{workspace_id}:{period_start}` (повторы — с суффиксом `:retryN`).
- **Dunning**: от первой неудачи повторы через 1, 3, 5 дней, через 7 — `read_only`. Успешная оплата из любого состояния возвращает `active`; период считается от `period_start` просроченного счёта (за «дыру» не доначисляем).
- Просроченный счёт owner оплачивает новой картой (`pay-outstanding`: тот же счёт, новый checkout).
- **Даунгрейд**: проекты сверх лимита архивируются, кроме трёх с самым свежим изменением (последняя правка задачи, иначе создание проекта). Участники, гости, задачи не удаляются.
- Смена периода месяц ↔ год сделана (`pending_period`, со следующего продления).
- Mock-провайдер: исход платежа решает тестовая карта; вебхук уходит из outbox на `API_INTERNAL_URL` с HMAC-SHA256 по сырому телу; `MOCK_WEBHOOK_DUPLICATE_RATE=0.2`.
- YooKassa: у уведомлений нет id и подписи → `event_id = {payment_id}:{status}`, подлинность = IP из официального списка **и** повторный запрос платежа; состояние берётся только из ответа API; несовпадение суммы со счётом не применяется (`PAYMENT_AMOUNT_MISMATCH`).

## Задачи и данные

- Лог активности: курсор `(created_at, id)` в base64; окно 7 дней на Free применяется в SQL.
- CSV: потоковая выдача со своей сессией БД; ячейки, начинающиеся с `= + - @`, экранируются апострофом (CSV-инъекции).
- Демо: свежий пользователь, 3 бота-участника, 2 проекта, 25 задач, trial Pro; очистка раз в час удаляет демо старше 24 часов вместе с воркспейсами. Ботам не нужен пароль.
- Seed (только `ENV=local|test`): воркспейс «Acme Studio» и три постоянных аккаунта (пароль указан в README) — для проверки ролей без регистрации.

## Фронтенд

- Vite + React 19 + TS strict, Tailwind v4, свои компоненты без UI-кита. Markdown — собственный минимальный рендер: сначала экранирование, потом разрешённые конструкции (XSS-безопасно).
- Оптимистичный drag-and-drop: снимок кэша → мгновенный перенос → откат при ошибке; сервер — источник истины.
- Глобальный обработчик ошибок мутаций показывает модалку «Перейдите на Pro» по коду `plan_limit_*`.
- Ссылка «GitHub» на лендинге берётся из `VITE_REPO_URL`.
