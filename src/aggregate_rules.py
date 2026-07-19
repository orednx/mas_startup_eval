"""Абляция «Интегратор -> детерминированные правила

Читает сохранённые JSON основного прогона (results/NNNN.json) и строит
final_score без пятого LLM-вызова: среднее оценок специалистов (шкала 1-5)
линейно отображается в шкалу исходов 0-3. Сравнение results_rules.csv с
results.csv показывает добавленную ценность LLM-Интегратора.

Запуск (из корня проекта):
    python src/aggregate_rules.py --results-dir results
"""

import argparse
import json
from pathlib import Path

import pandas as pd


def rule_final_score(tech: int | None, market: int | None, team: int | None) -> int | None:
    """Среднее имеющихся оценок специалистов (1-5) -> 0-3 линейно: 1->0, 5->3."""
    scores = [s for s in (tech, market, team) if s is not None]
    if not scores:
        return None
    mean5 = sum(scores) / len(scores)
    return int(round((mean5 - 1) * 3 / 4))


def main():
    parser = argparse.ArgumentParser(description="Правиловая агрегация вместо Интегратора")
    parser.add_argument("--results-dir", default="results", help="Папка с NNNN.json основного прогона")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    rows = []
    for path in sorted(results_dir.glob("[0-9][0-9][0-9][0-9].json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        tech = (data.get("technologist") or {}).get("innovation_momentum_score")
        market = (data.get("market") or {}).get("market_attractiveness_score")
        team = (data.get("team") or {}).get("team_strength_score")
        rows.append({
            "idx": int(path.stem),
            "Name": data.get("_name"),
            "final_score_rules": rule_final_score(tech, market, team),
            "tech_score": tech,
            "market_score": market,
            "team_score": team,
        })

    out_path = results_dir / "results_rules.csv"
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"Готово: {len(rows)} строк -> {out_path}")


if __name__ == "__main__":
    main()
