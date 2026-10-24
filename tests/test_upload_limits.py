"""Upload boundaries, large embedded images and supplier detection regressions."""
import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server import ApiError, Catalog, MAX_BODY, MAX_EXCEL_BYTES, decode_file
from backend.importer import inspect_file, parse_file
from backend.images import MAX_IMAGE, MAX_TOTAL, read_pictures
from test_importer import fixture


class UploadLimits(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='dinar-upload-qa-')
        self.root=Path(self.tmp.name)
        self.path=self.root/'PRAJS_KROVATEJ_DINAR.xlsx'
        fixture(self.path,{'Прайс':{'rows':[
            {'B':'ДИНАР МЕБЕЛЬ'},{'B':'ЗВОНИТЬ И ПИСАТЬ ПО ВОПРОСАМ НА : +7 000 000 00 00'},
            {'B':'НАЗВАНИЕ КРОВАТИ','I':'РАЗМЕР КРОВАТИ','L':'ЦЕНА БЕЗ П/М','N':'ЦЕНА С П/М'},
            {'B':'Октавия','I':'1200ММ Х 2000ММ','L':1000,'N':1500},
            {'B':'Октавия','I':'1400ММ Х 2000ММ','L':1200,'N':1700,'Q':'КОМПЛЕКТ ПОДСВЕТКИ'}]}})
        self.cat=Catalog(self.root/'data')
    def tearDown(self):
        self.cat.stop_event.set();self.tmp.cleanup()

    def test_limits_fit_encoded_single_file_in_request(self):
        self.assertEqual(MAX_EXCEL_BYTES,160*1024*1024)
        self.assertLess(4*((MAX_EXCEL_BYTES+2)//3)+65536,MAX_BODY)
        self.assertEqual(self.cat.state()['import_limits'],{'file_bytes':MAX_EXCEL_BYTES,'request_bytes':MAX_BODY})
        self.assertGreater(MAX_IMAGE,16581565)
        self.assertGreater(MAX_TOTAL,113831092)

    def test_decode_distinguishes_empty_and_too_large_and_checks_before_decode(self):
        self.assertEqual(decode_file({'content':base64.b64encode(b'abc').decode()},3),b'abc')
        with self.assertRaisesRegex(ApiError,'пустой'):decode_file({'content':''},3)
        with patch('server.base64.b64decode') as decode:
            with self.assertRaisesRegex(ApiError,'превышает'):decode_file({'content':'YWFhYWFh'},3)
            decode.assert_not_called()
        with self.assertRaisesRegex(ApiError,'превышает'):decode_file({'content':'YWJj'},2)
        with self.assertRaisesRegex(ApiError,'формат'):decode_file({'content':'!!!!'},3)

    def test_browser_upload_and_local_path_share_same_inclusive_limit(self):
        content=self.path.read_bytes()
        entry={'name':self.path.name,'content':base64.b64encode(content).decode()}
        for payload in ({'files':[entry]},{'paths':[str(self.path)]},{'folder':str(self.root)}):
            with patch('server.MAX_EXCEL_BYTES',len(content)):
                self.assertEqual(len(self.cat.inspect(payload)['files']),1)
            with patch('server.MAX_EXCEL_BYTES',len(content)-1):
                with self.assertRaisesRegex(ApiError,'превышает'):self.cat.inspect(payload)
        empty=self.root/'Пустой.xlsx';empty.touch()
        with self.assertRaisesRegex(ApiError,'пустой'):self.cat.inspect({'paths':[str(empty)]})

    def test_dinar_factory_contact_line_and_lift_mechanism_price(self):
        config=inspect_file(str(self.path),'file')
        self.assertEqual(config['factory'],'Динар')
        self.assertEqual(config['sheets'][0]['mapping']['price'],'L')
        parsed=parse_file(str(self.path),config)
        self.assertEqual([p['base_price'] for p in parsed['products']],[1000,1200])
        self.assertTrue(all('ЗВОНИТЬ' not in p['collection'] and '+7' not in p['collection'] for p in parsed['products']))
        config['sheets'][0]['mapping']['price']='N'
        self.assertEqual([p['base_price'] for p in parse_file(str(self.path),config)['products']],[1500,1700])

    def test_backlight_word_is_not_dsv_factory(self):
        p=self.root/'Без фабрики.xlsx'
        fixture(p,{'Прайс':{'rows':[{'A':'КОМПЛЕКТ ПОДСВЕТКИ'},
            {'A':'Наименование','B':'Цена'},{'A':'Подсветка','B':1000}]}})
        self.assertEqual(inspect_file(str(p),'file')['factory'],'')
        fixture(p,{'Прайс':{'rows':[{'A':'Фабрика ДСВ'},
            {'A':'Наименование','B':'Цена'},{'A':'Шкаф','B':1000}]}})
        self.assertEqual(inspect_file(str(p),'file')['factory'],'ДСВ')

    def test_browser_preflight_accepts_real_size_and_rejects_oversized_batch(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node runtime is unavailable')
        source=(Path(__file__).resolve().parents[1]/'static/app.js').read_text('utf-8')
        function=source[source.index('function validatePriceFiles('):source.index('function renderImportFiles(')]
        script="const assert=require('node:assert/strict');const ui={state:{import_limits:{file_bytes:167772160,request_bytes:251658240}}};"+function+"""
validatePriceFiles([{name:'Dinar.xlsx',size:113893349}]);
validatePriceFiles([{name:'Boundary.xlsx',size:167772160}]);
assert.throws(()=>validatePriceFiles([{name:'Empty.xlsx',size:0}]),/пустой/);
assert.throws(()=>validatePriceFiles([{name:'Too big.xlsx',size:167772161}]),/160/);
assert.throws(()=>validatePriceFiles([{name:'A.xlsx',size:113893349},{name:'B.xlsx',size:113893349}]),/по одному/);
"""
        subprocess.run([node,'-e',script],check=True,capture_output=True)

    def test_bed_view_photo_column_can_be_right_of_product_name(self):
        from excel_picture_fixtures import add_pictures
        from backend.images import product_pictures
        p=self.root/'Dinar-right-photo.xlsx'
        fixture(p,{'Кровати':{'rows':[{'B':'Динар мебель'},
            {'B':'Название кровати','D':'Вид кровати','I':'Размер кровати','L':'Цена'},
            {'B':'Октавия','I':'1200x2000','L':1000},{'I':'1400x2000','L':1200}],
            'merges':['B3:B4','D3:H4']}})
        add_pictures(p,[(2,3,3,7,False)])
        config=inspect_file(str(p),'pic');products=parse_file(str(p),config)['products']
        matches,warnings=product_pictures(p,config,products)
        self.assertFalse(warnings)
        self.assertEqual(set(matches),{('Кровати',3),('Кровати',4)})

    def test_real_large_dinar_source_is_read_without_changing_it(self):
        source=Path.home()/'Downloads/PRAJS_KROVATEJ_DINAR_12_10_2026_1.xlsx'
        if not source.is_file():self.skipTest('User Dinar source is not installed')
        before=(source.stat().st_size,source.stat().st_mtime_ns)
        config=inspect_file(str(source),'real');parsed=parse_file(str(source),config)
        self.assertLess(source.stat().st_size,MAX_EXCEL_BYTES)
        self.assertEqual(config['factory'],'Динар')
        self.assertEqual(len(parsed['products']),102)
        self.assertEqual(len([p for p in parsed['products'] if p['base_price']]),100)
        self.assertTrue(all('ЗВОНИТЬ' not in p['collection'] for p in parsed['products']))
        pictures,warnings=read_pictures(source)
        self.assertEqual(len(pictures),26);self.assertFalse(warnings)
        self.assertEqual(before,(source.stat().st_size,source.stat().st_mtime_ns))
