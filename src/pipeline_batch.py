"""
Пакетный прогон пайплайна через Batch API — вдвое дешевле синхронного API
(client.messages.create), но идёт не по одной строке за раз, а волнами по
всему датасету сразу, потому что агенты пайплайна зависят друг от друга:

    Волна 1: Scout                         — все строки разом
    Волна 2: Technologist + Market + Team  — все строки разом (нужен вывод волны 1)
    Волна 3: Integrator                    — все строки разом (нужен вывод волн 1-2)

Между волнами скрипт ждёт завершения батча (обычно минуты, иногда до часа).
Состояние каждой волны сохраняется на диск сразу после отправки (batch_id) и
после получения результатов — если скрипт прервать и перезапустить, он не
пересоздаст уже отправленный батч (это была бы двойная оплата), а продолжит
ждать/собирать тот же самый.

Использование:
    python src/pipeline_batch.py --input Data/Startups.xlsx --output-dir results_batch
    python src/pipeline_batch.py --input Data/Startups.xlsx --output-dir results_batch --limit 20
    python src/pipeline_batch.py --skip-agent technologist --output-dir results_batch_no_tech
"""

import argparse
import json
import random
import time
from pathlib import Path

from anthropic import Anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv

import pipeline  # переиспользуем промпты, схемы, белые списки, select_fields, save_json и т.д.

PROJECT_ROOT = pipeline.PROJECT_ROOT
POLL_INTERVAL_SECONDS = 60
MAX_NETWORK_RETRIES = 5


def _with_retries(fn, description: str, max_attempts: int = MAX_NETWORK_RETRIES, base_delay: float = 5.0):
    """Ретраит сетевые вызовы к Batch API с экспоненциальной паузой. Отправка
    и опрос батча иногда падают с временными 5xx/502 на инфраструктуре (уже
    ловили Cloudflare 502 при отправке 352 запросов) — это не повод терять
    результат целой волны и начинать всё заново."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            print(f"  [{description}] сетевая ошибка ({type(e).__name__}: {e}), попытка {attempt}/{max_attempts}, повтор через {delay:.0f}с")
            time.sleep(delay)


def submit_batch(client: Anthropic, requests: list[dict], model: str) -> str:
    batch = _with_retries(lambda: client.messages.batches.create(
        requests=[
            Request(
                custom_id=r["custom_id"],
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=pipeline.MAX_TOKENS,
                    system=r["system"],
                    # см. комментарий в pipeline.call_agent — на моделях с
                    # adaptive thinking по умолчанию это иначе обрезает JSON.
                    thinking={"type": "disabled"},
                    output_config={"format": {"type": "json_schema", "schema": r["schema"]}},
                    messages=[{
                        "role": "user",
                        "content": json.dumps(r["payload"], ensure_ascii=False, indent=2, default=str),
                    }],
                ),
            )
            for r in requests
        ]
    ), description="отправка батча")
    return batch.id


def wait_for_batch(client: Anthropic, batch_id: str) -> None:
    while True:
        batch = _with_retries(lambda: client.messages.batches.retrieve(batch_id), description="опрос батча")
        if batch.processing_status == "ended":
            return
        c = batch.request_counts
        done = c.succeeded + c.errored + c.canceled + c.expired
        print(f"  батч {batch_id}: {batch.processing_status}, готово {done}/{done + c.processing}")
        time.sleep(POLL_INTERVAL_SECONDS)


def collect_batch_results(client: Anthropic, batch_id: str) -> dict:
    """Возвращает {custom_id: распарсенный_json | {'_error': ...}}. Батч уже
    завершён на момент вызова (только чтение) — при сетевом сбое посреди
    итерации безопасно перечитать результаты с нуля."""
    def _fetch():
        out = {}
        for result in client.messages.batches.results(batch_id):
            if result.result.type == "succeeded":
                text = next((b.text for b in result.result.message.content if b.type == "text"), None)
                try:
                    out[result.custom_id] = json.loads(text) if text else {"_error": "пустой ответ"}
                except json.JSONDecodeError as e:
                    out[result.custom_id] = {"_error": f"JSONDecodeError: {e}"}
            else:
                out[result.custom_id] = {"_error": f"batch:{result.result.type}"}
        return out

    return _with_retries(_fetch, description="сбор результатов батча")


def load_or_run_wave(client: Anthropic, output_dir: Path, wave_name: str, build_requests_fn, model: str) -> dict:
    """Общая логика одной волны: если результаты уже на диске — просто их
    читает; если батч уже отправлен ранее (сохранён batch_id) — ждёт и
    собирает его; иначе строит запросы, отправляет новый батч и ждёт.
    Перезапуск скрипта поэтому не создаёт батч заново и не платит дважды."""
    results_path = output_dir / f"{wave_name}_results.json"
    batch_id_path = output_dir / f"{wave_name}_batch_id.txt"

    if results_path.exists():
        print(f"[{wave_name}] результаты уже на диске, пропускаю")
        return json.loads(results_path.read_text(encoding="utf-8"))

    if batch_id_path.exists():
        batch_id = batch_id_path.read_text(encoding="utf-8").strip()
        print(f"[{wave_name}] батч {batch_id} уже отправлен ранее, дожидаюсь")
    else:
        requests = build_requests_fn()
        print(f"[{wave_name}] отправляю батч из {len(requests)} запросов")
        batch_id = submit_batch(client, requests, model)
        batch_id_path.write_text(batch_id, encoding="utf-8")

    wait_for_batch(client, batch_id)
    results = collect_batch_results(client, batch_id)
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{wave_name}] готово: {len(results)} результатов")
    return results


def main():
    parser = argparse.ArgumentParser(description="Пакетный (Batch API) прогон пайплайна МАС")
    parser.add_argument("--input", default="Data/Startups.xlsx", help="Путь к входной таблице (xlsx/csv)")
    parser.add_argument("--output-dir", default="results_batch", help="Куда сохранять результаты")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число стартапов (для отладки)")
    parser.add_argument(
        "--skip-agent", nargs="+", choices=["technologist", "market", "team"],
        default=[], help="Пропустить указанных специалистов (для ablation study)",
    )
    parser.add_argument("--model", default=pipeline.MODEL, help=f"Модель для всех агентов (по умолчанию {pipeline.MODEL})")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    client = Anthropic()

    df = pipeline.load_table(PROJECT_ROOT / args.input)
    if args.limit:
        df = df.head(args.limit)
    rows = list(df.iterrows())  # фиксируем порядок (idx, row) один раз на весь прогон

    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # === Волна 1: Scout ===
    def build_scout_requests():
        return [
            {
                "custom_id": f"scout_{idx}", "system": pipeline.SCOUT_PROMPT,
                "payload": pipeline.select_fields(row, pipeline.SCOUT_FIELDS), "schema": pipeline.SCOUT_SCHEMA,
            }
            for idx, row in rows
        ]

    scout_results = load_or_run_wave(client, output_dir, "wave1_scout", build_scout_requests, args.model)

    # === Волна 2: Technologist + Market + Team ===
    def build_wave2_requests():
        reqs = []
        for idx, row in rows:
            scout_result = scout_results.get(f"scout_{idx}", {"_error": "нет результата Scout"})
            shared = {"профиль_разведчика": scout_result}
            if "technologist" not in args.skip_agent:
                reqs.append({
                    "custom_id": f"technologist_{idx}", "system": pipeline.TECH_PROMPT,
                    "payload": pipeline.build_tech_payload(row, scout_result), "schema": pipeline.TECH_SCHEMA,
                })
            if "market" not in args.skip_agent:
                reqs.append({
                    "custom_id": f"market_{idx}", "system": pipeline.MARKET_PROMPT,
                    "payload": {**pipeline.select_fields(row, pipeline.MARKET_FIELDS), **shared},
                    "schema": pipeline.MARKET_SCHEMA,
                })
            if "team" not in args.skip_agent:
                reqs.append({
                    "custom_id": f"team_{idx}", "system": pipeline.TEAM_PROMPT,
                    "payload": {**pipeline.select_fields(row, pipeline.TEAM_FIELDS), **shared},
                    "schema": pipeline.TEAM_SCHEMA,
                })
        return reqs

    wave2_results = load_or_run_wave(client, output_dir, "wave2_specialists", build_wave2_requests, args.model)

    # === Волна 3: Integrator ===
    def build_integrator_requests():
        reqs = []
        for idx, row in rows:
            scout_result = scout_results.get(f"scout_{idx}", {"_error": "нет результата Scout"})
            integrator_payload = {
                "профиль_разведчика": scout_result,
                "вывод_технолога": wave2_results.get(f"technologist_{idx}"),
                "вывод_рынка": wave2_results.get(f"market_{idx}"),
                "вывод_команды": wave2_results.get(f"team_{idx}"),
            }
            reqs.append({
                "custom_id": f"integrator_{idx}", "system": pipeline.INTEGRATOR_PROMPT,
                "payload": integrator_payload, "schema": pipeline.INTEGRATOR_SCHEMA,
            })
        return reqs

    integrator_results = load_or_run_wave(client, output_dir, "wave3_integrator", build_integrator_requests, args.model)

    # === Сборка финальных результатов — тот же формат, что и у синхронного pipeline.py ===
    print("Собираю финальные результаты...")
    summary_rows = []
    for idx, row in rows:
        name = row.get("Name", f"row_{idx}")
        result = {
            "scout": scout_results.get(f"scout_{idx}", {"_error": "нет результата"}),
            "technologist": wave2_results.get(f"technologist_{idx}"),
            "market": wave2_results.get(f"market_{idx}"),
            "team": wave2_results.get(f"team_{idx}"),
            "integrator": integrator_results.get(f"integrator_{idx}", {"_error": "нет результата"}),
            "_name": name,
            "_model": args.model,
        }
        pipeline.save_json(result, output_dir / f"{idx:04d}.json")
        summary_rows.append(pipeline.to_summary_row(idx, name, result))

    pipeline.save_results_csv(summary_rows, output_dir / "results.csv")
    print(f"\nГотово: {len(summary_rows)} стартапов, результаты в {output_dir}/")


if __name__ == "__main__":
    main()
