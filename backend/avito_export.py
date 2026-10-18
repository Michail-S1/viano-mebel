"""Patch price cells in a complete Avito workbook, preserving every other ZIP member.

No workbook-library round trip: instructions, validations, photos, hidden sheets,
relationships and all unselected cells remain exactly as supplied. No network.
"""
import io
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from backend.avito_import import COLUMNS, parse_export

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
CELL = re.compile(rb'<(?:[\w.-]+:)?c(?=[\s>])[^>]*(?:/>|>.*?</(?:[\w.-]+:)?c\s*>)', re.S)
ROW = re.compile(rb'<(?:[\w.-]+:)?row(?=[\s>])[^>]*(?:/>|>.*?</(?:[\w.-]+:)?row\s*>)', re.S)
PILOT_MODE = 'selected_first_manual_pilot_v1'


def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Неподдерживаемые XML-сущности в файле')
    return ET.fromstring(data)


def parsed_bytes(data, money_cents):
    with tempfile.TemporaryDirectory(prefix='avito-xlsx-check-') as directory:
        path = Path(directory)/'source.xlsx'
        path.write_bytes(data)
        return parse_export(path, money_cents)


def workbook_index(members):
    rels = {r.get('Id'): r for r in xml(members['xl/_rels/workbook.xml.rels'])}
    sheets = {}
    for sheet in xml(members['xl/workbook.xml']).findall(NS+'sheets/'+NS+'sheet'):
        rel = rels[sheet.get(REL)]
        target = rel.get('Target', '')
        path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
        if rel.get('TargetMode') == 'External' or not path.startswith('xl/') or path not in members:
            raise ValueError('Неподдерживаемая ссылка на лист')
        sheets[sheet.get('name')] = path
    strings = []
    if 'xl/sharedStrings.xml' in members:
        strings = [''.join(t.text or '' for t in item.iter(NS+'t')) for item in xml(members['xl/sharedStrings.xml'])]
    return sheets, strings


def cell_text(cell, strings):
    if cell.get('t') == 'inlineStr':
        return ''.join(t.text or '' for t in cell.iter(NS+'t'))
    value = cell.findtext(NS+'v') or ''
    return strings[int(value)] if cell.get('t') == 's' else value


def patch_workbook(data, requests, money_cents, required_ids=(), strict_coverage=True):
    """requests: {AvitoId: {target_cents, export_id}}. Return bytes + audit report."""
    if not requests or len(requests) > 10:
        raise ValueError('Для пилота выберите от 1 до 10 объявлений одной фабрики')
    source = io.BytesIO(data)
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if (len(infos) > 30000 or sum(i.file_size for i in infos) > 180*1024*1024
                or len({i.filename for i in infos}) != len(infos) or any(i.flag_bits & 1 for i in infos)):
            raise ValueError('Неподдерживаемый или слишком большой XLSX')
        if any('vbaproject' in i.filename.lower() or i.filename.startswith('_xmlsignatures/') for i in infos):
            raise ValueError('Файлы с макросами или цифровой подписью не поддерживаются')
        members = {i.filename: archive.read(i) for i in infos}
        parsed = parsed_bytes(data, money_cents)
        if parsed['issues'] or parsed['duplicates']:
            raise ValueError('В исходной выгрузке есть ошибки или повторные AvitoId. Используйте полный файл без неоднозначных строк')
        records = {r['avito_id']: r for r in parsed['records']}
        if any(r['status'].strip().casefold() not in {'активно', 'active'} for r in records.values()):
            raise ValueError('Для пилота нужна выгрузка только опубликованных объявлений со статусом «Активно». Неизвестные и архивные статусы запрещены')
        missing = set(required_ids) - records.keys()
        if missing and strict_coverage:
            raise ValueError('В файле отсутствуют объявления локального реестра ('+str(len(missing))+'). Нужна полная свежая выгрузка, без удаления строк')
        keys = [r['source_details']['export_id'] for r in records.values()]
        if not keys or any(not k or k.lower() == 'stop' for k in keys) or len(set(keys)) != len(keys):
            raise ValueError('В файле должны быть непустые уникальные Id; команда stop запрещена')
        sheets, strings = workbook_index(members)

        def text(cell):
            return cell_text(cell, strings)

        changes, patches = [], {}
        for avito_id, request in requests.items():
            record = records.get(avito_id)
            if not record or record['source_details']['export_id'] != request['export_id']:
                raise ValueError('Id / AvitoId не совпадают с подтверждённой связью: '+avito_id)
            amount = request['target_cents']
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0 or money_cents(amount/100) != amount:
                raise ValueError('Некорректная целевая цена')
            before = money_cents(record['price'])
            if before is None:
                raise ValueError('Нет корректной исходной цены: '+avito_id)
            if before == amount:
                continue
            path = sheets[record['source_details']['sheet']]
            root = xml(members[path])
            price_column = None
            for row in root.findall(NS+'sheetData/'+NS+'row')[:10]:
                labels = {text(c).strip(): re.sub(r'\d+$', '', c.get('r', '')) for c in row.findall(NS+'c')}
                if any(n in labels for n in COLUMNS['avito_id']) and any(n in labels for n in COLUMNS['title']):
                    columns = [re.sub(r'\d+$', '', c.get('r', '')) for c in row.findall(NS+'c') if text(c).strip() in COLUMNS['price']]
                    if len(columns) != 1:
                        raise ValueError('Неоднозначный столбец цены')
                    price_column = columns[0]
                    break
            if not price_column:
                raise ValueError('Не найден столбец Price / Цена')
            address = price_column+str(record['source_details']['row'])
            target_cells = [c for c in root.iter(NS+'c') if c.get('r') == address]
            if len(target_cells) != 1 or target_cells[0].find(NS+'f') is not None:
                raise ValueError('Повторная ячейка цены или формула вместо цены')
            patches.setdefault(path, {})[address] = amount
            changes.append({'avito_id': avito_id, 'export_id': request['export_id'], 'title': record['title'],
                            'previous_cents': before, 'target_cents': amount, 'sheet': record['source_details']['sheet'], 'cell': address})
        if not changes:
            raise ValueError('В выбранных объявлениях цены уже совпадают; файл не требуется')
        updated = dict(members)
        for path, cells in patches.items():
            found = set()

            def replace(match):
                raw = match.group()
                address_match = re.search(rb'\br=["\']([^"\']+)["\']', raw.split(b'>', 1)[0])
                address = address_match.group(1).decode() if address_match else ''
                if address not in cells:
                    return raw
                if address in found:
                    raise ValueError('Повторная ячейка цены или формула вместо цены')
                found.add(address)
                opening = raw[:raw.index(b'>')+1]
                opening = re.sub(rb'\s+t=["\'][^"\']*["\']', b'', opening).replace(b'/>', b'>')
                prefix = re.match(rb'<([\w.-]+:)?c', raw).group(1) or b''
                amount = cells[address]
                value = (str(amount//100) if amount % 100 == 0 else f'{amount//100}.{amount%100:02d}').encode()
                return opening+b'<'+prefix+b'v>'+value+b'</'+prefix+b'v></'+prefix+b'c>'

            updated[path] = CELL.sub(replace, members[path])
            if found != cells.keys():
                raise ValueError('Не найдены все ячейки цен')
            # Only the selected cell spans may differ, even at the byte level.
            def masked(payload):
                def mask(m):
                    r = re.search(rb'\br=["\']([^"\']+)["\']', m.group().split(b'>', 1)[0])
                    return b'<selected-price/>' if r and r.group(1).decode() in cells else m.group()
                return CELL.sub(mask, payload)
            if masked(updated[path]) != masked(members[path]):
                raise ValueError('Изменены посторонние данные листа')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as result:
            result.comment = archive.comment
            for info in infos:
                result.writestr(info, updated[info.filename])
    result_bytes = output.getvalue()
    after = parsed_bytes(result_bytes, money_cents)
    expected = {r['avito_id']: r for r in parsed['records']}
    for change in changes:
        expected[change['avito_id']] = {**expected[change['avito_id']], 'price': change['target_cents']/100}
    if after['issues'] or {r['avito_id']: r for r in after['records']} != expected:
        raise ValueError('Проверка сохранности объявлений не пройдена')
    return result_bytes, {'changes': changes, 'total_listings': len(records), 'unchanged_listings': len(records)-len(changes),
                          'preserved_members': len(members)-len(patches), 'changed_sheets': len(patches),
                          'missing_registry_ids': sorted(missing)}


def build_pilot_workbook(data, requests, money_cents):
    """First manual pilot only: include changed selected rows, never the full feed.

    Support confirmed this first-upload scenario for the user's account. This
    is not a permanent feed or permission to remove previously managed ads.
    """
    if any(not isinstance(r.get('target_cents'), int) or r['target_cents'] % 100 for r in requests.values()):
        raise ValueError('Шаблон Авито требует целые рубли. Исправьте настройки цены в каталоге; экспорт сам не округляет')
    patched, report = patch_workbook(data, requests, money_cents, strict_coverage=False)
    source = parsed_bytes(data, money_cents)
    by_id = {r['avito_id']: r for r in source['records']}
    included = {c['avito_id'] for c in report['changes']}
    with zipfile.ZipFile(io.BytesIO(patched)) as archive:
        infos = archive.infolist()
        members = {i.filename: archive.read(i) for i in infos}
        sheets, strings = workbook_index(members)
        keep = {}
        for identifier in included:
            location = by_id[identifier]['source_details']
            keep.setdefault(sheets[location['sheet']], set()).add(location['row'])
        modified = dict(members)
        checked_paths = set()
        for name, path in sheets.items():
            root = xml(members[path])
            rows = root.findall(NS+'sheetData/'+NS+'row')
            # Dictionary and instruction sheets have no advertisement headers.
            header = next((r for r in rows[:10] if
                any(cell_text(c, strings).strip() in COLUMNS['avito_id'] for c in r.findall(NS+'c')) and
                any(cell_text(c, strings).strip() in COLUMNS['title'] for c in r.findall(NS+'c'))), None)
            if header is None:
                continue
            if header.get('r') != '2':
                raise ValueError('Для частичного пилота поддержан шаблон с заголовком во второй строке')
            checked_paths.add(path)
            allowed = keep.get(path, set())
            found = set()

            def select_row(match):
                raw = match.group()
                ref = re.search(rb'\br=["\']([0-9]+)["\']', raw.split(b'>', 1)[0])
                if not ref:
                    raise ValueError('Неоднозначный номер строки XLSX')
                number = int(ref.group(1))
                if number <= 4:
                    return raw
                if number in allowed:
                    if number in found:
                        raise ValueError('Повторная строка выбранного объявления')
                    found.add(number)
                    return raw
                return b''

            modified[path] = ROW.sub(select_row, members[path])
            if found != allowed:
                raise ValueError('Не найдены строки выбранных объявлений')
            # There must be no unselected row in any advertisement sheet,
            # including hidden category sheets that the normal importer skips.
            remaining = {int(r.get('r')) for r in xml(modified[path]).findall(NS+'sheetData/'+NS+'row') if int(r.get('r')) > 4}
            if remaining != allowed:
                raise ValueError('В пилотном файле остались посторонние строки')
        if not keep.keys() <= checked_paths:
            raise ValueError('Не все выбранные листы имеют проверенный формат')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as result:
            result.comment = archive.comment
            for info in infos:
                result.writestr(info, modified[info.filename])
    result_bytes = output.getvalue()
    after = parsed_bytes(result_bytes, money_cents)
    expected = {key: {**by_id[key], 'price': requests[key]['target_cents']/100} for key in included}
    actual = {r['avito_id']: r for r in after['records']}
    if after['issues'] or after['duplicates'] or actual != expected:
        raise ValueError('Не пройдена проверка точного состава пилотного файла')
    with zipfile.ZipFile(io.BytesIO(data)) as original:
        changed_parts = [name for name in modified if original.read(name) != modified[name]]
    report.update({'mode': PILOT_MODE, 'source_total_listings': len(by_id),
        'output_total_listings': len(actual), 'output_avito_ids': sorted(actual),
        'preserved_members': len(modified)-len(changed_parts), 'changed_sheets': len(changed_parts),
        'excluded_from_pilot_count': len(by_id)-len(actual),
        'skipped_unchanged_ids': sorted(requests.keys()-included)})
    return result_bytes, report
