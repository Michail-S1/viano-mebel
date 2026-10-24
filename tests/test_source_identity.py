"""Cross-price-list duplicate repair preserves variants, user data and replay identity."""
import copy
import json
import time
import unittest
import zipfile
from unittest.mock import patch
import test_catalog_identity as fixtures
from backend.catalog_identity import repair_plan
from backend.source_identity import SourceIndex, descriptor, compatible
from server import compact


def item(name='НКМ Аэлита сек. №4 скамья',**changes):
    p=fixtures.product(name=name,factory='Росток',collection='НКМ Аэлита',dimensions='Ш: 445; В: 420; Г: 280',color='Дуб Вотан / Черный')
    p.update(changes);return p


def stock(p=None,**changes):
    p=copy.deepcopy(p or item());colour=p.get('color','');parent=p['name']
    p.update(name=parent+(' — '+colour if colour else ''),dimensions='',collection='АЭЛИТА',
             variant=colour,source_file='Ostatki.xlsx',source_sheet='РОСТОК',source_row=22,
             name_context={'kind':'warehouse-characteristic','parent_name':parent,'original_name':colour})
    p.update(changes);return p


class CrossSourceIdentityTests(unittest.TestCase):
    setUp=fixtures.SupplierIdentityTests.setUp
    tearDown=fixtures.SupplierIdentityTests.tearDown
    seed=fixtures.SupplierIdentityTests.seed

    def preview_many(self,products,mode='update'):
        sid='cross-test';cfg={'file_id':'file','factory':products[0]['factory'],'sheets':[],'import_photos':False}
        self.catalog.sessions[sid]={'created':time.time(),'files':{'file':{'name':'test.xlsx','path':self.root/'unused.xlsx'}},'configs':[cfg]}
        with patch('server.parse_file',return_value={'products':products,'issues':[]}):
            p=self.catalog.preview({'session_id':sid,'mode':mode})
        return sid,p

    def test_screenshot_pairs_merge_with_sizes_colour_price_and_old_id(self):
        a=item();self.seed('bench',a)
        self.seed('bench-stock',stock(a,base_price=1200),'2026-10-23')
        b=item('НКМ Аэлита сек. №1 тумба подвесная с зеркалом',dimensions='Ш: 1200; В: 2200; Г: 350')
        self.seed('tumba',b)
        self.seed('tumba-stock',stock(b,source_row=50),'2026-10-23')
        c=stock(item('НКМ Аэлита сек №1 Тумба подвес Зеркало круглое D859',color=''),source_row=70,collection='Другая группа')
        self.seed('tumba-short',c,'2026-10-23')
        result=self.catalog.repair_catalog_identity(force=True)
        self.assertEqual(result['removed'],3)
        self.assertEqual(self.catalog.product('bench-stock')['id'],'bench')
        self.assertEqual(self.catalog.product('tumba-short')['id'],'tumba')
        self.assertEqual(self.catalog.product('bench')['dimensions'],a['dimensions'])
        self.assertEqual(self.catalog.product('bench')['base_price'],1200)
        self.assertEqual(self.catalog.product('tumba')['color'],b['color'])
        with zipfile.ZipFile(result['backup']) as z:self.assertIn('catalog.sqlite3',z.namelist())
        self.assertEqual(self.catalog.repair_catalog_identity(force=True)['removed'],0)

    def test_ambiguous_missing_colour_requires_review(self):
        self.seed('black',item(color='Черный'))
        self.seed('white',item(color='Белый'))
        incoming=stock(item(color=''))
        self.seed('unknown',incoming,'2026-10-23')
        with self.catalog.connection() as db:plan=repair_plan(db)
        self.assertFalse(plan['groups']);self.assertTrue(plan['review'])
        # Use another unknown identity, avoiding its exact imported alias.
        incoming['collection']='Новое имя группы'
        _,p=self.preview_many([incoming])
        self.assertEqual(p['counts']['new'],0);self.assertEqual(p['counts']['issues'],1)

    def test_different_colours_sizes_factories_components_and_upholstery_survive(self):
        base=item();a=descriptor(base)
        for p in [item(color='Белый'),item(dimensions='Ш: 500; В: 420; Г: 280'),item(factory='Другая'),
                  item(variant='Корпус'),item(variant='1 кат / Боска 03')]:
            self.assertFalse(compatible(a,descriptor(p)))
        self.seed('old',base)
        self.seed('new',stock(item(color='Белый')),'2026-10-23')
        self.assertEqual(self.catalog.repair_catalog_identity(force=True)['removed'],0)

    def test_two_incoming_variants_cannot_both_fill_one_incomplete_record(self):
        self.seed('unknown',item(color=''))
        incoming=[stock(item(color='Белый')),stock(item(color='Черный'))]
        _,p=self.preview_many(incoming)
        self.assertEqual(p['counts']['new'],0);self.assertEqual(p['counts']['issues'],2)
        self.assertEqual(p['counts']['updated'],0)

    def test_historical_unknown_colour_does_not_swallow_a_new_colour(self):
        self.seed('old',item(color=''))
        self.seed('black',stock(item(color='Черный')),'2026-10-23')
        self.catalog.repair_catalog_identity(force=True)
        _,p=self.preview_many([stock(item(color='Белый'))])
        self.assertEqual(p['counts']['new'],1)

    def test_future_changed_group_updates_same_id_and_retains_rich_dimensions(self):
        original=item();self.seed('old',original)
        incoming=stock(original,base_price=1200,collection='Ещё одна группа')
        sid,p=self.preview_many([incoming]);self.assertEqual(p['counts']['new'],0)
        self.assertEqual(p['counts']['updated'],1);self.catalog.commit(sid)
        self.assertEqual(self.catalog.product('old')['dimensions'],original['dimensions'])
        self.assertEqual(self.catalog.product('old')['name'],original['name'])
        self.assertEqual(self.catalog.product('old')['base_price'],1200)
        sid,p=self.preview_many([incoming]);self.assertEqual(p['counts']['duplicates'],1)
        self.catalog.commit(sid)
        incoming['source_file']='Завтрашний.xlsx';incoming['collection']='Новая группа'
        _,p=self.preview_many([incoming]);self.assertEqual(p['counts']['new'],0)

    def test_add_only_keeps_existing_price_but_remembers_new_source(self):
        self.seed('old',item())
        sid,p=self.preview_many([stock(base_price=5000)],'add')
        self.assertEqual(p['counts']['duplicates'],1);self.catalog.commit(sid)
        self.assertEqual(self.catalog.product('old')['base_price'],1000)
        with self.catalog.connection() as db:self.assertTrue(self.catalog.meta(db,'duplicate_sources:old'))

    def test_conflicting_manual_edits_are_not_merged(self):
        self.seed('old',item(),overrides={'name':'Моя скамья'})
        self.seed('new',stock(), '2026-10-23',overrides={'name':'Другая скамья'})
        with self.catalog.connection() as db:plan=repair_plan(db)
        self.assertFalse(plan['groups']);self.assertTrue(plan['review'])

    def test_photos_primary_and_kit_ids_survive_repair(self):
        self.seed('old',item(),overrides={'name':'Моя скамья'})
        self.seed('new',stock(),'2026-10-23')
        with self.catalog.connection() as db:
            for identifier,owner,data in [('p1','old',b'old-image'),('p2','new',b'new-image')]:
                (self.catalog.photos_dir/(identifier+'.jpg')).write_bytes(data)
                db.execute('INSERT INTO photos VALUES (?,?,?,?,?,?)',(identifier,owner,identifier+'.jpg','manual',1,'old'))
        self.catalog.repair_catalog_identity(force=True)
        p=self.catalog.product('new');self.assertEqual(p['id'],'old');self.assertEqual(p['name'],'Моя скамья')
        self.assertEqual(len(p['photos']),2);self.assertEqual(p['photos'][0]['id'],'p1')
        quote=self.catalog.quote_selection({'items':[{'id':'new','quantity':2}]})
        self.assertTrue(quote['complete']);self.assertEqual(quote['items'][0]['product']['id'],'old')

    def test_same_warehouse_rows_with_conflicting_prices_are_not_merged(self):
        self.seed('old',item())
        self.seed('a',stock(base_price=1000),'2026-10-23')
        self.seed('b',stock(collection='Другой раздел',base_price=1100,source_row=33),'2026-10-23')
        with self.catalog.connection() as db:plan=repair_plan(db)
        self.assertFalse(plan['groups']);self.assertTrue(plan['review'])

    def test_reordered_furniture_type_uses_exact_remaining_model_tokens(self):
        old=fixtures.product(factory='NN-Мебель',name='Кровать Марс',dimensions='',color='Белый',collection='Спальни')
        incoming=stock(dict(old,name='Марс кровать'))
        self.seed('old',old);self.seed('stock',incoming,'2026-10-23')
        self.assertEqual(self.catalog.repair_catalog_identity(force=True)['removed'],1)

    def test_price_alone_never_establishes_identity(self):
        self.seed('old',item())
        self.seed('new',stock(item('НКМ Другая модель сек. №4 скамья')),'2026-10-23')
        self.assertEqual(self.catalog.repair_catalog_identity(force=True)['removed'],0)

    def test_previous_repair_id_redirects_survive_new_repair_without_new_groups(self):
        self.seed('old',item())
        with self.catalog.connection() as db:self.catalog.set_meta(db,'catalog_identity_repair',{'redirects':{'previous':'old'}})
        self.catalog.repair_catalog_identity(force=True)
        self.assertEqual(self.catalog.product('previous')['id'],'old')

    def test_two_prices_for_same_cross_source_target_require_review(self):
        self.seed('old',item())
        a=stock(base_price=1000);b=stock(base_price=1200,collection='Другая группа',source_row=44)
        _,p=self.preview_many([a,b])
        self.assertEqual(p['counts']['issues'],2);self.assertEqual(p['counts']['updated'],0)

    def test_browser_kit_redirects_combine_quantities_and_reject_overflow(self):
        import shutil,subprocess
        from pathlib import Path
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable')
        js=(Path(__file__).resolve().parents[1]/'static/kit.js').read_text('utf-8')
        block=js[js.index('      const redirected ='):js.index('      quote = result;')]
        sanitize=js[js.index('  function sanitize('):js.index('  function readSelection(')]
        code="const assert=require('node:assert/strict');const MAX_QUANTITY=999,MAX_LINES=500;"+sanitize+"""
function run(input){let items=input,changedCount=0;const changed=()=>changedCount++;
const received=new Map(items.map(item=>[item.id,{product:{id:'kept'}}]));
function apply(){"""+block+"""}apply();return {items,changedCount};}
assert.deepEqual(run([{id:'old',quantity:2},{id:'kept',quantity:3}]),{items:[{id:'kept',quantity:5}],changedCount:1});
assert.throws(()=>run([{id:'old',quantity:999},{id:'kept',quantity:1}]),/999/);
"""
        subprocess.run([node,'-e',code],check=True,capture_output=True)

    def test_explicit_different_types_mirrors_and_lifting_mechanisms_survive(self):
        for a,b in [('НКМ Лира сек. №1 тумба','НКМ Лира сек. №1 шкаф'),
                    ('НКМ Лира сек. №1 кровать без подъемного механизма','НКМ Лира сек. №1 кровать с подъемным механизмом'),
                    ('НКМ Лира сек. №1 шкаф без зеркал','НКМ Лира сек. №1 шкаф с зеркалами')]:
            self.assertFalse(compatible(descriptor(item(a)),descriptor(stock(item(b)))))

    def test_missing_historic_mirror_flag_cannot_swallow_explicit_other_variant(self):
        a=item('НКМ Лира сек. №1 тумба с зеркалом')
        self.seed('old',a)
        self.seed('stock',stock(a),'2026-10-23')
        short=stock(item('НКМ Лира сек. №1 тумба Зеркало круглое D859'),collection='Другая группа',source_row=50)
        self.seed('short',short,'2026-10-23')
        self.catalog.repair_catalog_identity(force=True)
        _,preview=self.preview_many([stock(item('НКМ Лира сек. №1 тумба без зеркала'))])
        self.assertEqual(preview['counts']['new'],1)

    def test_known_supplier_identity_namespace_is_retained_when_enriching(self):
        from backend.source_identity import enriched
        a=item(identity_context={'kind':'1c-nomenclature','version':1})
        b=stock(a);b.pop('identity_context')
        self.assertEqual(enriched(a,b)['identity_context'],a['identity_context'])

    def test_sku_name_collision_is_checked_before_preserving_rich_name(self):
        a=item(sku='001');self.seed('old',a)
        b=stock(a,name='Совсем другой товар',collection=a['collection'],dimensions=a['dimensions'],variant=a['variant'])
        b['name_context']['parent_name']='Совсем другой товар'
        _,p=self.preview_many([b]);self.assertEqual(p['counts']['issues'],1)
        self.assertEqual(p['counts']['updated'],0)
