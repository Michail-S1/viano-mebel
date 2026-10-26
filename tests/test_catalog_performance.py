"""Read optimizations preserve exact prices, matching and cache freshness."""
import copy
import unittest
from unittest.mock import patch

from server import sale_price
import test_catalog_identity as fixtures


class ReadOptimizationTests(unittest.TestCase):
    setUp = fixtures.SupplierIdentityTests.setUp
    tearDown = fixtures.SupplierIdentityTests.tearDown
    seed = fixtures.SupplierIdentityTests.seed

    def populate(self):
        for i, price in enumerate([None, 0, 0.004, 0.005, 1, 99.5, 100, 100.4, 100.5, 101, 9000]):
            self.seed(str(i), fixtures.product(name='шкаф кр-' + str(i), sku=str(i), base_price=price,
                                      dimensions='800×2200×500' if i % 2 else '1800×2200×500'))

    def test_price_ranges_equal_original_decimal_calculation_at_rounding_boundaries(self):
        self.populate()
        for rounding in ('none', 'ruble', 'ten', 'hundred'):
            self.catalog.settings({'markup_percent':17.25, 'fixed_add':0.005, 'rounding':rounding})
            with self.catalog.connection() as db:
                rows = [dict(r) for r in db.execute('SELECT id,base_price FROM products')]
            settings = self.catalog.state()['settings']
            boundaries = {-1, 0, 0.01, 100, 100.49, 100.5, 100.51, 1e6}
            for row in rows:
                value = sale_price(row['base_price'], settings)
                if value is not None:
                    boundaries.update((max(0, value-0.001), value, value+0.001))
            for boundary in sorted(n for n in boundaries if n >= 0):
                for field in ('min', 'max'):
                    result = self.catalog.list_products({field:str(boundary), 'page_size':'120'})
                    expected = {r['id'] for r in rows if r['base_price'] is not None and
                                (sale_price(r['base_price'], settings) >= boundary if field == 'min' else
                                 sale_price(r['base_price'], settings) <= boundary)}
                    self.assertEqual({r['id'] for r in result['items']}, expected, (rounding,field,boundary))

    def test_cached_results_are_independent_bounded_and_invalidated_by_edit(self):
        self.populate()
        params = {'q':'кр', 'dimensions':'800'}
        first = self.catalog.list_products(params)
        original = copy.deepcopy(first)
        first['items'].clear()
        with patch.object(self.catalog, 'serialize_product', wraps=self.catalog.serialize_product) as serialize:
            self.assertEqual(self.catalog.list_products(params), original)
            serialize.assert_not_called()
        self.catalog.edit_product('1', {'name':'кровать', 'dimensions':'900×2200×500'})
        self.assertNotIn('1', {p['id'] for p in self.catalog.list_products(params)['items']})
        for i in range(60):
            self.catalog.list_products({'q':'missing'+str(i)})
        self.assertLessEqual(len(self.catalog._read_cache), 48)

    def test_settings_refresh_cached_price_steps_and_visible_prices(self):
        self.populate()
        self.catalog.settings({'markup_percent':20, 'fixed_add':0})
        query = {'min':'120', 'max':'126', 'page_size':'120'}
        before = self.catalog.list_products(query)
        self.catalog.settings({'markup_percent':0, 'fixed_add':0, 'rounding':'none'})
        after = self.catalog.list_products(query)
        self.assertNotEqual(before, after)
        self.assertEqual(after['total'], 0)
        self.assertEqual(self.catalog.list_products({'max':'101'})['total'], 9)

    def test_lightweight_poll_keeps_folder_fresh_without_catalog_queries(self):
        self.populate()
        initial = self.catalog.state()
        initial['factories'].clear()
        revision = self.catalog.state()['revision']
        with patch.object(self.catalog, 'cached_read', side_effect=AssertionError('Full state requested')):
            probe = self.catalog.state(str(revision))
            self.assertTrue(probe['unchanged'])
            self.assertNotIn('collections', probe)
        self.catalog.folder({'path':str(self.root), 'watch':False})
        self.assertEqual(self.catalog.state(revision)['folder']['path'], str(self.root.resolve()))
        self.assertTrue(self.catalog.state()['factories'])
        self.catalog.edit_product('1', {'factory':'Новая фабрика'})
        self.assertNotIn('unchanged', self.catalog.state(revision))

    def test_indexes_are_recreated_for_restored_legacy_database(self):
        with self.catalog.connection() as db:
            db.execute('DROP INDEX idx_read_search')
            self.catalog.ensure_read_indexes(db)
            names = {r[1] for r in db.execute('PRAGMA index_list(products)')}
        self.assertIn('idx_read_search', names)


if __name__ == '__main__':
    unittest.main()
