# MAS Startup Eval

Мультиагентная система на Claude API для оценки перспективности российских технологических стартапов (0–3: закрылся / выживает / растёт / лидер-exit).

## Структура

```
prompts/     системные промпты 5 агентов (Scout → Technologist/Market/Team → Integrator)
src/         пайплайн (src/pipeline.py)
Data/        входная таблица со стартапами (xlsx/csv, не в репо)
results/     результаты прогона (JSON на стартап + results.csv, не в репо)
```

## Запуск

```
pip install -r requirements.txt
cp .env.example .env   # вписать ANTHROPIC_API_KEY

python src/pipeline.py --input Data/startups.xlsx --limit 20
```

Флаги: `--input` (путь к таблице), `--output-dir` (по умолчанию `results`), `--limit` (ограничить число стартапов для отладки).

## Архитектура

Агенты не выходят в интернет и не видят реальный исход (`Уровень_исхода_0_3`) — только поля датасета, явно перечисленные в белых списках в `pipeline.py`. Каждый агент возвращает JSON по фиксированной схеме (`output_config.format`), поэтому парсинг ответа надёжен без ретраев на кривой JSON.
