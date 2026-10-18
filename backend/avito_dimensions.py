"""Compare stated outer dimensions, without guessing units or sleeping size."""
import re
from functools import lru_cache

from backend.dimensions import axes_mm, LABEL, AXIS

AXES = {'width': 'Ширина', 'height': 'Высота', 'depth': 'Глубина'}


def dimension_text(value):
    # Sentence punctuation after «мм.» must not make the shared parser fall
    # back to the one-letter unit «м». Preserve decimal punctuation in numbers.
    value = value.replace('—', ':').replace('–', ':')
    return re.sub(r'\b(мм|см|м|mm|cm|m)[.,](?=\s|$)', r'\1 ', value, flags=re.I)


@lru_cache(maxsize=4096)
def description_dimensions(description):
    marker = re.search(r'\b(?:габариты|размеры)\s*:', description, re.I)
    if not marker:
        return {}
    block = description[marker.end():marker.end()+600]
    block = re.split(r'\b(?:цвет|материал\w*|свойства|спальное\s+место)\b', block, maxsplit=1, flags=re.I)[0]
    return axes_mm(dimension_text(block))


def compare(listing, product, plain):
    raw = product.get('dimensions', '')
    unit = listing.get('_dimension_unit', '')
    catalog = axes_mm(dimension_text(raw), source_unit=unit)
    external_raw = listing.get('attributes', {}).get('dimensions', '')
    external = axes_mm(dimension_text(external_raw)) or description_dimensions(plain(listing.get('description', '')))
    raw_catalog = {AXIS[m[1].lower()]: m[2] for m in LABEL.finditer(raw.lower())}
    rows, warnings, conflicts = [], [], []
    for axis, label in AXES.items():
        observed, value = external.get(axis), catalog.get(axis)
        if observed is None and value is None and axis not in raw_catalog:
            continue
        delta = abs(observed-value) if observed is not None and value is not None else None
        row = {'axis': axis, 'label': label, 'listing_mm': observed, 'catalog_mm': value,
               'catalog_raw': raw_catalog.get(axis), 'difference_mm': delta}
        rows.append(row)
        if delta:
            message = f'{label}: объявление {observed:g} мм, каталог {value:g} мм; разница {delta:g} мм'
            if delta <= 10:
                warnings.append(message + '. Возможная неточность — проверить вручную, не считать совпадением')
            else:
                warnings.append(message + '. Существенное расхождение: подтвердите вручную, что это ошибка сведений, а не другой вариант')
        if observed is not None and value is None and catalog:
            warnings.append(label + ': размер каталога или его единицы не определены; требуется ручная проверка')
    if external and raw_catalog and not catalog:
        warnings.append('У габаритов каталога не указаны единицы. Для сравнения выберите их явно; миллиметры автоматически не предполагаются')
    return {'rows': rows, 'warnings': warnings, 'conflicts': conflicts,
            'catalog_unit': unit, 'listing_raw': external_raw, 'catalog_raw': raw}
