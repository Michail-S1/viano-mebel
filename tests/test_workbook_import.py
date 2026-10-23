"""Hierarchical exports retain every sheet, manufacturer and parent/variant relation."""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from backend.importer import inspect_file, parse_file, _read, _price
from server import Catalog, profile_family
from test_importer import fixture, NS


def warehouse(path, sheets):
    fixture(path, {name:{"rows":data[0]} for name,data in sheets.items()})
    with zipfile.ZipFile(path) as source:
        parts={name:source.read(name) for name in source.namelist()}
    parts['xl/styles.xml']=(f'<styleSheet xmlns="{NS}"><cellXfs count="9">'+''.join(
        f'<xf><alignment indent="{i}"/></xf>' for i in range(9))+'</cellXfs></styleSheet>').encode()
    for i,(_,levels) in enumerate(sheets.values(),1):
        name=f'xl/worksheets/sheet{i}.xml';root=ET.fromstring(parts[name])
        for row in root.findall('{'+NS+'}sheetData/{'+NS+'}row'):
            n=int(row.attrib['r'])
            if n not in levels:continue
            cell=next((c for c in row if c.attrib.get('r')=='A'+str(n)),None)
            if cell is None:cell=ET.SubElement(row,'{'+NS+'}c',{'r':'A'+str(n)})
            cell.set('s',str(levels[n]))
        parts[name]=ET.tostring(root)
    with zipfile.ZipFile(path,'w') as target:
        for name,value in parts.items():target.writestr(name,value)


def stock_rows(factory='РЕГИНА КОРПУС'):
    rows=[{'A':'Склад','B':'Остаток','C':'Цена','D':'Сумма'}, {'A':'Номенклатура'}, {'A':'Характеристика'},
          {'A':'ОСНОВНОЙ','B':100,'D':999999}, {'A':factory,'B':100,'D':999999},
          {'A':'Шкаф АВИС (корпус)','B':5,'D':6500},
          {'A':'0,4','B':2,'C':1000,'D':2000}, {'A':'0,5','B':3,'C':1500,'D':4500},
          {'A':'Тумба БЕТА','B':9,'D':18000}, {'A':'Белый','B':9,'C':2000,'D':18000},
          {'A':'Пенал без цены','B':1}, {'B':1}]
    return rows,{4:0,5:1,6:2,7:4,8:4,9:2,10:4,11:2,12:4}


class WorkbookImport(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='workbook-qa-');self.root=Path(self.tmp.name);self.path=self.root/'Ostatki_22_10.xlsx'
    def tearDown(self):self.tmp.cleanup()

    def test_all_sheets_factories_unit_prices_and_numeric_children(self):
        warehouse(self.path, {'Регина':stock_rows(), 'Стендмебель':stock_rows('СТЕНДМЕБЕЛЬ')})
        config=inspect_file(str(self.path),'file')
        self.assertEqual(config['factory'],'')
        self.assertEqual([s['factory'] for s in config['sheets']],['Регина','Стендмебель'])
        self.assertTrue(all(s['selected'] for s in config['sheets']))
        parsed=parse_file(str(self.path),config)
        priced=[p for p in parsed['products'] if p['base_price'] is not None]
        self.assertEqual(len(priced),6)
        self.assertEqual({p['factory'] for p in priced},{'Регина','Стендмебель'})
        self.assertEqual([p['base_price'] for p in priced[:3]],[1000,1500,2000])
        self.assertIn('Шкаф АВИС (корпус)',priced[0]['name'])
        self.assertIn('0,4',priced[0]['name'])
        self.assertNotEqual(priced[0]['name'],priced[1]['name'])
        self.assertEqual(priced[0]['name_context']['original_name'],'0,4')
        self.assertEqual(len(parsed['issues']),2)
        config['sheets'][1]['selected']=False
        self.assertEqual({p['factory'] for p in parse_file(str(self.path),config)['products']},{'Регина'})

    def test_multiple_factory_groups_inside_one_sheet_stay_separate(self):
        first,levels=stock_rows('РЕГИНА')
        second,_=stock_rows('СТЕНДМЕБЕЛЬ')
        joined=first[:10]+second[4:10]
        levels={4:0,5:1,6:2,7:4,8:4,9:2,10:4,11:1,12:2,13:4,14:4,15:2,16:4}
        warehouse(self.path,{'Дополнение':(joined,levels)})
        config=inspect_file(str(self.path),'file')
        self.assertEqual(set(config['sheets'][0]['factory_groups']),{'Регина','Стендмебель'})
        self.assertEqual(config['sheets'][0]['factory'],'')
        parsed=parse_file(str(self.path),config)
        self.assertEqual({p['factory'] for p in parsed['products']},{'Регина','Стендмебель'})

    def test_total_and_quantity_columns_are_rejected_as_unit_price(self):
        warehouse(self.path,{'Регина':stock_rows()});config=inspect_file(str(self.path),'file')
        for col in ['B','D']:
            config['sheets'][0]['mapping']['price']=col
            parsed=parse_file(str(self.path),config)
            self.assertFalse(parsed['products']);self.assertTrue(parsed['issues'])

    def test_generic_supplement_inherits_and_manual_factory_is_saved(self):
        rows=[{'A':'Наименование','B':'Цена'},{'A':'Тумба Лира','B':1000}]
        fixture(self.path,{'ДСВ':{'rows':rows},'Дополнение':{'rows':rows},'МИФ':{'rows':rows}})
        config=inspect_file(str(self.path),'file')
        self.assertEqual([s['factory'] for s in config['sheets']],['ДСВ','ДСВ','МИФ'])
        parsed=parse_file(str(self.path),config)
        self.assertEqual([p['factory'] for p in parsed['products']],['ДСВ','ДСВ','МИФ'])
        cat=Catalog(self.root/'data')
        try:
            inspected=cat.inspect({'paths':[str(self.path)]});files=inspected['files']
            files[0]['sheets'][1]['factory']='Собственная фабрика'
            preview=cat.preview({'session_id':inspected['session_id'],'files':files,'mode':'add'})
            self.assertEqual(preview['counts']['issues'],0)
            cat.commit(inspected['session_id'])
            another=self.root/'Ostatki_23_10.xlsx';another.write_bytes(self.path.read_bytes())
            reloaded=cat.inspect({'paths':[str(another)]})
            self.assertEqual(reloaded['files'][0]['sheets'][1]['factory'],'Собственная фабрика')
            self.assertEqual(profile_family(self.path.name),profile_family(another.name))
        finally:cat.stop_event.set()

    def test_empty_file_factory_imports_all_detected_factories_and_repeats_without_copies(self):
        warehouse(self.path, {'Регина':stock_rows(), 'Стендмебель':stock_rows('СТЕНДМЕБЕЛЬ')})
        cat=Catalog(self.root/'multi-db')
        try:
            inspected=cat.inspect({'paths':[str(self.path)]});files=inspected['files']
            self.assertEqual(files[0]['factory'],'')
            self.assertEqual(files[0]['detected_factory'],'')
            self.assertEqual([s['detected_factory'] for s in files[0]['sheets']],['Регина','Стендмебель'])
            preview=cat.preview({'session_id':inspected['session_id'],'files':files,'mode':'add'})
            self.assertEqual(preview['counts']['new'],6)
            self.assertEqual(preview['factories'],[{'name':'Регина','count':3},{'name':'Стендмебель','count':3}])
            self.assertEqual(preview['counts']['issues'],2)  # Two source rows have no price.
            self.assertEqual(cat.commit(inspected['session_id'])['added'],6)
            again=cat.inspect({'paths':[str(self.path)]})
            repeat=cat.preview({'session_id':again['session_id'],'files':again['files'],'mode':'update'})
            self.assertEqual(repeat['counts']['new'],0)
            self.assertEqual(repeat['counts']['duplicates'],6)
            self.assertEqual(repeat['factories'],preview['factories'])
        finally:cat.stop_event.set()

    def test_auto_factory_metadata_survives_manual_profile_and_can_be_restored(self):
        warehouse(self.path, {'Регина':stock_rows(), 'Стендмебель':stock_rows('СТЕНДМЕБЕЛЬ')})
        cat=Catalog(self.root/'auto-db')
        try:
            inspected=cat.inspect({'paths':[str(self.path)]});files=inspected['files']
            files[0]['sheets'][1]['factory']='Ручная фабрика'
            cat.preview({'session_id':inspected['session_id'],'files':files,'mode':'add'})
            cat.commit(inspected['session_id'])
            again=cat.inspect({'paths':[str(self.path)]});config=again['files'][0]
            self.assertEqual(config['sheets'][1]['factory'],'Ручная фабрика')
            self.assertEqual(config['sheets'][1]['detected_factory'],'Стендмебель')
            config['factory']=config['detected_factory']
            for sheet in config['sheets']:sheet['factory']=sheet['detected_factory']
            preview=cat.preview({'session_id':again['session_id'],'files':[config],'mode':'add'})
            self.assertEqual({f['name'] for f in preview['factories']},{'Регина','Стендмебель'})
        finally:cat.stop_event.set()

    def test_numeric_names_use_proven_parent_or_require_review(self):
        rows=[{'A':'Наименование','B':'Цена'},{'A':'Шкаф АВИС (корпус)'},
              {'A':'0,4','B':1000},{'A':'0,5','B':1200}, {'A':'Спальни'}, {'A':'0,6','B':1400}]
        fixture(self.path,{'Прайс':{'rows':rows}})
        config={'factory':'Фабрика'}
        parsed=parse_file(str(self.path),config)
        self.assertEqual([p['name'] for p in parsed['products']],['Шкаф АВИС (корпус) — модуль 0,4','Шкаф АВИС (корпус) — модуль 0,5'])
        self.assertEqual(len(parsed['issues']),1)
        config['sheets']=[{'name':'Прайс','selected':True,'component_parent':'Корпус Лира'}]
        explicit=parse_file(str(self.path),config)
        self.assertIn('Корпус Лира — модуль 0,6',[p['name'] for p in explicit['products']])

    def test_bts_multiline_header_article_and_repeated_summary_rows(self):
        rows=[{'A':'Прайс-лист на сегодня'}, {}, {'A':'Ценовая группа','N':'БТС Оптовая_руб'},
              {'A':'Артикул','E':'Номенклатура, Характеристика, Упак.','N':'RUB'},
              {'A':'Номенклатура, Характеристика ценообразования, Упак.','N':'Включает НДС'},
              {'N':'Цена','O':'Остаток'}, {},
              {'A':'00123','E':'"Лира" шкаф белый, , шт','N':1000,'O':2},
              {'A':'"Лира" шкаф белый, , шт','N':1000},
              {'A':'00123','E':'"Лира" шкаф графит, , шт','N':1200,'O':3},
              {'A':'"Лира" шкаф графит, , шт','N':1200},
              {'A':'00123','E':'"Лира" комод белый, , шт','N':1500,'O':1}]
        fixture(self.path,{'Лист_1':{'rows':rows,'merges':['A9:M9','A11:M11']}})
        config=inspect_file(str(self.path),'bts')
        self.assertEqual(config['factory'],'БТС')
        sheet=config['sheets'][0]
        self.assertTrue(sheet['selected'])
        self.assertEqual((sheet['mapping']['name'],sheet['mapping']['price'],sheet['mapping']['sku']),('E','N','A'))
        parsed=parse_file(str(self.path),config)
        self.assertEqual(len(parsed['products']),3);self.assertFalse(parsed['issues'])
        self.assertEqual([p['sku'] for p in parsed['products']],['00123','00123','00123'])
        from backend.importer import product_key
        self.assertEqual(len({product_key(p) for p in parsed['products']}),3)
        self.assertEqual(parsed['products'][0]['base_price'],1000)

    def test_real_bts_export_automatically_reads_all_products_once(self):
        source=Path.home()/'Downloads'/'ostatki_bts_23_10_26.xlsx'
        if not source.is_file():self.skipTest('User BTS export is not installed')
        config=inspect_file(str(source),'bts');parsed=parse_file(str(source),config)
        self.assertEqual(config['factory'],'БТС');self.assertTrue(config['sheets'][0]['selected'])
        original=_read(str(source))[0]
        expected=sum(bool(row.get(5)) and (_price(row.get(14)) or 0)>0 for row in original.rows.values())
        self.assertEqual(len(parsed['products']),expected)
        self.assertFalse(parsed['issues'])
        self.assertTrue(all(p['factory']=='БТС' for p in parsed['products']))
        self.assertTrue(any(p['sku'].startswith('0') for p in parsed['products']))
        cat=Catalog(self.root/'bts-db')
        try:
            inspected=cat.inspect({'paths':[str(source)]})
            for f in inspected['files']:f['import_photos']=False
            preview=cat.preview({'session_id':inspected['session_id'],'files':inspected['files'],'mode':'add'})
            self.assertEqual(preview['counts']['new'],expected)
            self.assertEqual(preview['counts']['issues'],0)
            cat.commit(inspected['session_id'])
            again=cat.inspect({'paths':[str(source)]})
            for f in again['files']:f['import_photos']=False
            repeat=cat.preview({'session_id':again['session_id'],'files':again['files'],'mode':'update'})
            self.assertEqual(repeat['counts']['new'],0)
            self.assertEqual(repeat['counts']['updated'],0)
        finally:cat.stop_event.set()


    def test_real_export_all_18_sheets_and_every_priced_row(self):
        source=Path.home()/'Downloads'/'Ostatki_22_10.xlsx'
        if not source.is_file():self.skipTest('User export is not installed')
        config=inspect_file(str(source),'test');parsed=parse_file(str(source),config)
        self.assertEqual(len(config['sheets']),18)
        self.assertTrue(all(s['selected'] for s in config['sheets']))
        original={s.name:s for s in _read(str(source))}
        priced=[p for p in parsed['products'] if p['base_price'] is not None]
        expected=sum(1 for s in original.values() for row in s.rows.values() if (_price(row.get(3)) or 0)>0)
        self.assertEqual(len(priced),expected)
        self.assertTrue(all(p['factory'] for p in priced))
        self.assertEqual({p['source_sheet'] for p in priced},set(original))
        for p in priced:
            self.assertEqual(p['base_price'],_price(original[p['source_sheet']].rows[p['source_row']].get(3)))
            self.assertTrue(any(c.isalpha() for c in p['name']))


if __name__=='__main__':unittest.main()
