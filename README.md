# B2B Outreach Agent

Python 3.12 / Pydantic / httpx / SQLite. Агент исследует бизнес-заказчиков, сохраняет проверяемые наблюдения, готовит обращения и разбирает ответы до первого предметного созвона.

Каждое исходящее сообщение требует отдельного разрешения оператора. `outreach.enabled=true` включает доступность транспорта. `auto-reply` теперь только исследует запросы и создаёт `pending_approval`; старый флаг больше ничего не отправляет. Во время разработки реальные письма не отправлялись.

## Быстрый запуск

```bash
uv sync --frozen --extra dev
uv run b2b-agent init --config config.json
uv run b2b-agent validate-input --config config.json
uv run b2b-agent run --config config.json --csv examples/sites.csv --dry-run
uv run b2b-agent export --db output/research.sqlite --output output/export
```

`init` создаёт отсутствующие файлы INPUT и не перезаписывает данные владельца. Старые JSON-конфиги читаются с безопасными defaults; при отсутствии INPUT поддерживается явно версионированная миграция старых workflow-деклараций. Если INPUT существует, но повреждён, связанные действия блокируются до исправления. Данные клиентов, INPUT, SQLite, output и STATS исключены из Git; шаблоны находятся в `examples/INPUT`.

## Сквозная offline-демонстрация

Из корня репозитория:

```bash
uv run python examples/offline/demo.py --output output/demo-1
uv run b2b-agent review-actions --config examples/offline/config.json --db output/demo-1/demo.sqlite
uv run b2b-agent stats --db output/demo-1/demo.sqlite --output output/demo-1
uv run b2b-agent report --db output/demo-1/demo.sqlite
```

Демонстрация использует вымышленные адреса `.example`, HTML/MIME fixtures, явные события оператора и transport simulation. Любой сетевой запрос в ней вызывает ошибку. Она проходит запрос → evidence → черновик → отдельное разрешение → simulated send → MIME-ответ → статистика/короткая память → новое разрешение → предложение встречи → согласованное время → brief → отметка held. Появляются `demo.sqlite`, `synthetic-reply.eml`, `meeting-brief.json`, `report.json`, `STATS/`. Повторяйте запуск в новой папке: существующая БД не удаляется.

Отдельно проверить fixture и увидеть карточку можно без сайта и почты:

```bash
uv run b2b-agent inspect-request --config examples/offline/config.json \
  --html examples/offline/request.html --url https://platform.example/requests/demo-1 \
  --now 2026-11-12T12:00:00+00:00 --db output/review.sqlite
uv run b2b-agent review-actions --config examples/offline/config.json --db output/review.sqlite
```

`--now` нужен только для воспроизводимого inspection fixture; фактическая отправка всегда использует настоящее время, сроки разрешения и актуальные запреты.

## Рабочий цикл оператора

```bash
# Поиск требует настроенного Brave API и известной верхней оценки стоимости.
uv run b2b-agent find-requests --config config.json --db output/research.sqlite --output output/requests
uv run b2b-agent auto-reply --config config.json --db output/research.sqlite \
  --requests-json output/requests/request_candidates.json
uv run b2b-agent review-actions --config config.json --db output/research.sqlite
# Для research-возможности: буквальный контакт и черновик без автоматического права писать.
uv run b2b-agent prepare-contact OPPORTUNITY_ID --recipient buyer@example.com --config config.json

# Подставьте один action_id из карточки. Разрешения на всю очередь нет.
uv run b2b-agent approve-action ACTION_ID --actor operator --config config.json
uv run b2b-agent send-action ACTION_ID --config config.json
uv run b2b-agent reject-action ACTION_ID --actor operator --reason 'Нужно изменить предложение'
uv run b2b-agent edit-action ACTION_ID --body-file reviewed-message.txt
uv run b2b-agent defer-action ACTION_ID --until 2026-12-01T10:00:00+03:00
```

Для cold contact нужны явно настроенные `policy.cold_contact_allowed`, `market`, `provider_policy` и проверенное основание/источник контакта. `prepare-contact ... --verified-contact --basis 'Конкретное основание' --source 'operator:record-id'` фиксирует ввод оператора; само по себе это не разрешение отправить сообщение. При неизвестном основании остаётся внутренний черновик. Активная попытка той же компании/подтверждённого контакта блокирует параллельный первый питч из другого потока.

После редактирования текст проходит явную проверку владельцем при новом `approve-action`. Для изменившегося каталога используйте `refresh-action ACTION_ID`, затем заново откройте карточку и разрешите текущую версию. Содержимое уже отправленных/uncertain сообщений не редактируется. Вариант `send-action ... --simulate` фиксирует отдельный `simulated_sent` без OAuth/SMTP; это отличается от `--dry-run`, который не резервирует отправку.

Карточка содержит адресата, канал, событие, цель, источники, неизвестные, добавочные затраты/риски, точный текст и вложения. Разрешение одноразовое, ограничено сроком и связано с получателем, каналом, отправителем, текстом, материалами, контекстом и политикой затрат. Новый ответ, существенная правка каталога или подписи требуют нового решения. Разрешение не обходит suppression, паузу и ограничения контакта.

```bash
uv run b2b-agent import-replies replies.eml --config config.json
uv run b2b-agent import-replies replies.json --config config.json
uv run b2b-agent sync-replies --config config.json
uv run b2b-agent prepare-followup CONVERSATION_ID --reason 'Конкретное новое полезное уточнение'
uv run b2b-agent meeting-brief CONVERSATION_ID --config config.json
uv run b2b-agent mark-meeting CONVERSATION_ID --event scheduled --actor operator \
  --local-time 2026-11-16T15:00:00 --timezone Europe/Moscow --confirmed
uv run b2b-agent mark-meeting CONVERSATION_ID --event held --actor operator
uv run b2b-agent company-stats OPPORTUNITY_OR_ACCOUNT_ID
uv run b2b-agent tick --config config.json
```

Импорт JSON принимает массив нормализованных сообщений: `mailbox_id`, `provider_message_id`, `rfc_message_id`, `sender`, `recipients`, `subject`, `body`, `received_at`, при наличии `thread_id`, `in_reply_to`, `references`. Укажите тот же mailbox ID, что у диалога; похожей темы недостаточно. Неоднозначные письма остаются в ручном разборе. MIME-вложения не исполняются и не читаются как инструкции.

Сначала обрабатывается весь новый авторский текст, включая отказ в конце длинного письма. Цитаты старого письма не считаются новым отказом. Запрет имеет приоритет над интересом; OOO и bounce не считаются человеческим интересом. Обработка события идемпотентна. Свежий успешный inbox/import и обязательный unsubscribe sync нужны для отправки ответов; неполный sync оставляет очередь на hold. SMTP читает ответы через import/отдельный inbox adapter.

`meeting_scheduled` требует конкретных даты, времени, IANA timezone и подтверждения. Слова «давайте созвонимся» времени не назначают. `held`, `no_show`, `cancelled` — отдельные события оператора. Календарь не подключён, слоты не объявляются проверенными на занятость.

```bash
uv run b2b-agent suppress --scope contact --target buyer@example.com --reason 'Просьба клиента'
uv run b2b-agent suppress --scope channel --target buyer@example.com --channel phone --reason 'Не звонить'
uv run b2b-agent suppress --scope account --target ACCOUNT_ID --reason 'Запрет компании'
uv run b2b-agent pause --scope global
uv run b2b-agent takeover --target CONVERSATION_ID
uv run b2b-agent resume --scope human_takeover --target CONVERSATION_ID
```

Паузы и human takeover сохраняются после restart. Resume не снимает suppression и не превращает uncertain в повторную отправку. Старый `suppress-email` также поддерживается.

## Каталог и коммерческие границы

Редактируйте `INPUT/services.json`, `proof_assets.json`, `sender_profile.json`, затем запускайте `validate-input`/`reload-catalog`. Пояснения и схемы примеров — в [examples/INPUT/README.md](examples/INPUT/README.md).

Новые услуги добавляются данными; IDs `sales_intake`, `sales_quotes`, `sales_followup`, `documents` сохранены. Возможность, объявленная владельцем, отличается от подтверждённого клиентского результата. Пустой реестр кейсов означает unknown. Демо, личный опыт участника и внедрение нашей компании маркируются отдельно; без права раскрытия материал не используется. Известная цена сообщается прямо, неизвестная не заменяется вымышленным числом или обещанием «под ваш бюджет».

Запрос целого CRM-внедрения не становится полностью подходящим только по слову CRM. Для запросов вне стартовой оптовой ниши оценивается задача и услуга. Исключены порнография, азартные игры, политические и мошеннические проекты. Площадка и заказчик никогда не объединяются по домену площадки.

## Почта, каналы и отписка

Gmail send: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` в защищённых переменных среды; OAuth `gmail.send` с offline-доступом. Gmail inbox отдельно включается `inbox.enabled=true`, `provider=gmail`, ограниченным `query`/известными цепочками. Для чтения необходим дополнительный `gmail.readonly`; права не расширяются автоматически. Недостаточный scope даёт понятную ошибку. Provider message ID, RFC Message-ID и thread ID хранятся отдельно. Replies используют проверенные `In-Reply-To`/`References` и Gmail thread context.

SMTP: TLS/STARTTLS, `OUTREACH_SMTP_USER`, `OUTREACH_SMTP_PASSWORD`. Не используйте секреты в JSON/CLI-аргументах или чате. Подключение почтового приложения к ChatGPT не передаёт автоматически credentials Python-процессу.

По умолчанию доступна reply-отписка «не писать». `unsubscribe.mode=mailto` требует реального обслуживаемого адреса. Mailto открывает почтовый клиент и не объявляется RFC 8058 one-click. Код HTTPS endpoint и защищённого cursor-sync реализован и протестирован offline; GET/HEAD не меняют состояние, POST применяет долговечный scoped suppression. Реальный публичный endpoint, секреты, DKIM и подключённый ledger настраиваются отдельно. После настройки выполните `prepare-unsubscribe ACTION_ID` до review/approval: защищённый API выдаст scoped token, а точная ссылка и заголовки закрепятся в черновике. HTTPS-отправка всегда синхронизирует ledger перед передачей транспорту. Без них HTTPS-режим не выдаётся за готовый. В рамках задания сервис и DNS не развёртываются. При отдельном размещении отключите access logs для URL отписки (например, `uvicorn --no-access-log`): токены не должны попадать в технические журналы.

Площадка/LinkedIn/Telegram без разрешённого подключённого адаптера создают manual task. Статусы `awaiting_manual_send` и `sent_confirmed_by_user` различаются. Обязательный отклик через площадку нельзя заменить email. Копирование текста не считается отправкой; подтверждение ручной отправки — отдельная операция `confirm-manual-send`.

## Стоимость, статистика и память

`budget.tool_prices` хранит подтверждённый тариф/верхнюю оценку: например, для `llm` или `search` поля `per_call`, `currency`, `source`; для token pricing — `input_per_million` и `output_per_million`. Неизвестная цена платного инструмента ставит его на hold. BudgetLedger резервирует верхнюю стоимость до вызова и учитывает фактическую после; неизвестный исход сохраняет резерв. Валюты не суммируются без курса. Ограничиваются деньги на запуск/кампанию/компанию, вызовы, токены, шаги исследования и черновики. Повторный поиск использует ограниченный по сроку кэш; повторный tick неизменившейся очереди не вызывает LLM.

CompanyStats строится из SQLite-событий и сообщений; CSV/Markdown STATS — генерируемый экспорт. Компании, контакты, сообщения, человеческие/автоматические ответы и proposed/scheduled/held/no-show учитываются раздельно. Доли содержат знаменатель и окно. Доставляемость и complaints остаются unknown без наблюдений. Open rate не подтверждает интерес.

CompanyCapsule хранит короткие структурированные сведения с source refs. Для передачи модели проверяется общий input budget вместе с system/tools и резервом ответа. Используется явно консервативная верхняя оценка по UTF-8 байтам; она может потребовать повысить заданный предел или выбрать адресное извлечение/ручной разбор. Критические суммы, ограничения и отказы не обрезаются. Детерминированные шаблоны используют полное структурированное локальное состояние, без передачи переписки модели. Полное ТЗ и полный каталог не включаются в runtime prompt.

## Хранение, миграции и проверки

SQLite мигрируется автоматически и транзакционно при открытии Outbox. Старые `outbound_messages`, ключи, `sending`/`uncertain`, suppressions и досье сохраняются. Ограничение `source_url UNIQUE` заменено ограничениями логического initial/сообщения/действия; ответы в активном диалоге не блокируются 30-дневным initial cooldown. Версии research/evidence/offer/context сохраняются для аудита. Удалять БД для обновления не нужно.

```bash
uv sync --frozen --extra dev
uv run pytest -q
uv run ruff check src tests examples/offline
uv lock --check --offline
git diff --check
```

При недоступном домашнем каталоге задайте `CRAWL4_AI_BASE_DIRECTORY` в доступную рабочую папку. Браузер/Chromium для HTTP Crawl4AI не нужен. SSRF/robots/rate limits, TLS, параметризованный SQL и CSV formula escaping сохранены.

Ограничения: эвристический классификатор отправляет неоднозначности человеку; live OAuth/доставляемость в этой задаче не проверялись. HTML crawler не извлекает PDF/XLSX/вложения, не обходит CAPTCHA/авторизацию. Календарь, официальные platform adapters и расширенное расписание — P2. Внешняя автоматизация ChatGPT не создаётся. Уже переданное провайдеру письмо нельзя атомарно отозвать при позднее полученном отказе.
