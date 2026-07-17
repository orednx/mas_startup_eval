"""
Пайплайн мультиагентной системы оценки перспективности стартапов.

Читает таблицу со стартапами (Excel/CSV), прогоняет каждый через 5 агентов
в последовательно-параллельной схеме:

    Разведчик -> [Технолог, Рынок, Команда] -> Интегратор

Модель вызывается через OpenRouter (OpenAI-совместимый API), а не напрямую
через Anthropic API — верификация личного аккаунта Anthropic не пройдена,
но OpenRouter отдаёт ту же модель (anthropic/claude-sonnet-4.6) через
собственные договорённости, в обход этой блокировки.

Сохраняет сырой JSON-вывод каждого агента по каждому стартапу (для аудита
и ablation study) и сводную таблицу с итоговыми оценками.

Использование (из корня проекта):
    python src/pipeline.py --input Data/Startups.xlsx --limit 20
    python src/pipeline.py --input Data/Startups.xlsx --output-dir results
"""

import argparse
import concurrent.futures
import json
import os
import random
import time
from pathlib import Path

import pandas as pd
from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError
from dotenv import load_dotenv

from preprocessing import YEARS, compute_trend

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "anthropic/claude-sonnet-4.6"
MAX_TOKENS = 2048

# Провайдеры, через которых OpenRouter роутит модель, иногда отдают временный
# 429 (shared rate limit) или 5xx — это не повод сразу считать вызов агента
# неудачным и приближать общий прогон к FAILURE_THRESHOLD.
MAX_API_RETRIES = 5
RETRY_BASE_DELAY = 5.0

# Белые списки полей, передаваемых каждому агенту. Поля, палящие исход или
# принадлежность к группе (Уровень_исхода_0_3, Верифицирован, Источник_исхода,
# Комментарий_исхода, url, топ_программа, число попаданий в топ), сюда
# намеренно не включены и агентам никогда не передаются — они физически
# отсутствуют в Data/Startups.xlsx и хранятся отдельно у пользователя для
# валидации после прогона.
SCOUT_FIELDS = [
    "Name", "суть_проекта", "ключевые_слова_nlp", "рынок",
    "основатель", "вуз", "регион", "год",
]
# pat_cagr_rus_18_24 / pat_foreign_cagr / pub_cagr_18_24 / *_total намеренно
# не в списке — это "сырой" CAGR/total из исходных данных, который даёт
# аномальные значения при малой базе. Вместо них Технологу передаются
# предвычисленные показатели из preprocessing.compute_trend() (см. build_tech_payload).
TECH_FIELDS = [
    "pat_rus_2018", "pat_rus_2019", "pat_rus_2020", "pat_rus_2021",
    "pat_rus_2022", "pat_rus_2023", "pat_rus_2024", "pat_query_ru",
    "pub_2018", "pub_2019", "pub_2020", "pub_2021",
    "pub_2022", "pub_2023", "pub_2024", "pub_cite_avg", "pub_query_en",
    "pat_foreign_2018", "pat_foreign_2019", "pat_foreign_2020", "pat_foreign_2021",
    "pat_foreign_2022", "pat_foreign_2023", "pat_foreign_2024", "pat_foreign_query",
]
MARKET_FIELDS = ["рынок", "инвестиции", "регион", "год"]
TEAM_FIELDS = ["основатель", "вуз", "регион", "год"]

SCOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "startup_name": {"type": "string"},
        "domain": {"type": "string"},
        "business_model": {"type": "string"},
        "target_customer": {"type": "string"},
        "development_stage_signal": {
            "type": "string",
            "enum": [
                "идея", "MVP/прототип", "пилот с клиентами",
                "масштабирование", "есть выручка", "не определено",
            ],
        },
        "refined_keywords": {"type": "array", "items": {"type": "string"}},
        "data_quality_flags": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "input_completeness": {
            "type": "string",
            "enum": ["полные_данные", "только_ключевые_слова", "минимальные_данные"],
        },
    },
    "required": [
        "startup_name", "domain", "business_model", "target_customer",
        "development_stage_signal", "refined_keywords",
        "data_quality_flags", "summary", "input_completeness",
    ],
    "additionalProperties": False,
}

TECH_SCHEMA = {
    "type": "object",
    "properties": {
        "ru_patent_trend": {"type": "string", "enum": ["растущий", "стабильный", "снижающийся", "нет данных"]},
        "global_publication_trend": {"type": "string", "enum": ["растущий", "стабильный", "снижающийся", "нет данных"]},
        "foreign_patent_trend": {"type": "string", "enum": ["растущий", "стабильный", "снижающийся", "нет данных"]},
        "niche_scale": {
            "type": "string",
            "enum": ["крупная устоявшаяся область", "средняя ниша", "узкая/зарождающаяся ниша", "нет данных"],
        },
        "ru_vs_global_alignment": {"type": "string"},
        "innovation_momentum_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "confidence": {"type": "string", "enum": ["высокая", "средняя", "низкая"]},
        "key_signals": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": [
        "ru_patent_trend", "global_publication_trend", "foreign_patent_trend",
        "niche_scale", "ru_vs_global_alignment", "innovation_momentum_score",
        "confidence", "key_signals", "reasoning",
    ],
    "additionalProperties": False,
}

MARKET_SCHEMA = {
    "type": "object",
    "properties": {
        "market_context_summary": {"type": "string"},
        "investment_plan_summary": {"type": "string"},
        "investment_plan_clarity": {
            "type": "string",
            "enum": ["чёткий и обоснованный", "общий/расплывчатый", "нет данных"],
        },
        "regional_context": {"type": "string"},
        "market_attractiveness_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "confidence": {"type": "string", "enum": ["высокая", "средняя", "низкая"]},
        "key_signals": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": [
        "market_context_summary", "investment_plan_summary", "investment_plan_clarity",
        "regional_context", "market_attractiveness_score", "confidence",
        "key_signals", "reasoning",
    ],
    "additionalProperties": False,
}

TEAM_SCHEMA = {
    "type": "object",
    "properties": {
        "team_size_signal": {"type": "string", "enum": ["один основатель", "несколько основателей", "не определено"]},
        "university_signal": {"type": "string"},
        "team_strength_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "confidence": {"type": "string", "enum": ["низкая", "средняя", "высокая"]},
        "data_limitations": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "team_size_signal", "university_signal", "team_strength_score",
        "confidence", "data_limitations", "reasoning",
    ],
    "additionalProperties": False,
}

INTEGRATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "final_score": {"type": "integer", "enum": [0, 1, 2, 3]},
        "score_label": {
            "type": "string",
            "enum": ["закрылся", "выживает без роста", "растёт/инвестиции", "лидер/exit"],
        },
        "confidence": {"type": "string", "enum": ["высокая", "средняя", "низкая"]},
        "missing_dimensions": {"type": "array", "items": {"type": "string"}},
        "dimension_summary": {
            "type": "object",
            "properties": {
                "технология": {"type": "string"},
                "рынок": {"type": "string"},
                "команда": {"type": "string"},
            },
            "required": ["технология", "рынок", "команда"],
            "additionalProperties": False,
        },
        "key_strengths": {"type": "array", "items": {"type": "string"}},
        "key_risks": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": [
        "final_score", "score_label", "confidence", "missing_dimensions",
        "dimension_summary", "key_strengths", "key_risks", "rationale",
    ],
    "additionalProperties": False,
}


def load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


SCOUT_PROMPT = load_prompt("01_scout.txt")
TECH_PROMPT = load_prompt("02_technologist.txt")
MARKET_PROMPT = load_prompt("03_market.txt")
TEAM_PROMPT = load_prompt("04_team.txt")
INTEGRATOR_PROMPT = load_prompt("05_integrator.txt")


def select_fields(row: pd.Series, fields: list[str]) -> dict:
    """Берёт только явно перечисленные поля из строки и заменяет NaN на None
    для корректной сериализации в JSON."""
    out = {}
    for f in fields:
        v = row.get(f)
        out[f] = None if pd.isna(v) else v
    return out


def _is_retryable(e: Exception) -> bool:
    """429 (shared rate limit у провайдера на OpenRouter) и 5xx — временные,
    стоит повторить. 4xx кроме 429 (неверный ключ, невалидная схема и т.п.) —
    повторять бессмысленно, ошибка будет той же."""
    if isinstance(e, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(e, APIStatusError):
        return e.status_code == 429 or e.status_code >= 500
    return False


def call_agent(client: OpenAI, system_prompt: str, payload: dict, schema: dict, agent_name: str, model: str = MODEL) -> dict:
    """Вызывает одного агента с structured output через OpenRouter, с ретраями
    на временные сбои (429/5xx у апстрим-провайдера). При невосстановимой
    ошибке API или парсинга возвращает словарь с ключом _error, не прерывая
    обработку остальных стартапов. default=str в json.dumps — часть числовых
    колонок (год, pat_rus_*, pub_*) приходит из pandas как numpy int64,
    который стандартный json.dumps не сериализует."""
    last_error = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2, default=str)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": agent_name.lower(), "strict": True, "schema": schema},
                },
            )
            text = response.choices[0].message.content
            return json.loads(text)
        except Exception as e:
            last_error = e
            if attempt == MAX_API_RETRIES or not _is_retryable(e):
                break
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
            time.sleep(delay)
    return {"_error": f"{agent_name}: {type(last_error).__name__}: {last_error}"}


def build_tech_payload(row: pd.Series, scout_result: dict) -> dict:
    """Сырые ряды по трём источникам (для контекста) + предвычисленные в Python
    показатели тренда (total/суммы по периодам/growth_ratio/low_base/trend_label),
    чтобы Технологу не приходилось самому считать динамику по сырым числам."""
    payload = select_fields(row, TECH_FIELDS)
    payload["pat_rus_indicators"] = compute_trend({y: row.get(f"pat_rus_{y}") for y in YEARS})
    payload["pat_foreign_indicators"] = compute_trend({y: row.get(f"pat_foreign_{y}") for y in YEARS})
    payload["pub_indicators"] = compute_trend({y: row.get(f"pub_{y}") for y in YEARS})
    payload["профиль_разведчика"] = scout_result
    return payload


def process_startup(client: OpenAI, row: pd.Series, skip_agents: list[str] = [], model: str = MODEL) -> dict:
    """skip_agents — имена специалистов ("technologist"/"market"/"team"), которые
    нужно пропустить для ablation study. Пропущенный агент даёт None и на входе
    Интегратора, и в сохранённом результате — Интегратор промптом умеет работать
    с отсутствующим измерением (см. missing_dimensions в его схеме)."""
    scout_payload = select_fields(row, SCOUT_FIELDS)
    scout_result = call_agent(client, SCOUT_PROMPT, scout_payload, SCOUT_SCHEMA, "Scout", model)

    shared_context = {"профиль_разведчика": scout_result}
    tech_payload = build_tech_payload(row, scout_result)
    market_payload = {**select_fields(row, MARKET_FIELDS), **shared_context}
    team_payload = {**select_fields(row, TEAM_FIELDS), **shared_context}

    agent_specs = {
        "technologist": (TECH_PROMPT, tech_payload, TECH_SCHEMA),
        "market": (MARKET_PROMPT, market_payload, MARKET_SCHEMA),
        "team": (TEAM_PROMPT, team_payload, TEAM_SCHEMA),
    }

    parallel_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        for name, (prompt, payload, schema) in agent_specs.items():
            if name in skip_agents:
                parallel_results[name] = None
            else:
                futures[name] = executor.submit(call_agent, client, prompt, payload, schema, name.capitalize(), model)
        for name, f in futures.items():
            parallel_results[name] = f.result()

    integrator_payload = {
        "профиль_разведчика": scout_result,
        "вывод_технолога": parallel_results["technologist"],
        "вывод_рынка": parallel_results["market"],
        "вывод_команды": parallel_results["team"],
    }
    integrator_result = call_agent(client, INTEGRATOR_PROMPT, integrator_payload, INTEGRATOR_SCHEMA, "Integrator", model)

    return {
        "scout": scout_result,
        "technologist": parallel_results["technologist"],
        "market": parallel_results["market"],
        "team": parallel_results["team"],
        "integrator": integrator_result,
    }


def to_summary_row(idx: int, name: str, result: dict) -> dict:
    scout, tech, market, team, integrator = (
        result["scout"], result["technologist"], result["market"], result["team"], result["integrator"]
    )
    errors = [
        d["_error"] for d in (scout, tech, market, team, integrator) if d is not None and "_error" in d
    ]
    tech = tech or {}
    market = market or {}
    team = team or {}
    return {
        "idx": idx,
        "Name": name,
        "final_score": integrator.get("final_score"),
        "score_label": integrator.get("score_label"),
        "integrator_confidence": integrator.get("confidence"),
        "tech_score": tech.get("innovation_momentum_score"),
        "tech_confidence": tech.get("confidence"),
        "market_score": market.get("market_attractiveness_score"),
        "market_confidence": market.get("confidence"),
        "team_score": team.get("team_strength_score"),
        "team_confidence": team.get("confidence"),
        "rationale": integrator.get("rationale"),
        "errors": "; ".join(errors) if errors else "",
    }


def load_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    return pd.read_excel(path)


# Сколько подряд неудачных стартапов означает системную проблему (кончились
# кредиты API, неверный ключ, нет сети), а не разовый сбой одного вызова.
FAILURE_THRESHOLD = 5


def _atomic_write(write_fn, path: Path) -> None:
    """Пишет через временный файл + rename, чтобы обрыв процесса ровно во
    время записи не оставил битый results.csv/JSON вместо предыдущей
    валидной версии."""
    tmp_path = path.parent / (path.name + ".tmp")
    write_fn(tmp_path)
    tmp_path.replace(path)


def save_json(result: dict, path: Path) -> None:
    _atomic_write(lambda p: p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"), path)


def save_results_csv(summary_rows: list[dict], path: Path) -> None:
    _atomic_write(lambda p: pd.DataFrame(summary_rows).to_csv(p, index=False), path)


def is_complete(result: dict) -> bool:
    """Стартап считается успешно посчитанным, если Scout и Integrator
    отработали без ошибок (Technologist/Market/Team могут быть None из-за
    --skip-agent — это не ошибка, а осознанный ablation)."""
    return "_error" not in result.get("scout", {}) and "_error" not in result.get("integrator", {})


def load_cached(path: Path, name: str, model: str) -> dict | None:
    """Возвращает сохранённый результат, если он есть, читается и относится
    к тому же стартапу (Name) и той же модели — иначе None, и стартап будет
    пересчитан. Сверка по Name защищает от коллизий idx при переиспользовании
    --output-dir для другого входного файла; сверка по модели — от того, чтобы
    сравнение моделей (--model) по ошибке подхватило кэш от предыдущей."""
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if cached.get("_name") != name or cached.get("_model") != model:
        return None
    return cached


def main():
    parser = argparse.ArgumentParser(description="Пайплайн МАС для оценки стартапов")
    parser.add_argument("--input", default="Data/Startups.xlsx", help="Путь к входной таблице (xlsx/csv)")
    parser.add_argument("--output-dir", default="results", help="Куда сохранять результаты")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число стартапов (для отладки)")
    parser.add_argument(
        "--skip-agent", nargs="+", choices=["technologist", "market", "team"],
        default=[], help="Пропустить указанных специалистов (для ablation study)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Пересчитать все стартапы заново, даже уже успешно сохранённые (по умолчанию они пропускаются)",
    )
    parser.add_argument(
        "--model", default=MODEL,
        help=f"Модель для всех 5 агентов (по умолчанию {MODEL})",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])

    input_path = PROJECT_ROOT / args.input
    df = load_table(input_path)
    if args.limit:
        df = df.head(args.limit)

    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    results_csv_path = output_dir / "results.csv"

    summary_rows = []
    total = len(df)
    consecutive_failures = 0

    for position, (idx, row) in enumerate(df.iterrows(), start=1):
        name = row.get("Name", f"row_{idx}")
        json_path = output_dir / f"{idx:04d}.json"

        cached = None if args.force else load_cached(json_path, name, args.model)
        if cached is not None and is_complete(cached):
            print(f"[{position}/{total}] {name} — уже посчитан, пропускаю")
            result = cached
        else:
            print(f"[{position}/{total}] {name}")
            result = process_startup(client, row, skip_agents=args.skip_agent, model=args.model)
            result["_name"] = name
            result["_model"] = args.model
            save_json(result, json_path)

            if is_complete(result):
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= FAILURE_THRESHOLD:
                    summary_rows.append(to_summary_row(idx, name, result))
                    save_results_csv(summary_rows, results_csv_path)
                    print(
                        f"\nОСТАНОВКА: {FAILURE_THRESHOLD} стартапов подряд завершились с ошибкой — "
                        f"похоже на системную проблему (кончились кредиты API, неверный ключ, нет сети), "
                        f"а не на разовый сбой. Прогон прерван, всё уже посчитанное сохранено в {output_dir}/. "
                        f"После устранения причины перезапустите ту же команду — уже готовые стартапы "
                        f"пересчитываться не будут."
                    )
                    return

        summary_rows.append(to_summary_row(idx, name, result))
        save_results_csv(summary_rows, results_csv_path)

    print(f"\nГотово: {len(summary_rows)} стартапов обработано, результаты в {output_dir}/")


if __name__ == "__main__":
    main()
