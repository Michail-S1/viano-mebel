"""Read the supplied Avito XLSX export. Never fetch URLs or infer missing fields."""
import collections
import re
import zipfile
from xml.etree import ElementTree as ET

from backend.importer import _read, normalize_text


COLUMNS = {
    "avito_id": ("Номер объявления на Авито", "AvitoId"),
    "export_id": ("Уникальный идентификатор объявления", "Id"),
    "title": ("Название объявления", "Title"),
    "description": ("Описание объявления", "Description"),
    "price": ("Цена", "Price"), "category": ("Категория", "Category"),
    "status": ("AvitoStatus",), "image_urls": ("Ссылки на фото", "ImageUrls"),
    "factory": ("Производитель", "Manufacturer"), "collection": ("Коллекция",),
    "sku": ("Артикул", "SKU"), "color": ("Цвет от производителя",),
    "dimensions": ("Размеры",), "material": ("Материал",), "variant": ("Комплектация",),
}
# Public product fields only; contact details, account information and address
# are unnecessary for matching and are not copied into the listing registry.
DETAIL_FIELDS = {"Бренд", "Название модели", "Основной цвет", "Цвет от производителя", "Материал", "Материал фасада",
                 "Материал каркаса", "Материал обивки", "Материал основания", "Материал столешницы", "Вид товара",
                 "Подвид товара", "Тип товара", "Подтип товара", "Подтип тумбы", "Тип шкафов", "Тип дверей",
                 "Количество створок", "Ширина", "Высота", "Глубина", "Длина", "Ширина спального места",
                 "Длина спального места", "Матрас в комплекте", "Каркас", "За что цена", "Состав комплекта",
                 "Состояние", "Комплектация", "Доступность", "Тип кровати", "Тип кухни", "Форма кухни"}


def parse_export(path, money_cents):
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 30000 or sum(i.file_size for i in archive.infolist()) > 180*1024*1024:
            raise ValueError("Слишком большой распакованный XLSX")
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        root = ET.fromstring(archive.read("xl/workbook.xml"))
        hidden = {s.get("name") for s in root.findall(ns + "sheets/" + ns + "sheet") if s.get("state") in ("hidden", "veryHidden")}
    records, issues, sheets, duplicates = [], [], [], 0
    seen = {}
    for sheet in _read(str(path)):
        if sheet.name in hidden:
            continue
        header, columns, header_row = None, {}, None
        for number in sheet.row_numbers[:10]:
            labels = {normalize_text(v): k for k, v in sheet.rows[number].items() if v not in (None, "")}
            columns = {field: next((labels[name] for name in names if name in labels), None) for field, names in COLUMNS.items()}
            if columns["avito_id"] and columns["title"]:
                header, header_row = labels, number
                break
        if header is None:
            continue
        count = 0
        for number in sheet.row_numbers:
            if number <= header_row:
                continue
            row = sheet.rows[number]
            raw_id = row.get(columns["avito_id"])
            # Rows 3/4 in the real export describe required fields and accepted
            # values. They are instructions for the template, not advertisements.
            if normalize_text(raw_id) in ("Необязательно", "Обязательно", "Уникально в профиле", "Заполнить в профиле"):
                continue
            if number < header_row + 3 and not re.fullmatch(r"\d{1,32}", normalize_text(raw_id)):
                continue
            if not raw_id and not row.get(columns["title"]):
                continue
            location = {"sheet": sheet.name, "row": number}
            avito_id = normalize_text(raw_id)
            if not re.fullmatch(r"[0-9]{1,32}", avito_id):
                issues.append({**location, "message": "Нет однозначного номера Авито. ID строки не используется вместо него."})
                continue
            def value(field):
                return row.get(columns[field]) if columns.get(field) else None
            title = normalize_text(value("title"))
            if not title:
                issues.append({**location, "avito_id": avito_id, "message": "Нет названия объявления"})
                continue
            price = value("price")
            if isinstance(price, str):
                price = price.replace("\u00a0", "").replace(" ", "").replace(",", ".")
            try:
                amount = money_cents(price)
            except ValueError:
                amount = None
                issues.append({**location, "avito_id": avito_id, "message": "Некорректная цена; запись требует проверки"})
            details = {name: normalize_text(row.get(column)) for name, column in header.items()
                       if name in DETAIL_FIELDS and row.get(column) not in (None, "")}
            attrs = {key: normalize_text(value(key)) for key in ("factory", "collection", "sku", "color", "dimensions", "material", "variant") if value(key) not in (None, "")}
            photos = [u.strip() for u in normalize_text(value("image_urls")).split("|") if u.strip()]
            record = {"avito_id": avito_id, "title": title, "description": str(value("description") or "").strip(),
                      "category": normalize_text(value("category")), "status": normalize_text(value("status")),
                      "price": amount / 100 if amount is not None else None, "attributes": attrs,
                      "url": "",
                      "source_details": {**location, "export_id": normalize_text(value("export_id")),
                                         "image_urls": photos, "characteristics": details}}
            if avito_id in seen:
                a = {k:v for k,v in record.items() if k != "source_details"}
                b = {k:v for k,v in seen[avito_id].items() if k != "source_details"}
                a["details"] = {k:v for k,v in record["source_details"].items() if k not in ("sheet", "row")}
                b["details"] = {k:v for k,v in seen[avito_id]["source_details"].items() if k not in ("sheet", "row")}
                if a != b:
                    issues.append({**location, "avito_id": avito_id, "message": "Противоречивые строки с одним номером Авито"})
                    seen[avito_id]["duplicate_conflict"] = True
                else:
                    duplicates += 1
                continue
            seen[avito_id] = record
            records.append(record)
            count += 1
            if len(records) > 10000:
                raise ValueError("В одном импорте допускается до 10 000 объявлений")
        sheets.append({"name": sheet.name, "records": count})
    if not sheets:
        raise ValueError("Не найдены листы известного формата с номером Авито и названием")
    return {"records": records, "issues": issues, "sheets": sheets, "duplicates": duplicates,
            "categories": dict(collections.Counter(r["category"] for r in records))}
