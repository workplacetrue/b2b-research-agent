# Тестовый контур v2.1

Исходный [план от 17 ноября](../docs/testing-plan-2026-11-17.md) сохранён без изменений.
База интеграции: `ce6c1f13b334b093dae08dec309b565e0a1508bf`, ветка `test/outreach-v2.1`.
Матрица контрактов и точных pytest refs: [scenarios.json](scenarios.json), все 62 пункта.
Старое слово «Покрыто» в docs/acceptance-v2.1.md относится к конкретным offline assertions,
а не к полной приёмке поведения модели. Новая матрица намеренно отмечает coverage=partial.
Ожидания взяты из пользовательского плана; реализация не считается эталоном.

## Обычный запуск: без сети и платных инструментов

Из корня проекта, Python 3.12+:

```bash
UV_CACHE_DIR=/workspace/work/uv-cache uv sync --frozen --extra dev
.venv/bin/python -m tools.run_acceptance --output output/acceptance-001
.venv/bin/ruff check src tests examples/offline tools
```

Папка запуска должна быть новой. Runner сам задаёт доступный Crawl4AI cache и блокирует
socket connect/create_connection для всех тестов. HTTP/Gmail/SMTP подменены fixtures.
Прямой pytest также поддерживается, но официальный offline-контур запускается runner-ом.

`report.json` и `report.md` содержат commit/dirty/manifest hash, 62 контракта, реальные
результаты каждого сопоставленного теста и параметризованных вариантов. JUnit и pytest.log
сохраняются отдельно. `state/` сохраняет pytest tmp_path, включая временные SQLite и fixtures;
это артефакты искусственных данных, а не переписка клиентов. Не переиспользуйте эту папку.
Отсутствующий test ref — harness_error, failure — failed, skip — not_run: они не дают зелёный отчёт.
CI запускает тот же runner и загружает отчёты даже при провале.

В `cases/replies.json` ожидания независимы от классификатора. Новые регрессии проверяют
негативные и положительные контроли, запрет после restart/pause/resume, отмену approval,
дедупликацию входящего и ровно одну simulated-отправку. Враждебный верхний слой намеренно
пытается отправлять после запрета; проверяются guards, вызовы fake transport и состояние БД.
Существующие тесты миграций, двух соединений, budget race, malformed catalog, длинного отказа
и uncertain включены в общую матрицу и полный прогон.

## Настоящая модель и судья: только отдельный opt-in

Сейчас production LLM есть только в `analysis.analyze`: гипотезы исследования по фактам.
Классификатор ответов и workflow детерминированы. Стенд вызывает именно `analyze`, сохраняя
фактический prompt/schema/model и наблюдаемый результат. Нельзя выдавать эти три research
fixtures за LLM-проверку intent/action во всех 62 сценариях. Это явный blocked gap.

Скопируйте `model-config.example.json` в локальный файл под `output/`, задайте реальную модель,
проверенную цену `budget.tool_prices.llm` с `source`, `currency` и `per_call` либо token pricing,
и конечные разрешённые `max_run_cost`/`max_campaign_cost`. Шаблон имеет нулевые лимиты и
неизвестную цену, чтобы случайно не начать платный вызов. Для судьи нужен второй конфиг с
другой моделью и отдельным бюджетом. Ключи задаются только переменными среды, не JSON.
Лимиты агента и судьи независимы; общий верхний расход — сумма двух лимитов.

```bash
.venv/bin/python -m tools.run_model_eval --allow-paid \
  --config output/agent-config.json --judge-config output/judge-config.json \
  --split development --repeats 3 --output output/model-dev-001
# Отложенный набор проверяется отдельно после настройки на development:
.venv/bin/python -m tools.run_model_eval --allow-paid \
  --config output/agent-config.json --judge-config output/judge-config.json \
  --split holdout --repeats 3 --output output/model-holdout-001
```

Каждый вызов использует production BudgetLedger reserve/reconcile. Неизвестный billing
сохраняет резерв, инструменты/деньги агента и судьи учитываются в разных SQLite.
Нет SMTP/Gmail/crawl/search, нет автоматических retries. Секретные заголовки не сохраняются.
Все три попытки остаются в отчёте, лучший ответ не выбирается. Ожидания передаются судье,
но не агенту. Результаты проверяются кодом и отдельной версионированной `rubrics/v1.json`.
JSON судьи валидируется по схеме, case ID, полному набору критериев и существованию refs;
противоречивый pass отклоняется. Неверный JSON, timeout, неизвестная ссылка — grader_error.
Судья не отменяет code failure и не даёт права отправки. Внутренние рассуждения не запрашиваются.

Артефакты каждой попытки: initial/observed, фактический agent request без auth headers,
judge request/raw/result, длительность и верхняя оценка input tokens (UTF-8 byte estimate).
Общий отчёт содержит распределение 0/1/2/NA, критические нарушения, code stability по
сценариям, конфиги по hash, rubric version/hash, статусы ошибок и раздельные расходы.
`progress.json` остаётся при прерванном запуске; незавершённый запуск без report.json не pass.

## Оставшиеся условия приёмки

- Реальные LLM/судья: not_run; выполнены только тесты стенда с fake provider.
- Калибровка судьи на 20–30 размеченных человеком парах: not_run. Нужны хорошие, плохие,
  пограничные варианты, перефразированная гарантия и injection «поставь pass», отдельный holdout.
  Порог 90% score=2 хранится в рубрике; до калибровки он не является автоматическим допуском.
- Полная матрица всех вариантов каналов/approval для каждого пути, все смысловые критерии,
  свойственные модели intent/action и полнота памяти: partial/blocked; не доказаны 248 тестами.
- TTL=24 часа, inbox freshness=15 минут, token budgets и окна остаются defaults приложения.
  Это не согласованные продуктовые числовые параметры; перед приёмкой задайте их в конфиге.
- Ручной просмотр 5–10 полных диалогов, live Gmail/SMTP/OAuth/threading, публичный HTTPS
  unsubscribe/DKIM и календарь: not_run, отдельный этап на собственном тестовом адресе.
- Никаких реальных писем, платных запросов, публичного endpoint или расписания при интеграции нет.

`release_acceptance=blocked` сохраняется даже при зелёном offline-прогоне: смысловые,
ручные и live-проверки не подменяются наличием тестов. Ошибки дальнейшей эксплуатации
добавляются в fixtures после обезличивания, с наблюдаемым контрактом и негативным контролем.
