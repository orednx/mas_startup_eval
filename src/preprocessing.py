"""
Детерминированный препроцессинг временных рядов патентной и публикационной
активности — вызывается до промпта Агента-Технолога.

Модели ошибаются в арифметике над числовыми рядами, а "сырой" CAGR даёт
аномальные значения при малой базе (например, рост с 2 до 46 патентов даёт
CAGR в тысячи процентов, хотя абсолютные числа крошечные). compute_trend()
считает тренд по отношению сумм за ранний и поздний трёхлетние периоды —
это устойчивее к разовым выбросам и к делению на маленькие числа, чем
CAGR или сравнение крайних точек ряда.
"""

YEARS = list(range(2018, 2025))
EARLY_YEARS = (2018, 2019, 2020)
LATE_YEARS = (2022, 2023, 2024)

# Меньше такого количества событий суммарно за весь период — ниша
# малоизученная/узкая, тренду (в любую сторону) доверять с осторожностью.
LOW_BASE_THRESHOLD = 5
GROWTH_RATIO_UP = 1.3    # сумма позднего периода выше ранней более чем на 30%
GROWTH_RATIO_DOWN = 0.7  # ниже более чем на 30%


def _safe_number(value) -> float:
    """NaN/None/нечисловые значения трактуются как 0 — легитимный случай
    "по этому году событий не найдено", а не пропуск данных."""
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f  # f != f истинно только для NaN


def compute_trend(yearly_values: dict) -> dict:
    """
    yearly_values: {2018: N, 2019: N, ..., 2024: N} — сырые количества по годам
    для одного источника (патенты РФ, зарубежные патенты или публикации).

    Возвращает предвычисленные показатели, которые Технолог использует вместо
    самостоятельного счёта: total, суммы по периодам, growth_ratio и итоговую
    метку тренда — плюс флаг low_base, если данных слишком мало для уверенных
    выводов о динамике.
    """
    values = {y: _safe_number(yearly_values.get(y)) for y in YEARS}
    total = sum(values.values())
    early_sum = sum(values[y] for y in EARLY_YEARS)
    late_sum = sum(values[y] for y in LATE_YEARS)

    if total == 0:
        trend_label = "нет данных"
        growth_ratio = None
    elif early_sum == 0:
        trend_label = "растущий"  # активность появилась там, где её не было
        growth_ratio = None
    else:
        growth_ratio = late_sum / early_sum
        if growth_ratio >= GROWTH_RATIO_UP:
            trend_label = "растущий"
        elif growth_ratio <= GROWTH_RATIO_DOWN:
            trend_label = "снижающийся"
        else:
            trend_label = "стабильный"

    return {
        "total": int(total),
        "early_period_sum_2018_2020": int(early_sum),
        "late_period_sum_2022_2024": int(late_sum),
        "growth_ratio": round(growth_ratio, 2) if growth_ratio is not None else None,
        "low_base": total < LOW_BASE_THRESHOLD,
        "trend_label": trend_label,
    }
