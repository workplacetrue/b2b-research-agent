# Проверка версии 2.1

Baseline: commit `b282dbbf1f16ccf3365121820f437a7fbce3e46f`, ветка `feat/ethical-auto-outreach`, 55 offline-тестов прошли. Для managed cloud кэш Crawl4AI направлен в рабочую папку через `CRAWL4_AI_BASE_DIRECTORY`.

Новые проверки используют временные SQLite, независимые подключения, fake clock и mock HTTP/Gmail/SMTP. Не нужны платные API, почтовые credentials, браузер или реальные письма. Сквозной пример — `examples/offline/demo.py`; он запрещает любой сетевой вызов, отличает simulated_sent от принятия провайдером и создаёт brief/статистику.

Проверяются: совместимая миграция старой схемы; exact one-use approval; адресные catalog/proof/sender invalidations; suppression/pause после подготовки и непосредственно перед SMTP/Gmail; неизвестная стоимость и бюджеты; полный новый текст/quoted history; Gmail scopes/pagination/history reset; повторный импорт; ручной оператор; multi-label ответы; реальные границы предложений/кейсов; явные timezone/meeting события; CSV escaping и исследовательский baseline.

Результаты полного финального запуска фиксируются в PR. Подробная матрица сценариев задания — `acceptance-v2.1.md`.

Live Gmail OAuth, реальная доставляемость, публичный HTTPS unsubscribe/DKIM и календарь не проверялись и не объявляются подключёнными. HTTPS endpoint не развёрнут, DNS не менялся; reply/import-отписка работает локально. GitHub-публикация выполняется отдельной веткой/PR, без автоматического merge.

## Итоговый запуск 12 ноября 2026

- `CRAWL4_AI_BASE_DIRECTORY=/workspace/work/crawl4ai .venv/bin/pytest -q`: **225 passed**, 6.79 s.
- `.venv/bin/ruff check src tests examples/offline`: passed.
- `UV_CACHE_DIR=/workspace/work/uv-cache uv sync --frozen --extra dev`: passed.
- `UV_CACHE_DIR=/workspace/work/uv-cache uv lock --check --offline`: passed.
- `git diff --check`: passed.
- `examples/offline/demo.py`: 0 provider-accepted messages, 2 simulated messages, 1 human reply, 1 proposed / 1 scheduled / 1 held meeting event; brief и STATS выгружены.
- Защищённый HTTPS ledger → exact reviewed footer → mock Gmail и отказ до dispatch проверены дополнительным сквозным тестом. Настоящий endpoint не развёрнут.

## Интеграция пользовательского плана — 17 ноября 2026

База: `ce6c1f1`, ветка `test/outreach-v2.1`. Исходный план сохранён в
`docs/testing-plan-2026-11-17.md`; запуск и ограничения — `evaluation/README.md`.

Реально выполнено в окружении Python 3.12.14:

- `UV_CACHE_DIR=/workspace/work/uv-cache uv sync --frozen --extra dev` — зависимости установлены ранее на этой базе.
- `CRAWL4_AI_BASE_DIRECTORY=/workspace/work/crawl4ai .venv/bin/pytest -q tests/test_plan_regressions.py` — 13 passed.
- `CRAWL4_AI_BASE_DIRECTORY=/workspace/work/crawl4ai .venv/bin/pytest -q tests/test_evaluation_harness.py` — 10 passed.
- `.venv/bin/python -m tools.run_acceptance --output /workspace/work/acceptance-plan-20261117-final` — **248 passed**, все 62 сопоставления offline completed, сеть запрещена pytest plugin.
- `.venv/bin/ruff check src tests examples/offline tools` — passed.
- `UV_CACHE_DIR=/workspace/work/uv-cache uv lock --check --offline` — passed.
- `git diff --check` — passed.
- `.venv/bin/python -m tools.run_model_eval --help` — CLI доступен; платный запуск не выполнялся.

Новых провалов приложения на выполненном наборе не обнаружено. Тесты стенда отдельно
доказали, что отсутствующий test ref, провал параметризованного варианта, противоречивый
pass судьи, неверный JSON, чужой case ID и несуществующий evidence ref не дают ложный pass.
Fake provider прогоняет production analyze и отдельного judge с раздельным бюджетом;
это проверка стенда, а не настоящий LLM-результат.

Пробелы: LLM intent/action runtime отсутствует (исследовательская LLM не заменяет его),
judge calibration и human review не выполнены, полный набор перефразировок и всех вариантов
approval/channel не реализован. Смысловая оценка, реальные модели и живые capabilities:
**not_run**. Матрица явно partial; release_acceptance=blocked. Живые сообщения не отправлялись,
расходы агента и судьи в offline-прогоне — 0.
