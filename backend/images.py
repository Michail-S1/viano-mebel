"""Extract locally embedded Excel pictures and match them to product row groups."""
from __future__ import annotations
import hashlib
import io
import posixpath
import struct
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
D = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MAX_IMAGE = 20 * 1024 * 1024
MAX_TOTAL = 160 * 1024 * 1024


@dataclass
class Picture:
    sheet: str
    row: int
    last_row: int
    col: int
    last_col: int
    name: str
    data: bytes


def _part(owner, target):
    path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
        posixpath.join(posixpath.dirname(owner), target))
    if not path.startswith("xl/") or "\\" in path or ":" in path:
        raise ValueError("Недопустимая ссылка изображения.")
    return path


def _xml(archive, path):
    entry = archive.getinfo(path)
    if entry.file_size > 12 * 1024 * 1024:
        raise ValueError("Слишком большой XML рисунков.")
    return ET.fromstring(archive.read(path))


def _relationships(archive, owner):
    path = posixpath.join(posixpath.dirname(owner), "_rels", posixpath.basename(owner) + ".rels")
    if path not in archive.namelist():
        return {}
    return {item.attrib["Id"]: _part(owner, item.attrib["Target"])
            for item in _xml(archive, path)
            if item.attrib.get("TargetMode") != "External" and item.attrib.get("Target")
            and item.attrib.get("Type", "").rsplit("/", 1)[-1] in ("worksheet", "drawing", "image")}


def _xlsx_pictures(path):
    pictures, warnings, total = [], [], 0
    media_cache = {}
    with zipfile.ZipFile(path) as archive:
        workbook_path = "xl/workbook.xml"
        rels = _relationships(archive, workbook_path)
        for sheet in _xml(archive, workbook_path).findall(S + "sheets/" + S + "sheet"):
            sheet_path = rels.get(sheet.get(R + "id"))
            if not sheet_path:
                continue
            content = _xml(archive, sheet_path)
            sheet_rels = _relationships(archive, sheet_path)
            heights = {int(row.get("r")): float(row.get("ht")) for row in content.findall(
                S + "sheetData/" + S + "row") if row.get("ht")}
            default = content.find(S + "sheetFormatPr")
            default_height = float(default.get("defaultRowHeight", 15)) if default is not None else 15
            for drawing in content.findall(S + "drawing"):
                drawing_path = sheet_rels.get(drawing.get(R + "id"))
                if not drawing_path:
                    continue
                media = _relationships(archive, drawing_path)
                for anchor in _xml(archive, drawing_path):
                    start = anchor.find(D + "from")
                    if start is None:
                        warnings.append("Изображение без привязки к ячейке пропущено.")
                        continue
                    row = int(start.findtext(D + "row", "0")) + 1
                    col = int(start.findtext(D + "col", "0")) + 1
                    end = anchor.find(D + "to")
                    last_row, last_col = row, col
                    if end is not None:
                        last_row = int(end.findtext(D + "row", "0")) + (
                            1 if int(end.findtext(D + "rowOff", "0")) else 0)
                        last_col = int(end.findtext(D + "col", "0")) + (
                            1 if int(end.findtext(D + "colOff", "0")) else 0)
                    else:
                        extent = anchor.find(D + "ext")
                        remaining = int(extent.get("cy", 0)) if extent is not None else 0
                        remaining += int(start.findtext(D + "rowOff", "0"))
                        current = row
                        while remaining > 0 and current - row < 10000:
                            remaining -= max(1, heights.get(current, default_height) * 12700)
                            last_row = current
                            current += 1
                    for pic in anchor.findall(".//" + D + "pic"):
                        blip = pic.find(".//" + A + "blip")
                        filename = media.get(blip.get(R + "embed")) if blip is not None else None
                        if not filename:
                            warnings.append("Связанное внешнее фото пропущено; нужен встроенный файл изображения.")
                            continue
                        if filename not in media_cache:
                            entry = archive.getinfo(filename)
                            if entry.file_size > MAX_IMAGE or total + entry.file_size > MAX_TOTAL:
                                warnings.append("Фото Excel пропущено: превышен допустимый размер.")
                                continue
                            media_cache[filename] = archive.read(filename)
                            total += len(media_cache[filename])
                        data = media_cache[filename]
                        pictures.append(Picture(sheet.get("name", ""), row, max(row, last_row),
                            col, max(col, last_col), posixpath.basename(filename), data))
        if any(name.startswith("xl/richData/") for name in archive.namelist()) and not pictures:
            warnings.append("Обнаружены изображения внутри ячеек нового формата Excel. Переведите их в рисунки над ячейками для автоматического импорта.")
    return pictures, warnings


def _biff_records(data):
    pos = 0
    while pos + 4 <= len(data):
        code, size = struct.unpack_from("<HH", data, pos)
        if pos + 4 + size > len(data):
            raise ValueError("Повреждённая запись XLS.")
        yield pos, code, data[pos + 4:pos + 4 + size]
        pos += size + 4


def _drawing_bytes(records, code):
    parts, collecting = [], False
    for _offset, record, payload in records:
        if record == code:
            parts.append(payload)
            collecting = True
        elif record == 0x003C and collecting:
            parts.append(payload)
        else:
            collecting = False
    return b"".join(parts)


def _art_records(data, depth=0):
    if depth > 32:
        raise ValueError("Слишком глубокая структура рисунка XLS.")
    pos = 0
    while pos + 8 <= len(data):
        options, kind, size = struct.unpack_from("<HHI", data, pos)
        if pos + 8 + size > len(data) and options & 15 != 15:
            raise ValueError("Повреждённый рисунок XLS.")
        # Some supplier generators retain the original container byte count
        # after deleting shapes. Read only the available, individually bounded children.
        payload = data[pos + 8:min(len(data), pos + 8 + size)]
        yield kind, options >> 4, payload
        if options & 15 == 15:
            yield from _art_records(payload, depth + 1)
        pos += 8 + size


def _blip(kind, instance, payload):
    data = payload[33 if instance & 1 else 17:]
    if kind == 0xF01F and len(data) >= 40:  # Device-independent bitmap -> BMP.
        header, _width, _height, _planes, bits = struct.unpack_from("<IiiHH", data)
        palette = struct.unpack_from("<I", data, 32)[0] or (1 << bits if bits <= 8 else 0)
        offset = 14 + header + palette * 4
        if struct.unpack_from("<I", data, 16)[0] == 3 and header == 40:
            offset += 12
        return struct.pack("<2sIHHI", b"BM", len(data) + 14, 0, 0, offset) + data
    if kind in (0xF01D, 0xF01E, 0xF029, 0xF02A):
        return data
    return None


def _xls_stream_pictures(stream):
    records = list(_biff_records(stream))
    sheets = []
    version = next((struct.unpack_from("<H", data)[0] for _, code, data in records
                    if code == 0x0809 and len(data) >= 4), 0x0600)
    codepage = next((struct.unpack_from("<H", data)[0] for _, code, data in records
                     if code == 0x0042 and len(data) >= 2), 1251)
    for _, code, data in records:
        if code == 0x0085 and len(data) >= 8:
            offset = struct.unpack_from("<I", data)[0]
            length = data[6]
            unicode = data[7] & 1 if version >= 0x0600 else False
            start = 8 if version >= 0x0600 else 7
            name = data[start:start + length * (2 if unicode else 1)].decode(
                "utf-16-le" if unicode else ("latin1" if version >= 0x0600 else "cp" + str(codepage)))
            sheets.append((offset, name))
    store = []
    warnings = []
    drawing_group = _drawing_bytes(records, 0x00EB)
    for kind, _instance, payload in _art_records(drawing_group):
        if kind != 0xF007:
            continue
        image = None
        if len(payload) >= 36:
            start = 36 + payload[33]
            embedded = payload[start:]
            if len(embedded) >= 8:
                options, blip_type, size = struct.unpack_from("<HHI", embedded)
                if size <= len(embedded) - 8:
                    image = _blip(blip_type, options >> 4, embedded[8:8 + size])
        store.append(image)
    pictures, total = [], 0
    for index, (offset, name) in enumerate(sheets):
        following = min((position for position, _ in sheets if position > offset), default=len(stream))
        shape_data = _drawing_bytes([record for record in records if offset <= record[0] < following], 0x00EC)
        for kind, _, shape in _art_records(shape_data):
            if kind != 0xF004:
                continue
            image_index, anchor = None, None
            for child, count, payload in _art_records(shape):
                if child in (0xF00B, 0xF121, 0xF122):
                    for position in range(min(count, len(payload) // 6)):
                        property_id, value = struct.unpack_from("<HI", payload, position * 6)
                        if property_id & 0x3FFF in (0x0104, 0x0186):
                            image_index = value
                elif child == 0xF010 and len(payload) >= 18:
                    anchor = struct.unpack_from("<9H", payload)
            if image_index is None or anchor is None:
                continue
            image = store[image_index - 1] if 0 < image_index <= len(store) else None
            if not image:
                warnings.append("Фото XLS пропущено: неподдерживаемый или внешний формат изображения.")
                continue
            if len(image) > MAX_IMAGE or total + len(image) > MAX_TOTAL:
                warnings.append("Фото XLS пропущено: превышен допустимый размер.")
                continue
            total += len(image)
            pictures.append(Picture(name, anchor[3] + 1,
                max(anchor[3] + 1, anchor[7] + bool(anchor[8])),
                anchor[1] + 1, max(anchor[1] + 1, anchor[5] + bool(anchor[6])),
                "Фото из Excel", image))
    return pictures, warnings


def read_pictures(path):
    try:
        if Path(path).suffix.lower() == ".xlsx":
            return _xlsx_pictures(path)
        from xlrd.compdoc import CompDoc, SIGNATURE
        raw = Path(path).read_bytes()
        if raw.startswith(SIGNATURE):
            document = CompDoc(raw, logfile=io.StringIO())
            raw = document.get_named_stream("Workbook") or document.get_named_stream("Book") or b""
        return _xls_stream_pictures(raw)
    except Exception as error:
        return [], ["Не удалось извлечь фотографии из Excel: " + str(error) + ". Товары можно загрузить без фото."]


def product_pictures(path, config, products):
    from .importer import _read, _header, _colnum, normalize_text, _header_identity
    pictures, warnings = read_pictures(path)
    if not pictures:
        return {}, warnings
    rows = defaultdict(list)
    for product in products:
        rows[product["source_sheet"]].append(product)
    configurations = {item["name"]: item for item in config.get("sheets", [])}
    matches = defaultdict(list)
    for sheet in _read(path):
        if sheet.name not in rows:
            continue
        header_row, default, _ = _header(sheet)
        mapping = {**default, **(configurations.get(sheet.name, {}).get("mapping") or {})}
        name_col = _colnum(mapping.get("name"))
        photo_columns = {col for col, value in sheet.rows.get(header_row, {}).items()
                         if any(term in _header_identity(value) for term in ("фото", "изображен", "картин"))
                         or _header_identity(value) in ("видкровати", "внешнийвид", "внешнийвидизделия")}
        for picture in (item for item in pictures if item.sheet == sheet.name):
            if picture.row <= header_row:
                continue  # Title banners, factory logos and cover graphics.
            if photo_columns:
                if not any(picture.col <= col <= picture.last_col for col in photo_columns):
                    continue  # e.g. NEW badges in the product-name column.
            elif not name_col or picture.col >= name_col:
                continue  # An unlabelled photo column is accepted only to the left of names.
            midpoint = (picture.row + picture.last_row) / 2
            group = [(first, last) for first, last, left, right in sheet.merges
                     if first <= midpoint <= last and last > first and
                     (any(left <= col <= right for col in photo_columns) if photo_columns
                      else left <= picture.col <= right)]
            if not group:
                group = [(first, last) for first, last, left, right in sheet.merges
                         if first <= midpoint <= last and last > first and left <= name_col <= right]
            first, last = max(group, key=lambda pair: pair[1] - pair[0]) if group else (
                picture.row, picture.last_row)
            targets = [product for product in rows[sheet.name] if first <= product["source_row"] <= last]
            identities = {(normalize_text(item["name"]).casefold(), normalize_text(item.get("sku")).casefold())
                          for item in targets}
            if len(identities) > 1:
                warnings.append(f'Лист «{sheet.name}»: фото около строки {picture.row} охватывает разные товары; добавьте его вручную.')
                continue
            digest = hashlib.sha256(picture.data).hexdigest()
            for target in targets:
                key = (sheet.name, target["source_row"])
                if not any(item["digest"] == digest for item in matches[key]):
                    matches[key].append({"digest": digest, "name": picture.name, "data": picture.data})
    return dict(matches), list(dict.fromkeys(warnings))
