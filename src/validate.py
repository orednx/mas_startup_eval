"""
Сверка результатов прогона пайплайна (results/results.csv) с реальными
исходами стартапов.

Реальные исходы приватные и живут вне репозитория — по умолчанию скрипт ищет
их в private/ (эта папка в .gitignore, ничего оттуда в git не попадает).
Обязательные колонки: Name, Уровень_исхода_0_3. Колонка Group (топ/контроль)
опциональна — если её нет, сравнение топ/контроль просто пропускается.

Файл с исходами может быть как отдельной таблицей, так и полной копией с
листами Sheet1 + Answer_Key (как исходно было устроено до разделения на
публичную и приватную часть) — в последнем случае лист Answer_Key находится
автоматически, вручную ничего вырезать не нужно.

Join выполняется по Name, а не по idx: idx — это позиция строки в конкретном
прогоне (полный датасет или подвыборка), она не стабильна между прогонами.

Использование:
    python src/validate.py --results results/results.csv
    python src/validate.py --results results/results.csv --truth /другой/путь.xlsx
    python src/validate.py --results results/results.csv \
        --compare-to results_no_tech/results.csv
"""

import argparse
from pathlib import Path

import pandas as pd
from scipy.stats import mannwhitneyu
from sklearn.metrics import (
    accuracy_score,
    cohen_kappa_score,
    confusion_matrix,
    f1_score,
    recall_score,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRUTH_PATH = PROJECT_ROOT / "private" / "outcomes.xlsx"
LEVELS = [0, 1, 2, 3]


def load_truth(path: Path, sheet: str | None = None) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        if sheet is None:
            sheet_names = pd.ExcelFile(path).sheet_names
            sheet = "Answer_Key" if "Answer_Key" in sheet_names else 0
        df = pd.read_excel(path, sheet_name=sheet)
    missing = {"Name", "Уровень_исхода_0_3"} - set(df.columns)
    if missing:
        raise ValueError(f"В файле с исходами не хватает колонок: {missing}")
    dup = df["Name"][df["Name"].duplicated(keep=False)]
    if len(dup):
        print(f"ВНИМАНИЕ: {len(dup)} дублирующихся Name в файле исходов — берётся первое совпадение.")
        df = df.drop_duplicates(subset="Name", keep="first")
    return df


def load_predictions(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    n_errors = df["final_score"].isna().sum()
    if n_errors:
        print(f"ВНИМАНИЕ: {n_errors} строк без final_score (ошибка агента) — исключены из метрик.")
    return df.dropna(subset=["final_score"])


def merge_with_truth(pred: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    merged = pred.merge(truth, on="Name", how="inner", suffixes=("", "_truth"))
    unmatched_pred = set(pred["Name"]) - set(truth["Name"])
    if unmatched_pred:
        print(f"ВНИМАНИЕ: {len(unmatched_pred)} строк из results.csv не нашли пару в файле исходов.")
    print(f"Сопоставлено {len(merged)} из {len(pred)} строк прогона.")
    return merged


def compute_summary(merged: pd.DataFrame) -> dict:
    y_true = merged["Уровень_исхода_0_3"].astype(int)
    y_pred = merged["final_score"].astype(int)
    return {
        "n": len(merged),
        "qwk": cohen_kappa_score(y_true, y_pred, weights="quadratic"),
        "macro_f1": f1_score(y_true, y_pred, average="macro", zero_division=0),
        "macro_recall": recall_score(y_true, y_pred, average="macro", zero_division=0),
        "accuracy": accuracy_score(y_true, y_pred),
        "within_one": (abs(y_true - y_pred) <= 1).mean(),
    }


def print_classification_report(merged: pd.DataFrame) -> None:
    y_true = merged["Уровень_исхода_0_3"].astype(int)
    y_pred = merged["final_score"].astype(int)
    s = compute_summary(merged)

    print(f"\n=== Метрики качества (N={s['n']}) ===")
    print(f"Quadratic Weighted Kappa:     {s['qwk']:.3f}")
    print(f"Macro F1:                     {s['macro_f1']:.3f}")
    print(f"Macro Recall:                 {s['macro_recall']:.3f}")
    print(f"Accuracy (точное совпадение): {s['accuracy']:.3f}")
    print(f"Accuracy (в пределах ±1):     {s['within_one']:.3f}")

    print("\nRecall по классам:")
    per_class = recall_score(y_true, y_pred, average=None, labels=LEVELS, zero_division=0)
    for level, r in zip(LEVELS, per_class):
        support = int((y_true == level).sum())
        print(f"  уровень {level}: recall={r:.3f} (n={support})")

    print("\nМатрица ошибок (строки — реальный уровень, столбцы — предсказанный):")
    cm = confusion_matrix(y_true, y_pred, labels=LEVELS)
    print("            " + "".join(f"pred={l:<6}" for l in LEVELS))
    for level, row in zip(LEVELS, cm):
        print(f"true={level}      " + "".join(f"{v:<11}" for v in row))


def print_group_comparison(merged: pd.DataFrame) -> None:
    if "Group" not in merged.columns:
        print("\n(колонки Group в файле исходов нет — сравнение топ/контроль пропущено)")
        return

    is_top = merged["Group"].astype(str).str.lower().str.contains("top") & ~merged["Group"].astype(str).str.lower().str.contains("not")
    top_scores = merged.loc[is_top, "final_score"]
    control_scores = merged.loc[~is_top, "final_score"]
    if len(top_scores) == 0 or len(control_scores) == 0:
        print("\n(недостаточно данных по обеим группам для сравнения топ/контроль)")
        return

    stat, p_value = mannwhitneyu(top_scores, control_scores, alternative="two-sided")
    print("\n=== Топ vs контроль (final_score) ===")
    print(f"TOP:     n={len(top_scores)}, среднее={top_scores.mean():.2f}, медиана={top_scores.median():.1f}")
    print(f"Control: n={len(control_scores)}, среднее={control_scores.mean():.2f}, медиана={control_scores.median():.1f}")
    print(f"Mann-Whitney U p-value: {p_value:.4f}")


def print_comparison_table(baseline: dict, other: dict, baseline_label: str, other_label: str) -> None:
    print(f"\n=== Сравнение: {other_label} относительно {baseline_label} ===")
    print(f"{'Метрика':<20}{baseline_label:>15}{other_label:>15}{'delta':>10}")
    for key, label in [
        ("qwk", "QWK"), ("macro_f1", "Macro F1"),
        ("macro_recall", "Macro Recall"), ("accuracy", "Accuracy"),
        ("within_one", "Accuracy ±1"),
    ]:
        delta = other[key] - baseline[key]
        print(f"{label:<20}{baseline[key]:>15.3f}{other[key]:>15.3f}{delta:>+10.3f}")


def main():
    parser = argparse.ArgumentParser(description="Сверка результатов пайплайна с реальными исходами")
    parser.add_argument("--results", required=True, help="Путь к results.csv из прогона пайплайна")
    parser.add_argument(
        "--truth", default=str(DEFAULT_TRUTH_PATH),
        help=f"Путь к приватному файлу с реальными исходами (по умолчанию {DEFAULT_TRUTH_PATH})",
    )
    parser.add_argument(
        "--truth-sheet", default=None,
        help="Имя листа в --truth, если не Answer_Key/автоопределение (для xlsx)",
    )
    parser.add_argument(
        "--compare-to", default=None,
        help="Второй results.csv для сравнения (например, ablation-прогон с --skip-agent)",
    )
    args = parser.parse_args()

    truth = load_truth(Path(args.truth), sheet=args.truth_sheet)
    pred = load_predictions(Path(args.results))
    merged = merge_with_truth(pred, truth)

    print(f"\n########## {args.results} ##########")
    print_classification_report(merged)
    print_group_comparison(merged)

    if args.compare_to:
        pred2 = load_predictions(Path(args.compare_to))
        merged2 = merge_with_truth(pred2, truth)

        print(f"\n########## {args.compare_to} ##########")
        print_classification_report(merged2)

        print_comparison_table(
            compute_summary(merged), compute_summary(merged2),
            Path(args.results).parent.name or "baseline",
            Path(args.compare_to).parent.name or "compare",
        )


if __name__ == "__main__":
    main()
