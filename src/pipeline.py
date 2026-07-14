"""
Пайплайн мультиагентной системы оценки перспективности стартапов.

Читает таблицу со стартапами (Excel/CSV), прогоняет каждый через 5 агентов
Claude API в последовательно-параллельной схеме:

    Разведчик -> [Технолог, Рынок, Команда] -> Интегратор

Сохраняет сырой JSON-вывод каждого агента по каждому стартапу (для аудита
и ablation study) и сводную таблицу с итоговыми оценками.

Использование (из корня проекта):
    python src/pipeline.py --input Data/Startups.xlsx --limit 20
    python src/pipeline.py --input Data/Startups.xlsx --output-dir results
"""

import argparse
import concurrent.futures
import json
from pathlib import Path

import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

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
TECH_FIELDS = [
    "pat_rus_2018", "pat_rus_2019", "pat_rus_2020", "pat_rus_2021",
    "pat_rus_2022", "pat_rus_2023", "pat_rus_2024",
    "pat_cagr_rus_18_24", "pat_rus_total", "pat_query_ru",
    "pub_2018", "pub_2019", "pub_2020", "pub_2021",
    "pub_2022", "pub_2023", "pub_2024",
    "pub_cagr_18_24", "pub_cite_avg", "pub_total", "pub_query_en",
    "pat_foreign_2018", "pat_foreign_2019", "pat_foreign_2020", "pat_foreign_2021",
    "pat_foreign_2022", "pat_foreign_2023", "pat_foreign_2024",
    "pat_foreign_cagr", "pat_foreign_total", "pat_foreign_query",
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
    },
    "required": [
        "startup_name", "domain", "business_model", "target_customer",
        "development_stage_signal", "refined_keywords",
        "data_quality_flags", "summary",
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


def call_agent(client: Anthropic, system_prompt: str, payload: dict, schema: dict, agent_name: str) -> dict:
    """Вызывает одного агента с structured output. При ошибке API или парсинга
    возвращает словарь с ключом _error, не прерывая обработку остальных стартапов."""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)
    except Exception as e:
        return {"_error": f"{agent_name}: {type(e).__name__}: {e}"}


def process_startup(client: Anthropic, row: pd.Series) -> dict:
    scout_payload = select_fields(row, SCOUT_FIELDS)
    scout_result = call_agent(client, SCOUT_PROMPT, scout_payload, SCOUT_SCHEMA, "Scout")

    shared_context = {"профиль_разведчика": scout_result}
    tech_payload = {**select_fields(row, TECH_FIELDS), **shared_context}
    market_payload = {**select_fields(row, MARKET_FIELDS), **shared_context}
    team_payload = {**select_fields(row, TEAM_FIELDS), **shared_context}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            "technologist": executor.submit(call_agent, client, TECH_PROMPT, tech_payload, TECH_SCHEMA, "Technologist"),
            "market": executor.submit(call_agent, client, MARKET_PROMPT, market_payload, MARKET_SCHEMA, "Market"),
            "team": executor.submit(call_agent, client, TEAM_PROMPT, team_payload, TEAM_SCHEMA, "Team"),
        }
        parallel_results = {name: f.result() for name, f in futures.items()}

    integrator_payload = {
        "профиль_разведчика": scout_result,
        "вывод_технолога": parallel_results["technologist"],
        "вывод_рынка": parallel_results["market"],
        "вывод_команды": parallel_results["team"],
    }
    integrator_result = call_agent(client, INTEGRATOR_PROMPT, integrator_payload, INTEGRATOR_SCHEMA, "Integrator")

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
        d["_error"] for d in (scout, tech, market, team, integrator) if "_error" in d
    ]
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


def main():
    parser = argparse.ArgumentParser(description="Пайплайн МАС для оценки стартапов")
    parser.add_argument("--input", default="Data/Startups.xlsx", help="Путь к входной таблице (xlsx/csv)")
    parser.add_argument("--output-dir", default="results", help="Куда сохранять результаты")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число стартапов (для отладки)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    client = Anthropic()  # берёт ANTHROPIC_API_KEY из окружения

    input_path = PROJECT_ROOT / args.input
    df = load_table(input_path)
    if args.limit:
        df = df.head(args.limit)

    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows = []
    total = len(df)
    for position, (idx, row) in enumerate(df.iterrows(), start=1):
        name = row.get("Name", f"row_{idx}")
        print(f"[{position}/{total}] {name}")

        result = process_startup(client, row)

        with open(output_dir / f"{idx:04d}.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        summary_rows.append(to_summary_row(idx, name, result))

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(output_dir / "results.csv", index=False)
    print(f"\nГотово: {len(summary_rows)} стартапов обработано, результаты в {output_dir}/")


if __name__ == "__main__":
    main()
