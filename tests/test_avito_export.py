"""Price-only XLSX export: temporary data, preserved package, no network."""
import base64
import io
import json
import hashlib
import re
import unittest
import zipfile

from backend.avito import money_cents
from backend.avito_export import patch_workbook, build_pilot_workbook, parsed_bytes, PILOT_MODE, NS, ROW
from server import ApiError
import test_avito as avito_fixtures


class PriceExport(unittest.TestCase):
    def setUp(self):
        self.fixture = avito_fixtures.LocalAvito('runTest')
        self.fixture.setUp()
        self.avito = self.fixture.avito
        self.avito.setup()
        self.source = self.fixture.export(rows=[
            ['external-key', '123456789', 'Тумба 1000×1000', '<p>Фабрика не указана</p>',
             'Мебель и интерьер', 'Да', 15000, 'http://avito.ru/image-1 | http://avito.ru/image-2', 100, 100, 50, 'Активно', 'PRIVATE-PHONE'],
            ['external-job', '987654321', 'Вакансия', 'Описание', 'Вакансии', '', 10000, '', '', '', '', 'Активно']])
        preview = self.avito.inspect_export(self.source, 'fresh.xlsx', 'update')
        self.avito.commit_export({'session_id': preview['session_id'], 'categories': ['Мебель и интерьер']})
        with self.fixture.catalog.connection() as db:
            self.listing = self.avito._listing(db, db.execute('SELECT id FROM avito_listings').fetchone()[0])
        self.fixture.confirm(self.listing, self.fixture.region)
        self.plan = self.avito.prepare({'factory': 'Регион 058'})
        self.payload = {'plan_id': self.plan['id'], 'listing_ids': [self.listing['id']], 'mode': PILOT_MODE}

    def tearDown(self):
        self.fixture.tearDown()

    def preview(self, **changes):
        return self.avito.preview_price_export(self.source, 'fresh.xlsx', {**self.payload, **changes})

    def download(self, preview):
        return self.avito.download_price_export({'session_id': preview['session_id'], 'reviewed_prices': True, 'first_manual_pilot': True})

    def replace_member(self, source, transform):
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as before, zipfile.ZipFile(out, 'w') as after:
            for i in before.infolist():
                after.writestr(i, transform(i.filename, before.read(i.filename)))
        return out.getvalue()

    def test_complete_package_only_selected_price_changes(self):
        preview = self.preview()
        self.assertEqual(len(preview['changes']), 1)
        self.assertEqual(preview['total_listings'], 2)  # vacancy remains in full file
        self.assertEqual(preview['unchanged_listings'], 1)
        self.assertEqual(preview['changes'][0]['target_cents'], money_cents(self.fixture.region['sale_price']))
        output = (self.avito.price_export_sessions[preview['session_id']]['directory']/'prices.xlsx').read_bytes()
        self.assertTrue(preview['upload_allowed'])
        self.assertFalse(preview['automatic_upload_allowed'])
        self.assertEqual(preview['output_total_listings'], 1)
        self.assertEqual(preview['output_avito_ids'], ['123456789'])
        self.assertEqual(len(parsed_bytes(output, money_cents)['records']), 1)
        self.assertEqual(preview['selected_found_count'], 1)
        self.assertEqual(preview['selected_items'][0]['avito_id'], self.listing['avito_id'])
        with zipfile.ZipFile(io.BytesIO(self.source)) as a, zipfile.ZipFile(io.BytesIO(output)) as b:
            self.assertEqual(a.namelist(), b.namelist())
            different = [n for n in a.namelist() if a.read(n) != b.read(n)]
            self.assertEqual(different, ['xl/worksheets/sheet1.xml'])
            self.assertIn(b'PRIVATE-PHONE', b.read(different[0]))
        with self.fixture.catalog.connection() as db:
            self.assertEqual(db.execute('SELECT price_cents FROM avito_listings').fetchone()[0], 1500000)
            kinds = [r[0] for r in db.execute('SELECT kind FROM avito_events')]
        self.assertIn('price_file_prepared', kinds)
        self.assertNotIn('pilot_file_downloaded', kinds)

    def test_explicit_checks_required_and_file_is_not_sent(self):
        preview = self.preview()
        with self.assertRaises(ApiError):
            self.avito.download_price_export({'session_id': preview['session_id']})
        self.assertFalse(self.avito.state()['transport_enabled'])

    def test_stale_plan_and_changed_price_block_download(self):
        preview = self.preview()
        self.fixture.catalog.settings({'markup_percent': 30, 'fixed_add': 1000, 'rounding': 'ruble'})
        with self.assertRaises(ApiError):
            self.preview()
        with self.assertRaises(ApiError):
            self.download(preview)

    def test_unlink_after_preview_blocks_download(self):
        preview = self.preview()
        self.avito.decide(self.fixture.decision_payload(self.listing, self.fixture.region, 'unlink'))
        with self.assertRaises(ApiError):
            self.preview()
        with self.assertRaises(ApiError):
            self.download(preview)

    def test_source_id_and_other_fields_cannot_change(self):
        for old, new in ((b'external-key', b'wrong-key'), ('Фабрика не указана'.encode(), 'Другое описание'.encode())):
            data = self.replace_member(self.source, lambda n, b: b.replace(old, new) if n.endswith('sheet1.xml') else b)
            with self.assertRaises(ApiError):
                self.avito.preview_price_export(data, 'fresh.xlsx', self.payload)

    def test_duplicate_ids_missing_rows_and_limit_fail_closed(self):
        request = {'123456789': {'target_cents': 1600000, 'export_id': 'external-key'}}
        with self.assertRaises(ValueError):
            patch_workbook(self.source, request, money_cents, ['000000'])
        duplicate = self.replace_member(self.source, lambda n, b: b.replace(b'987654321', b'123456789') if n.endswith('sheet1.xml') else b)
        with self.assertRaises(ValueError):
            patch_workbook(duplicate, request, money_cents)
        with self.assertRaises(ApiError):
            self.preview(listing_ids=[str(i) for i in range(11)])
        with self.assertRaises(ApiError):
            self.preview(listing_ids=[self.listing['id']]*2)
        with self.assertRaises(ApiError):
            self.preview(listing_ids=['0'*32])

    def test_unchanged_price_does_not_create_file(self):
        with self.assertRaisesRegex(ValueError, 'совпадают'):
            patch_workbook(self.source, {'123456789': {'target_cents': 1500000, 'export_id': 'external-key'}}, money_cents)

    def test_formula_price_is_rejected(self):
        data = self.replace_member(self.source, lambda n, b: b.replace(b'<v>15000</v>', b'<f>10000+5000</f><v>15000</v>') if n.endswith('sheet1.xml') else b)
        with self.assertRaises(ValueError):
            patch_workbook(data, {'123456789': {'target_cents': 1600000, 'export_id': 'external-key'}}, money_cents)

    def test_cancelled_and_expired_preview(self):
        preview = self.preview()
        self.avito.price_export_sessions[preview['session_id']]['created'] = 0
        with self.assertRaises(ApiError):
            self.download(preview)
        self.avito.cancel(self.plan['id'])
        with self.assertRaises(ApiError):
            self.preview()

    def test_archived_row_even_unselected_blocks_export(self):
        data = self.replace_member(self.source, lambda n, b: b.replace('Активно'.encode(), 'В архиве'.encode()) if n.endswith('sheet1.xml') else b)
        with self.assertRaisesRegex(ValueError, 'статус'):
            patch_workbook(data, {'123456789': {'target_cents': 1600000, 'export_id': 'external-key'}}, money_cents)

    def test_same_workbook_preserves_extra_parts_and_decimal_price(self):
        data = self.replace_member(self.source, lambda n, b: b.replace(b'<c r="G5"', b'<c s="0" r="G5"') if n.endswith('sheet1.xml') else b)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as a, zipfile.ZipFile(out, 'w') as b:
            for i in a.infolist():
                b.writestr(i, a.read(i.filename))
            b.writestr('customXml/keep.xml', b'<untouched/>')
        result, report = patch_workbook(out.getvalue(), {'123456789': {'target_cents': 1600001, 'export_id': 'external-key'}}, money_cents)
        with zipfile.ZipFile(io.BytesIO(result)) as z:
            self.assertEqual(z.read('customXml/keep.xml'), b'<untouched/>')
            self.assertIn(b'<v>16000.01</v>', z.read('xl/worksheets/sheet1.xml'))

    def test_modified_output_is_not_downloaded(self):
        preview = self.preview()
        path = self.avito.price_export_sessions[preview['session_id']]['directory']/'prices.xlsx'
        path.write_bytes(b'not the prepared workbook')
        with self.assertRaises(ApiError):
            self.download(preview)

    def test_missing_historical_listing_is_not_a_missing_selected_item(self):
        self.fixture.listing(avito_id='5555555', title='Старое объявление')
        self.plan = self.avito.prepare({'factory': 'Регион 058'})
        self.payload['plan_id'] = self.plan['id']
        preview = self.preview()
        self.assertEqual(preview['missing_registry_ids'], ['5555555'])
        self.assertEqual(preview['selected_found_count'], 1)
        self.assertNotIn(self.listing['avito_id'], preview['missing_registry_ids'])
        # The unrelated historical record does not enter this selected pilot.
        self.assertEqual(self.download(preview)['report']['output_avito_ids'], ['123456789'])
        self.avito.price_export_sessions[preview['session_id']]['report']['mode'] = 'old_full_file'
        with self.assertRaises(ApiError) as denied:
            self.download(preview)
        self.assertEqual(denied.exception.status, 403)

    def test_only_the_imported_source_workbook_can_be_used(self):
        data = self.replace_member(self.source, lambda n, b: b.replace(b'PRIVATE-PHONE', b'DIFFERENT-PHONE') if n.endswith('sheet1.xml') else b)
        with self.assertRaisesRegex(ApiError, 'не та выгрузка'):
            self.avito.preview_price_export(data, 'changed.xlsx', self.payload)

    def test_shared_string_price_and_prefixed_cells(self):
        source = self.replace_member(self.source, lambda n, b: b.replace(b'<c r="G5"><v>15000</v></c>', b'<c r="G5" t="s"><v>0</v></c>').replace(b'<c ', b'<x:c ').replace(b'</c>', b'</x:c>').replace(b'<worksheet ', b'<worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ') if n.endswith('sheet1.xml') else b)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as a, zipfile.ZipFile(out, 'w') as b:
            for i in a.infolist():
                b.writestr(i, a.read(i.filename))
            b.writestr('xl/sharedStrings.xml', b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>15000</t></si></sst>')
        result, report = patch_workbook(out.getvalue(), {'123456789': {'target_cents': 1600000, 'export_id': 'external-key'}}, money_cents)
        self.assertEqual(report['changes'][0]['previous_cents'], 1500000)
        with zipfile.ZipFile(io.BytesIO(result)) as z:
            self.assertIn(b'<x:v>16000</x:v>', z.read('xl/worksheets/sheet1.xml'))

    def test_partial_pilot_preserves_headers_and_selected_row_except_price(self):
        request = {'123456789': {'target_cents': 1600000, 'export_id': 'external-key'}}
        result, report = build_pilot_workbook(self.source, request, money_cents)
        self.assertEqual(report['output_avito_ids'], ['123456789'])
        self.assertEqual(report['excluded_from_pilot_count'], 1)
        with zipfile.ZipFile(io.BytesIO(self.source)) as a, zipfile.ZipFile(io.BytesIO(result)) as b:
            before = ROW.findall(a.read('xl/worksheets/sheet1.xml'))
            after = ROW.findall(b.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(before[:4], after[:4])
            self.assertEqual(len(after), 5)
            self.assertEqual(before[4].replace(b'<v>15000</v>', b'<v>16000</v>'), after[4])
            for name in a.namelist():
                if name != 'xl/worksheets/sheet1.xml':
                    self.assertEqual(a.read(name), b.read(name))

    def test_pilot_skips_unchanged_and_rejects_fractional_rubles(self):
        result, report = build_pilot_workbook(self.source, {
            '123456789': {'target_cents': 1600000, 'export_id': 'external-key'},
            '987654321': {'target_cents': 1000000, 'export_id': 'external-job'}}, money_cents)
        self.assertEqual(report['skipped_unchanged_ids'], ['987654321'])
        self.assertEqual([r['avito_id'] for r in parsed_bytes(result, money_cents)['records']], ['123456789'])
        with self.assertRaisesRegex(ValueError, 'целые рубли'):
            build_pilot_workbook(self.source, {'123456789': {'target_cents': 1600001, 'export_id': 'external-key'}}, money_cents)

    def test_full_file_cannot_be_replayed_as_selected_pilot(self):
        preview = self.preview()
        session = self.avito.price_export_sessions[preview['session_id']]
        (session['directory']/'prices.xlsx').write_bytes(self.source)
        session['report']['output_sha256'] = hashlib.sha256(self.source).hexdigest()
        with self.assertRaisesRegex(ApiError, 'Состав файла'):
            self.download(preview)
        with self.assertRaises(ApiError):
            self.preview(mode='full')

    def test_unselected_hidden_advertisement_sheet_is_cleared(self):
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.source)) as a, zipfile.ZipFile(out, 'w') as b:
            hidden_sheet = a.read('xl/worksheets/sheet1.xml')
            for info in a.infolist():
                data = a.read(info.filename)
                if info.filename == 'xl/workbook.xml':
                    data = data.replace(b'</sheets>', b'<sheet name="Hidden ads" sheetId="2" state="hidden" r:id="rId2"/></sheets>')
                if info.filename == 'xl/_rels/workbook.xml.rels':
                    data = data.replace(b'</Relationships>', b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')
                b.writestr(info, data)
            b.writestr('xl/worksheets/sheet2.xml', hidden_sheet)
        result, report = build_pilot_workbook(out.getvalue(), {'123456789': {'target_cents': 1600000, 'export_id': 'external-key'}}, money_cents)
        with zipfile.ZipFile(io.BytesIO(result)) as z:
            self.assertEqual(len(ROW.findall(z.read('xl/worksheets/sheet2.xml'))), 4)
            self.assertEqual(len(ROW.findall(z.read('xl/worksheets/sheet1.xml'))), 5)

    def test_http_export_download_and_send_remains_blocked(self):
        import test_server as fixtures
        client = fixtures.CatalogAPI('runTest')
        client.setUp()
        try:
            client.request('POST', '/api/avito/setup', {})
            client.commit([['ТБ-02', 'Тумба 1000×1000', 'Лира', 'Белый', '1000×1000', 10005]], factory='Регион 058')
            imported = client.request('POST', '/api/avito/import/inspect', {'name': 'fresh.xlsx', 'content': base64.b64encode(self.source).decode()})
            client.request('POST', '/api/avito/import/commit', {'session_id': imported['session_id'], 'categories': ['Мебель и интерьер']})
            listing = client.request('GET', '/api/avito/listings')['items'][0]
            product = client.products()[0]
            detail = client.request('GET', '/api/avito/listings/'+listing['id'])
            from backend.avito import product_snapshot
            client.request('POST', '/api/avito/decisions', {'action': 'confirm', 'listing_id': listing['id'],
                'listing_version': listing['version'], 'link_version': detail['link_version'], 'product_id': product['id'],
                'product_snapshot': product_snapshot(product), 'reason': 'Проверка экспорта'})
            plan = client.request('POST', '/api/avito/prepare', {'factory': 'Регион 058'})
            payload = {'name': 'fresh.xlsx', 'content': base64.b64encode(self.source).decode(), 'plan_id': plan['id'], 'listing_ids': [listing['id']], 'mode': PILOT_MODE}
            preview = client.request('POST', '/api/avito/export/preview', payload)
            self.assertTrue(preview['upload_allowed'])
            self.assertEqual(preview['selected_found_count'], 1)
            downloaded = client.request('POST', '/api/avito/export/download', {'session_id': preview['session_id'], 'reviewed_prices': True, 'first_manual_pilot': True})
            rows = parsed_bytes(base64.b64decode(downloaded['content']), money_cents)['records']
            self.assertEqual([r['avito_id'] for r in rows], ['123456789'])
            self.assertFalse(client.request('GET', '/api/avito/state')['transport_enabled'])
            client.expect_http_error('POST', '/api/avito/export/preview', payload, headers={'Origin': 'https://foreign.example'}, statuses=(403,)).close()
            client.expect_http_error('POST', '/api/avito/send', {}, statuses=(403,)).close()
        finally:
            client.tearDown()


if __name__ == '__main__':
    unittest.main()
