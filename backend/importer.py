"""Read furniture price lists without modifying source workbooks.

The importer deliberately keeps variants separate and exposes ambiguous/missing prices
as issues. XLSX is read sparsely from OOXML: supplier files may format all 16,384
columns while containing only two useful columns, so rectangular iteration is unsafe.
"""
from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from .catalog_identity import header_context, normalize_product
from xml.etree import ElementTree as ET

FIELDS = ("name", "price", "sku", "color", "dimensions", "width", "height", "depth", "collection")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MAX_CELLS = 2_000_000
MAX_XML_BYTES = 180_000_000
CELL_REF = re.compile(r"([A-Z]+)([0-9]+)", re.I)
DIMENSION_PATTERN = re.compile(
    r"(?<![\d.,])\d+(?:[,.]\d+)?(?:\s*[хx×*]\s*\d+(?:[,.]\d+)?){1,2}"
    r"(?:\s*(?:мм|см|м)\b)?", re.I)
TRIPLE_SLASH = re.compile(r"(?<!\d)\d{2,5}/\d{2,5}/\d{2,5}(?!\d)")
SERVICE = re.compile(
    r"прай[сc]|прай-лист|оптовый|отдел (?:продаж|рекламац)|@|https?://|"
    r"^звонить\b|писать по вопросам|e-mail|тел(?:ефон)?\s*[:.]|тел[.]?(?:\s|\+)|"
    r"республика крым|г[.]белогорск|нижнегорский|ул[.]|"
    r"итогов|^итого\b|^всего\b|сумма\s*[(]|наценка|скидка\s*[(]|"
    r"содержание|сняти[ея].*производств|важная информация|"
    r"готовое решение.*состоит|рекомендаци|внимание[!:]|"
    r"^цены на|комплектуется|уточняйте|^\*{2,}", re.I)
CATEGORY_NAMES = {
    "спальни", "спальни.", "гостиные", "прихожие", "кухни", "буфеты",
    "кровати", "детские кровати", "столы", "стулья", "аксессуары",
    "дополнения", "продукция", "гардеробные", "шкафы", "комоды", "матрасы",
    "тумбы", "пеналы", "фасады", "корпуса", "фурнитура", "маркетинг",
    "мягкая мебель", "стенки", "модульные кухни", "детские", "полки",
    "столешницы", "готовые решения кухонь", "складская программа",
}
NONPRODUCT_SHEETS = re.compile(
    r"содержани|итого|расчет|расчёт|настройк|сняти[ея]|снятые|важная информац", re.I)


def normalize_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            value = int(value)
    text = unicodedata.normalize("NFC", str(value))
    return re.sub(r"\s+", " ", text.replace("\x00", "").replace("\u200b", "")).strip()


def _identity(value: Any) -> str:
    text = normalize_text(value).casefold().replace("ё", "е")
    text = re.sub(r"\s*([/*хx×])\s*", r"\1", text)
    return text


def product_key(product: dict) -> str:
    """Price/file/row independent ID; SKU is scoped to factory and collection."""
    sku = _identity(product.get("sku"))
    fields = ["sku" if sku else "name", _identity(product.get("factory")),
              _identity(product.get("collection")), sku or _identity(product.get("name")),
              _identity(product.get("color")), _identity(product.get("dimensions")),
              _identity(product.get("variant"))]
    if (product.get("identity_context") or {}).get("kind") == "1c-nomenclature":
        # In this export the article can identify an entire furniture group.
        # The explicit nomenclature distinguishes different models in that group.
        fields.extend(["1c-nomenclature", _identity(product.get("name"))])
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def match_signature(product: dict) -> str:
    """Conservative signature for changed names with the same SKU and variant.

    No fuzzy merging: colours and dimensions remain in the signature. The server
    still compares names/prices and lets the user review conflicting records.
    """
    return product_key(product)


def _col(n: int) -> str:
    result = ""
    while n:
        n, digit = divmod(n - 1, 26)
        result = chr(65 + digit) + result
    return result


def _colnum(label: Any) -> int:
    result = 0
    for character in str(label or "").strip().upper():
        if not "A" <= character <= "Z":
            return 0
        result = result * 26 + ord(character) - 64
    return result if result <= 16384 else 0


def _coords(ref: str) -> tuple[int, int]:
    match = CELL_REF.fullmatch(ref)
    if not match:
        raise ValueError("Некорректная ссылка Excel: " + ref)
    return int(match.group(2)), _colnum(match.group(1))


@dataclass
class Sheet:
    name: str
    rows: dict[int, dict[int, Any]]
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    _merge_rows: dict[int, dict[int, Any]] = field(default_factory=dict)
    cell_indents: dict[tuple[int, int], int] = field(default_factory=dict)

    def __post_init__(self):
        # Only populated merged anchors are expanded. Empty/format-only merged
        # areas are ignored; horizontal merging never changes raw header cells.
        populated = 0
        for first_row, last_row, first_col, last_col in self.merges:
            value = self.rows.get(first_row, {}).get(first_col)
            if value is None or value == "":
                continue
            span = (last_row - first_row + 1) * (last_col - first_col + 1)
            if populated + span > MAX_CELLS:
                continue
            populated += span
            for row in range(first_row, last_row + 1):
                target = self._merge_rows.setdefault(row, {})
                for col in range(first_col, last_col + 1):
                    target[col] = value

    def get(self, row: int, column: Any) -> Any:
        columns = str(column or "").upper().split("+")
        if len(columns) > 1:
            parts = [normalize_text(self.get(row, item)) for item in columns]
            return " + ".join(part for part in parts if part and part != "_")
        col = _colnum(column)
        if not col:
            return None
        return self.rows.get(row, {}).get(col, self._merge_rows.get(row, {}).get(col))

    @property
    def row_numbers(self):
        return sorted(self.rows)


def _xlsx_read(path: Path) -> list[Sheet]:
    sheets = []
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            if item.filename.endswith(".xml") and item.file_size > MAX_XML_BYTES:
                raise ValueError("Слишком большой XML-фрагмент Excel.")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            with archive.open("xl/sharedStrings.xml") as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag == NS + "si":
                        shared.append("".join(part.text or "" for part in element.iter(NS + "t")))
                        element.clear()
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships}
        style_indents = []
        if "xl/styles.xml" in archive.namelist():
            styles = ET.fromstring(archive.read("xl/styles.xml")).find(NS + "cellXfs")
            for style in styles if styles is not None else []:
                alignment = style.find(NS + "alignment")
                style_indents.append(int(alignment.get("indent", 0)) if alignment is not None else 0)
        count = 0
        for item in workbook.findall(NS + "sheets/" + NS + "sheet"):
            rel = item.attrib.get(REL_NS + "id")
            target = targets.get(rel, "")
            xml_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
                posixpath.join("xl", target))
            if not xml_path.startswith("xl/") or xml_path not in archive.namelist():
                continue
            rows: dict[int, dict[int, Any]] = {}
            merges = []
            cell_indents = {}
            with archive.open(xml_path) as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag == NS + "c":
                        reference = element.attrib.get("r", "")
                        if not reference:
                            element.clear()
                            continue
                        row, col = _coords(reference)
                        style = int(element.get("s", 0))
                        indent = style_indents[style] if style < len(style_indents) else 0
                        if indent and col == 1 and len(cell_indents) < MAX_CELLS:
                            cell_indents[(row, col)] = indent
                        kind = element.attrib.get("t", "")
                        value_element = element.find(NS + "v")
                        value = value_element.text if value_element is not None else None
                        if kind == "inlineStr":
                            value = "".join(t.text or "" for t in element.iter(NS + "t"))
                        elif kind == "s" and value is not None:
                            value = shared[int(value)]
                        elif kind == "b":
                            value = bool(int(value or "0"))
                        elif kind not in ("str", "e", "d") and value not in (None, ""):
                            try:
                                value = float(value)
                            except ValueError:
                                pass
                        if value not in (None, ""):
                            rows.setdefault(row, {})[col] = value
                            count += 1
                            if count > MAX_CELLS:
                                raise ValueError("В файле слишком много заполненных ячеек.")
                        elif element.find(NS + "f") is not None:
                            # Preserve a row with a formula lacking a cached value.
                            rows.setdefault(row, {})[col] = None
                        element.clear()
                    elif element.tag == NS + "mergeCell":
                        ref = element.attrib.get("ref", "")
                        first, _, last = ref.partition(":")
                        first_row, first_col = _coords(first)
                        last_row, last_col = _coords(last or first)
                        merges.append((first_row, last_row, first_col, last_col))
                        element.clear()
            sheets.append(Sheet(item.attrib["name"], rows, merges, cell_indents=cell_indents))
    return sheets


def _xls_read(path: Path) -> list[Sheet]:
    try:
        import xlrd
    except ImportError as exc:
        raise ValueError("Для .xls требуется библиотека xlrd. Запустите установку зависимостей.") from exc
    book = xlrd.open_workbook(str(path), formatting_info=True, on_demand=True)
    result = []
    try:
        count = 0
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            rows = {}
            for row in range(sheet.nrows):
                items = {}
                for col in range(sheet.ncols):
                    cell = sheet.cell(row, col)
                    if cell.value == "" or cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        continue
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_ERROR:
                        value = xlrd.error_text_from_code.get(int(value), "#ERROR!")
                    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value = bool(value)
                    items[col + 1] = value
                    count += 1
                    if count > MAX_CELLS:
                        raise ValueError("В файле слишком много заполненных ячеек.")
                if items:
                    rows[row + 1] = items
            merged = [(r1 + 1, r2, c1 + 1, c2) for r1, r2, c1, c2 in sheet.merged_cells]
            result.append(Sheet(sheet.name, rows, merged))
            book.unload_sheet(index)
    finally:
        book.release_resources()
    return result


@lru_cache(maxsize=2)
def _read_cached(path: str, modified_ns: int, size: int) -> list[Sheet]:
    source = Path(path)
    if source.suffix.casefold() == ".xls":
        return _xls_read(source)
    if source.suffix.casefold() == ".xlsx":
        return _xlsx_read(source)
    raise ValueError("Поддерживаются только файлы .xls и .xlsx.")


def _read(path: str) -> list[Sheet]:
    source = Path(path)
    stat = source.stat()
    return _read_cached(str(source.resolve()), stat.st_mtime_ns, stat.st_size)


def _factory(filename: str, sheets: list[Sheet]) -> str:
    text = filename.casefold().replace("ё", "е")
    if re.search(r"регион[\s_-]*058", text):
        return "Регион 058"
    known = [("динар", "Динар"), ("dinar", "Динар"), ("бтс", "БТС"), ("bts", "БТС"), ("регина", "Регина"), ("стенд", "Стендмебель"), ("альбина", "Альбина"),
             ("росток", "Росток"), ("эста", "ЭСТА Мебель"), ("аленушка", "Алёнушка"),
             ("дсв", "ДСВ"), ("миф", "МИФ"), ("пирамида", "Пирамида")]
    for marker, name in known:
        if (re.search(r"(?<![а-яa-z0-9])"+marker+r"(?![а-яa-z0-9])",text) if marker in ("дсв","миф","бтс","bts") else marker in text):
            return name
    if re.search(r"sv[- _]?мебель", text):
        return "SV-Мебель"
    # Unnamed suppliers must be confirmed by the user, not guessed from reseller.
    for sheet in sheets[:2]:
        for row in sheet.row_numbers[:12]:
            for value in sheet.rows[row].values():
                raw = normalize_text(value)
                for marker, name in known:
                    normalized = raw.casefold().replace("ё", "е")
                    if (re.search(r"(?<![а-яa-z0-9])"+marker+r"(?![а-яa-z0-9])",normalized) if marker in ("дсв","миф","бтс","bts") else marker in normalized):
                        return name
    return ""


def _header_identity(value: Any) -> str:
    # Formatting inside headings must not affect product names or product IDs.
    return re.sub(r"[\s\-\u00ad\u2010\u2011]+", "", normalize_text(value).casefold())


def _name_header(value: Any) -> bool:
    label = _header_identity(value)
    return bool(re.search(r"наименован|название|обозначениесекци|^изделия$|^готовыерешениякухонь$", label)) and not bool(
        re.search(r"корпус|фасад|ценов", label))


def _price_score(label: str) -> int:
    text = _header_identity(label)
    if not re.search(r"цен[аы]|стоимость|^ррц$|^1категория$|акция", text):
        return -1
    if "модул" in text or "продукц" in text:
        return 140
    if "ваша" in text or "опт" in text:
        return 130
    if "рознич" in text or "рекоменд" in text or text == "ррц":
        return 20
    if "нацен" in text:
        return 10
    if "скид" in text or "акци" in text:
        return 80
    if "корпус" in text or "фасад" in text:
        return 100
    return 120


def _header(sheet: Sheet) -> tuple[int, dict[str, str | None], list[dict]]:
    from .stock_import import layout
    warehouse = layout(sheet)
    if warehouse:
        return warehouse["header_row"], warehouse["mapping"], warehouse["headers"]
    mapping = dict.fromkeys(FIELDS)
    header_row = 0
    best = -1
    for number in sheet.row_numbers[:80]:
        values = sheet.rows[number]
        name_columns = [col for col, value in values.items() if _name_header(value)]
        prices = [col for col, value in values.items() if _price_score(normalize_text(value)) >= 0]
        # Multi-level kitchen price headings have names on the preceding row.
        nearby_prices = prices + [
            col for col, value in sheet.rows.get(number + 1, {}).items()
            if _price_score(normalize_text(value)) >= 0]
        if name_columns and nearby_prices:
            header_row = number
            break
    if not header_row:
        # A custom mapping can still import an unfamiliar table, but do not
        # silently treat arbitrary unstructured content as a valid price list.
        return 0, mapping, [{"column": _col(c), "label": _col(c)}
                            for c in sorted({c for r in sheet.rows.values() for c in r})[:100]]

    values = sheet.rows[header_row]
    # Pick a populated name column; Pyramid's "Полное наименование" B is empty.
    name_columns = [c for c, v in values.items() if _name_header(v)]
    for c, v in values.items():
        if "наименованиеполное" in _identity(v) or "полное наименование" in _identity(v):
            name_columns.append(c)
    scored_names = [(sum(bool(normalize_text(sheet.rows.get(n, {}).get(c)))
                         for n in sheet.row_numbers if header_row < n <= header_row + 50), c)
                    for c in name_columns]
    # Prefer a complete name column if populated; otherwise the first name.
    preferred_full = [c for c in name_columns if "полн" in _identity(values.get(c))]
    if preferred_full and any(score >= 5 and c in preferred_full for score, c in scored_names):
        mapping["name"] = _col(preferred_full[0])
    elif scored_names:
        maximum = max(score for score, _ in scored_names)
        mapping["name"] = _col(next(c for score, c in scored_names if score == maximum))

    labels = {}
    for c in sorted({c for row in sheet.rows.values() for c in row}):
        top = normalize_text(values.get(c))
        below = normalize_text(sheet.rows.get(header_row + 1, {}).get(c))
        # Treat a second header line only if it contains recognised headings,
        # never append the first product to a label.
        if below and re.search(r"^артикул$|^вес|^цена", below, re.I):
            inherited = normalize_text(sheet._merge_rows.get(header_row, {}).get(c))
            top = " / ".join(v for v in [top or inherited, below] if v)
        if not top:
            top = _col(c)
        labels[c] = top
        label = _header_identity(top)
        if "цвет" in label or "характеристик" in label:
            mapping["color"] = _col(c)
        elif "габарит" in label or "размер" in label or re.search(r"ш\s*[*хx]\s*в\s*[*хx]\s*г", label):
            mapping["dimensions"] = _col(c)
        elif "ширин" in label:
            mapping["width"] = _col(c)
        elif "высот" in label:
            mapping["height"] = _col(c)
        elif "глубин" in label:
            mapping["depth"] = _col(c)
        elif "коллекц" in label or "серия" in label:
            mapping["collection"] = _col(c)
        elif "артикул" in label or label in ("модуль", "код"):
            mapping["sku"] = _col(c)
    prices = [( _price_score(label), c) for c, label in labels.items()
              if _price_score(label) >= 0]
    if prices:
        mapping["price"] = _col(max(prices, key=lambda item: (item[0], -item[1]))[1])
    # Росток: the header is merged C:D, but names are physically in D.
    if mapping["name"] and not any(normalize_text(sheet.rows.get(n, {}).get(_colnum(mapping["name"])))
                                   for n in range(header_row + 1, header_row + 10)):
        for first_row, last_row, first_col, last_col in sheet.merges:
            if first_row <= header_row <= last_row and first_col == _colnum(mapping["name"]):
                for c in range(first_col, last_col + 1):
                    if any(normalize_text(sheet.rows.get(n, {}).get(c))
                           for n in range(header_row + 1, header_row + 10)):
                        mapping["name"] = _col(c)
                        break
    # Stand kitchen composite identity: module = body SKU + facade SKU.
    if mapping["price"] == "D" and "Цена модуля" in labels.get(4, ""):
        mapping["sku"] = "E+H"
    headers = [{"column": _col(c), "label": label} for c, label in labels.items()]
    if mapping["sku"] == "E+H":
        headers.append({"column": "E+H", "label": "Артикулы корпуса + фасада"})
    return header_row, mapping, headers


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = normalize_text(value).casefold()
        text = re.sub(r"(?:руб[.]?|р[.]|₽)$", "", text).strip()
        text = text.replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
            return None
        try:
            result = float(text)
        except ValueError:
            return None
    # Telephones, Excel errors, infinities and negative prices are never money.
    return result if math.isfinite(result) and 0 <= result < 100_000_000 else None


def _dimensions(sheet: Sheet, number: int, mapping: dict, name: str) -> str:
    raw = normalize_text(sheet.get(number, mapping.get("dimensions")))
    if raw:
        return raw
    dimensions = []
    for key, label in (("width", "Ш"), ("height", "В"), ("depth", "Г")):
        value = normalize_text(sheet.get(number, mapping.get(key)))
        if value and _price(value) != 0:
            dimensions.append(f"{label}: {value}")
    if dimensions:
        return "; ".join(dimensions)
    # Rostok prints dimension labels on three physical rows beside a merged name.
    if any(normalize_text(sheet.get(number + offset, "F")) in ("Ш", "В", "Г")
           for offset in range(3)):
        values = []
        for offset in range(3):
            label = normalize_text(sheet.get(number + offset, "F"))
            value = normalize_text(sheet.get(number + offset, "G"))
            if label in ("Ш", "В", "Г") and value:
                values.append(f"{label}: {value}")
        if values:
            return "; ".join(values)
    # Preserve notation exactly, without inferring axes or units from magnitude.
    match = DIMENSION_PATTERN.search(name) or TRIPLE_SLASH.search(name)
    return match.group(0) if match else ""


def _collection_name(value: str) -> str:
    match = re.search(r"(?:коллекци[яи]|модульная (?:система|программа))\s*(.*)", value, re.I)
    if match:
        text = match.group(1).strip()
        quoted = re.search(r'["«]([^"»]+)["»]', text)
        return quoted.group(1) if quoted else text
    return value.rstrip(".")


def _service_name(name: str) -> bool:
    if not name or len(name) < 2 or len(name) > 1500:
        return True
    if SERVICE.search(name):
        return True
    if not re.search(r"[A-Za-zА-Яа-яЁё]", name):
        return True
    if _name_header(name) or _price_score(name) >= 0 and len(name) < 45:
        return True
    return False


def _parse_sheet(sheet: Sheet, filename: str, factory: str, configuration: dict) -> tuple[list[dict], list[dict]]:
    from .stock_import import layout, parse as parse_warehouse
    warehouse = layout(sheet)
    if warehouse:
        return parse_warehouse(sheet, filename, factory, configuration, warehouse)
    header_row, default, headers = _header(sheet)
    user_mapping = configuration.get("mapping") or {}
    mapping = {**default, **{k: v for k, v in user_mapping.items() if k in FIELDS}}
    if not mapping.get("name") or not mapping.get("price"):
        return [], [{"message": "Не выбраны столбцы наименования и цены.",
                     "source_file": filename, "source_sheet": sheet.name, "source_row": header_row or 1}]
    collection = "" if re.fullmatch(r"sheet\d*|лист\d*|tdsheet|основной|кухни|корпус", sheet.name, re.I) else sheet.name
    actual_factory = normalize_text(configuration.get("factory")) or factory
    if factory == "SV-Мебель" and "nn" in sheet.name.casefold():
        actual_factory = "NN-Мебель"
    label_map = {h["column"]: h["label"] for h in headers}
    price_label = label_map.get(str(mapping.get("price")), "").casefold()
    variant = ""
    if "корпус" in price_label:
        variant = "Корпус"
    elif "фасад" in price_label:
        variant = "Фасад"
    elif "модул" in price_label:
        variant = "Модуль"
    identity_context = header_context(actual_factory, sheet.name, headers, mapping)
    # Component-specific prices must use the matching component article.
    stand_kitchen = default.get("sku") == "E+H"
    if stand_kitchen and mapping.get("sku") in ("E+H", None):
        if mapping["price"] == "G":
            mapping["sku"] = "E"
            variant = "Корпус"
        elif mapping["price"] == "J":
            mapping["sku"] = "H"
            variant = "Фасад"
        elif mapping["price"] == "D":
            variant = "Модуль"

    # Collection titles often appear before the first table header.
    for number in sheet.row_numbers:
        if number >= header_row:
            break
        text_values = [normalize_text(v) for v in sheet.rows[number].values()
                       if isinstance(v, str) and normalize_text(v)]
        if len(text_values) == 1 and not _service_name(text_values[0]):
            collection = _collection_name(text_values[0])
    products, issues = [], []
    start_row = max(header_row + 1, int(configuration.get("start_row") or 1))
    last_named = None
    configured_parent = normalize_text(configuration.get("component_parent"))
    component_parent = configured_parent
    numbers = sheet.row_numbers
    for position, number in enumerate(numbers):
        if number < start_row:
            continue
        row = sheet.rows[number]
        name = normalize_text(sheet.get(number, mapping["name"]))
        original_name = normalize_text(row.get(_colnum(mapping["name"])))
        raw_price = sheet.get(number, mapping["price"])
        direct_price = row.get(_colnum(mapping["price"]))
        price = _price(raw_price)
        sku = normalize_text(sheet.get(number, mapping.get("sku")))
        if sku.casefold() in ("артикул", "модуль", "код"):
            sku = ""
        color = normalize_text(sheet.get(number, mapping.get("color")))
        named_collection = normalize_text(sheet.get(number, mapping.get("collection")))
        if not original_name and name and last_named and name == last_named["name"] and raw_price in (None, ""):
            continue
        # A group row has no price and only one relevant text, sometimes zero in
        # a formula-driven list. Keep category context without inventing products.
        candidates = [normalize_text(value) for value in row.values()
                      if isinstance(value, str) and normalize_text(value)]
        is_header = any(_name_header(value) for value in row.values()) and any(
            _price_score(normalize_text(value)) >= 0 for value in row.values())
        if is_header:
            # Some supplier headers also carry a collection label (АЛИСА).
            field_columns = set(mapping.values())
            for c, value in row.items():
                label = normalize_text(value)
                if _col(c) not in field_columns and isinstance(value, str) and not _service_name(label):
                    if not re.search(r"цвет|фото|характерист|вес|объем|объём|ширин|высот|глубин|размер|габарит|артикул|наименование|название", label, re.I):
                        collection = _collection_name(label)
            last_named = None
            component_parent = configured_parent
            continue
        heading = name or (candidates[0] if len(candidates) == 1 else "")
        category = re.sub(r"^\d+[.]\s*", "", heading).casefold().rstrip(".")
        raw_name = row.get(_colnum(mapping["name"]))
        other_named_column = not original_name and len(candidates) == 1
        horizontal_heading = any(
            r1 <= number <= r2 and c1 <= _colnum(mapping["name"]) <= c2 and c2 - c1 >= 2
            for r1, r2, c1, c2 in sheet.merges)
        next_sku = ""
        if mapping.get("sku") and position + 1 < len(numbers):
            next_sku = normalize_text(sheet.get(numbers[position + 1], mapping["sku"]))
        sku_title = bool(mapping.get("sku") and not sku and len(candidates) == 1 and
                         next_sku and next_sku.casefold() not in ("артикул", "модуль", "код"))
        collection_title = bool(re.search(r"коллекци|модульная (?:система|программа)|"
                                          r"гардеробная система", heading, re.I))
        upper_title = heading.isupper() and not re.search(r"\d", heading)
        numeric_children = False
        if price in (None,0) and heading and re.search(r"(?i)шкаф|кухн|гардероб|стеллаж|корпус|фасад|модул|комод|тумб",heading):
            following = numbers[position+1:position+3]
            numeric_children = any(re.fullmatch(r"\d+[.,]\d+",normalize_text(sheet.get(n,mapping["name"])))
                                   and _price(sheet.get(n,mapping["price"])) is not None for n in following)
        # MIF uses indentation to distinguish a zero-price group from its
        # actual priced children. Only descend when the original file proves it.
        hierarchical_group = False
        if price == 0 and isinstance(raw_name, str) and position + 1 < len(numbers):
            next_name = sheet.rows[numbers[position + 1]].get(_colnum(mapping["name"]))
            if isinstance(next_name, str):
                indent = len(raw_name) - len(raw_name.lstrip())
                next_indent = len(next_name) - len(next_name.lstrip())
                hierarchical_group = next_indent > indent
        if price in (None, 0) and heading and (
                category in CATEGORY_NAMES or collection_title or hierarchical_group or numeric_children or sku_title or
                len(candidates) == 1 and direct_price in (None, "") and (
                    other_named_column or horizontal_heading or upper_title or
                    not color and not sku and not re.search(
                        r"шкаф|тумба|комод|кровать|стол\b|стул|пенал|диван|полка|фасад|антресоль",
                        heading, re.I))):
            if not _service_name(heading):
                # Preserve a named furniture collection through generic category
                # subheadings such as "Спальни" in Untitled.
                if not (category in CATEGORY_NAMES and collection and
                        "untitled" in filename.casefold()):
                    collection = _collection_name(heading)
                if category not in CATEGORY_NAMES and re.search(r"(?i)шкаф|кухн|гардероб|стеллаж|корпус|фасад|модул|комод|тумб", heading):
                    component_parent = configured_parent or heading
                else:
                    component_parent = configured_parent
            last_named = None
            continue
        # A physical continuation of a merged item with dimensions has no own
        # name or price. It is not a second product with a missing price.
        if not original_name and name and last_named and name == last_named["name"] and raw_price in (None, ""):
            continue
        name_context = None
        if re.fullmatch(r"\d+[.,]\d+", name) and price is not None:
            if component_parent:
                original_label = name
                name = component_parent + " — модуль " + name.replace(".", ",")
                name_context = {"kind":"numeric-component","original_name":original_label,"parent_name":component_parent}
            else:
                issues.append({"name":name,"message":"В названии только размер или номер модуля. Укажите базовое название модулей для этого листа.",
                               "source_file":filename,"source_sheet":sheet.name,"source_row":number})
                continue
        if _service_name(name):
            # Explicit colour variants may continue a merged/non-merged name,
            # only when there is a real price and an identifying colour.
            if not name and last_named and color and price is not None:
                name = last_named["name"]
            else:
                continue
        if name in CATEGORY_NAMES and not sku:
            continue
        # A merged name may cover independently priced size variants. Only
        # suppress physical continuations that have no distinct dimensions/price.
        dimensions = _dimensions(sheet, number, mapping, name)
        if not original_name and name and last_named and name == last_named["name"]:
            own_dimensions = any(normalize_text(row.get(_colnum(mapping.get(key))))
                                 for key in ("dimensions", "width", "height", "depth"))
            size_changed = bool(own_dimensions and dimensions != last_named["dimensions"])
            price_changed = direct_price not in (None, "") and price != last_named["price"]
            if not size_changed and not price_changed and (not color or color == last_named["color"]):
                continue
        if not original_name and last_named:
            dimensions = dimensions or last_named["dimensions"]
        if named_collection and not _service_name(named_collection):
            product_collection = named_collection
        else:
            product_collection = collection
        if not product_collection:
            # Albina uses collection headings in A; generic row context handled
            # above, no forced filename/date-based collection.
            product_collection = ""
        product = {"name": name, "factory": actual_factory, "collection": product_collection,
                   "sku": sku if sku != "_" else "", "color": color,
                   "dimensions": dimensions, "base_price": price if price and price > 0 else None,
                   "variant": variant, "source_file": filename,
                   "source_sheet": sheet.name, "source_row": number}
        if price is None or price <= 0:
            issues.append({"message": "Нет корректной исходной цены: " + name,
                           "source_file": filename, "source_sheet": sheet.name, "source_row": number})
        if name_context:
            product["name_context"] = name_context
        product = normalize_product(product, identity_context)
        products.append(product)
        last_named = {"name": name, "row": number, "color": color,
                      "price": price, "dimensions": dimensions}
    return products, issues


def inspect_file(path: str, file_id: str) -> dict:
    from .stock_import import layout, factory_label
    sheets = _read(path)
    filename = Path(path).name
    factory = _factory(filename, sheets)
    warehouse_info = {sheet.name:layout(sheet) for sheet in sheets}
    warehouse_factories = {f for info in warehouse_info.values() if info for f in info["factories"]}
    if len(warehouse_factories)>1:
        factory = ""
    result = {"file_id":file_id,"name":filename,"factory":factory,"sheets":[],"warnings":[]}
    previous_factory = factory
    for sheet in sheets:
        info = warehouse_info[sheet.name]
        sheet_factory = info["factory"] if info else factory_label(sheet.name, known_only=True)
        if not sheet_factory and not info and previous_factory and re.match(r"^(?:лист\d*|дополн\w*|продолж\w*|фасады|корпус|кухни)(?:\b|\d)",sheet.name,re.I):
            sheet_factory = previous_factory
        if factory == "SV-Мебель" and "nn" in sheet.name.casefold():
            sheet_factory = "NN-Мебель"
        if sheet_factory:
            previous_factory = sheet_factory
        header_row,mapping,headers = _header(sheet)
        selected = not bool(NONPRODUCT_SHEETS.search(sheet.name)) and bool(mapping.get("name") and mapping.get("price"))
        products,issues = _parse_sheet(sheet,filename,factory,{"mapping":mapping,"factory":sheet_factory}) if selected else ([],[])
        sample=[]
        for n in ([p["source_row"] for p in products[:5]] if products else [n for n in sheet.row_numbers if n>header_row][:3]):
            sample.append({_col(col):normalize_text(value) for col,value in sheet.rows.get(n,{}).items()})
        item={"name":sheet.name,"selected":selected,"mapping":mapping,"headers":headers,"sample_rows":sample,
              "detected_factory":sheet_factory,
              "count":len(products),"factory":sheet_factory,"issues_count":len(issues)}
        if info:
            item["format"]=info.get("kind","warehouse")
            item["note"]=("Многострочный заголовок 1С: номенклатура, артикул и цена распознаны. Повторяющиеся служебные строки пропускаются; остаток не загружается."
                          if info.get("kind")=="price-export" else "Номенклатура и характеристики объединяются в товар. Цена — за единицу; остаток и сумма запасов не загружаются.")
            item["factory_groups"]=info["factories"]
        result["sheets"].append(item)
        available_prices=[h for h in headers if _price_score(h["label"])>=0]
        if selected and len(available_prices)>1:
            result["warnings"].append(f'Лист «{sheet.name}»: несколько цен. Проверьте выбранный столбец {mapping["price"]}.')
    if not factory and not all(s.get("factory") or s.get("factory_groups") for s in result["sheets"] if s["selected"]):
        result["warnings"].append("Укажите фабрику для файла либо отдельно для каждого выбранного листа.")
    if len(warehouse_factories)>1:
        result["warnings"].append("В книге несколько фабрик. Проверьте фабрику каждого листа; листы одной фабрики можно объединить одним названием.")
    result["detected_factory"]=factory
    result["sheet_count"]=len(sheets)
    return result


def parse_file(path: str, config: dict) -> dict:
    sheets = _read(path)
    filename = normalize_text(config.get("source_file")) or Path(path).name
    default_factory = _factory(filename, sheets)
    from .stock_import import layout
    factories = {f for sheet in sheets for info in [layout(sheet)] if info for f in info["factories"]}
    if len(factories)>1:
        default_factory = ""
    factory = normalize_text(config.get("factory")) or default_factory
    by_name = {item.get("name"): item for item in config.get("sheets", [])}
    explicit = "sheets" in config
    products, issues, warnings = [], [], []
    if not factory and not all(item.get("factory") or layout(sheet) for sheet in sheets
                               for item in [by_name.get(sheet.name,{})] if item.get("selected",True)):
        warnings.append("Фабрика не указана. Заполните её для файла или выбранных листов.")
    for sheet in sheets:
        configuration = by_name.get(sheet.name)
        if configuration is None:
            if explicit:
                continue
            configuration = {"selected": not bool(NONPRODUCT_SHEETS.search(sheet.name))}
        if not configuration.get("selected", True):
            continue
        sheet_products, sheet_issues = _parse_sheet(sheet, filename, factory, configuration)
        products.extend(sheet_products)
        issues.extend(sheet_issues)
    return {"products": products, "issues": issues, "warnings": warnings}
