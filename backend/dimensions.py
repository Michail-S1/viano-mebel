"""Size filters keep supplier notation and only interpret axes/units explicitly."""
import math
import re
from collections import Counter
from functools import lru_cache

NUMBER = r"\d+(?:[.,]\d+)?"
UNITS = r"(?:мм|mm|см|cm|м|m)"
VALUE = re.compile(r"(?<![\d.,])(" + NUMBER + r")\s*(" + UNITS + r")?(?![\d.,])", re.I)
LABEL = re.compile(r"(?<!\w)(ширина|высота|глубина|ш|в|г)\s*[:=.-]?\s*(" + NUMBER + r")\s*(" + UNITS + r")?(?![\d.,])", re.I)
ORDER = re.compile(r"(?<!\w)([швг](?:\s*[xх×*/]\s*[швг]){1,2})(?!\w)", re.I)
AXIS = {"ш": "width", "ширина": "width", "в": "height", "высота": "height", "г": "depth", "глубина": "depth"}
UNIT = {"мм": 1, "mm": 1, "см": 10, "cm": 10, "м": 1000, "m": 1000}
ORDERS = {"": (), "whd": ("width", "height", "depth"), "wdh": ("width", "depth", "height"),
          "hwd": ("height", "width", "depth"), "hdw": ("height", "depth", "width"),
          "dwh": ("depth", "width", "height"), "dhw": ("depth", "height", "width")}

def clean(value):
    return re.sub(r"\(\s*№[^)]*\)", "", str(value or "").lower()).replace("\u00a0", " ")

@lru_cache(maxsize=50000)
def numbers(value):
    # Separators have no effect; a whole value must match (800 never matches 1800).
    return tuple(float(m.group(1).replace(",", ".")) for m in VALUE.finditer(clean(value)))

@lru_cache(maxsize=50000)
def axes_mm(value, order="", source_unit=""):
    text = clean(value)
    unit_hits = [m.group(2) for m in VALUE.finditer(text) if m.group(2)] + re.findall(r"(?<!\w)" + UNITS + r"(?!\w)", text)
    common_unit = UNIT.get(unit_hits[0], 0) if len(set(unit_hits)) == 1 else UNIT.get(source_unit, 0)
    result = {}
    stated_order = ORDER.search(text)
    labelled = [] if stated_order else list(LABEL.finditer(text))
    if labelled:
        for match in labelled:
            axis = AXIS[match.group(1)]
            scale = UNIT.get(match.group(3), common_unit)
            number = float(match.group(2).replace(",", ".")) * scale
            if scale and axis not in result and math.isfinite(number):
                result[axis] = number
            elif axis in result:
                result[axis] = None  # conflicting values are not a single dimension
        return {key: number for key, number in result.items() if number is not None}
    axis_order = tuple(AXIS[c] for c in re.findall("[швг]", stated_order.group(1))) if stated_order else ORDERS[order]
    matches = list(VALUE.finditer(text))
    if not axis_order or len(matches) != len(axis_order) or len(set(axis_order)) != len(axis_order):
        return {}
    for axis, match in zip(axis_order, matches):
        scale = UNIT.get(match.group(2), common_unit)
        number = float(match.group(1).replace(",", ".")) * scale
        if scale and math.isfinite(number):
            result[axis] = number
    return result

def make_matcher(params):
    query = str(params.get("dimensions", "")).strip()
    if len(query) > 200:
        raise ValueError("Запись размеров слишком длинная.")
    requested = Counter(numbers(query))
    if query and not requested:
        raise ValueError("Введите числа размеров, например 800×2200×520.")
    if requested and not all(math.isfinite(n) and n > 0 for n in requested):
        raise ValueError("Размеры должны быть положительными числами.")
    order = params.get("dimension_order", "")
    source_unit = params.get("dimension_unit", "")
    if order not in ORDERS or source_unit not in ("", "mm", "cm", "m"):
        raise ValueError("Выберите корректные порядок размеров и единицы прайса.")
    ranges = {}
    for axis in ("width", "height", "depth"):
        low, high = None, None
        for suffix in ("min", "max"):
            raw = params.get(axis + "_" + suffix)
            if raw not in (None, ""):
                try:
                    number = float(str(raw).replace(",", "."))
                except (TypeError, ValueError):
                    raise ValueError("Укажите корректные границы размеров.")
                if not math.isfinite(number) or number <= 0 or number > 10000000:
                    raise ValueError("Границы размеров должны быть положительными числами.")
                if suffix == "min": low = number
                else: high = number
        if low is not None and high is not None and low > high:
            raise ValueError("Размер «от» не может быть больше размера «до».")
        if low is not None or high is not None:
            ranges[axis] = (low, high)
    if not requested and not ranges:
        return None
    def matches(value):
        if requested and requested - Counter(numbers(value)):
            return False
        if ranges:
            actual = axes_mm(value, order, source_unit)
            for axis, (low, high) in ranges.items():
                number = actual.get(axis)
                if number is None or (low is not None and number < low - 1e-7) or (high is not None and number > high + 1e-7):
                    return False
        return True
    return matches
