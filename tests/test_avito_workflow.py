"""Review queues and bundle guards, using only disposable catalogs."""
import unittest

from backend.avito_workflow import ReviewIndex, exclusion
import test_avito as fixtures


class ReviewRules(unittest.TestCase):
    def test_bundles_are_excluded_without_confusing_individual_items(self):
        for title in ('Спальный гарнитур Ника', 'Спальня Ника', 'Прихожая со шкафом',
                      'Прихожая (комплект)', 'Детская Ника', 'Модульная кухня',
                      'Кухня под ключ', 'Комплект кровать и комод', 'Стенка Модерн'):
            with self.subTest(title=title):
                self.assertTrue(exclusion({'title': title}))
        for title in ('Детская кровать', 'Тумба для прихожей', 'Кухонный стол',
                      'Кровать без матраса', 'Комод из коллекции спальни Ника', 'Комплектующее для шкафа'):
            with self.subTest(title=title):
                self.assertFalse(exclusion({'title': title}))
        self.assertFalse(exclusion({'title': 'Кровать', 'description': 'Подберём кухню под ключ. Есть комплекты.'}))
        self.assertTrue(exclusion({'title': 'Кровать', 'description': 'Состав комплекта: кровать, шкаф, комод'}))

    def test_shared_collections_do_not_assign_factory(self):
        index = ReviewIndex([{'factory': 'Первая', 'collection': 'Лира'},
                             {'factory': 'Вторая', 'collection': 'Лира'},
                             {'factory': 'Первая', 'collection': 'Спальня Версаль-5'}])
        self.assertEqual(index.classify({'title': 'Комод Лира'})['queue'], 'unassigned')
        found = index.classify({'title': 'Кровать Версаль-5'})
        self.assertEqual(found['suggested_factory'], 'Первая')
        self.assertEqual(found['queue'], 'suggested')
        self.assertEqual(index.classify({'title': 'Кровать Версаль-6'})['queue'], 'unassigned')
        self.assertEqual(index.classify({'title': 'Кровать Первая Вторая'})['queue'], 'unassigned')

    def test_component_headings_and_case_do_not_pollute_collection_filters(self):
        index = ReviewIndex([{'factory': 'Первая', 'collection': 'Фасад'},
                             {'factory': 'Первая', 'collection': 'Лира'}])
        self.assertEqual(index.classify({'title': 'Комод', 'description': 'Фасад и ручки'})['queue'], 'unassigned')
        result = index.classify({'title': 'Комод', 'description': 'Коллекция «Лира». Коллекция «ЛИРА».'})
        self.assertEqual(result['collections'], ['лира'])


class WorkflowAPI(unittest.TestCase):
    # Reuse only the disposable catalog helpers.
    setUp = fixtures.LocalAvito.setUp
    tearDown = fixtures.LocalAvito.tearDown
    add_product = fixtures.LocalAvito.add_product
    listing = fixtures.LocalAvito.listing
    decision_payload = fixtures.LocalAvito.decision_payload
    confirm = fixtures.LocalAvito.confirm
    def test_factory_queue_separates_unknown_other_and_bundles(self):
        self.avito.setup()
        a = self.listing()
        self.listing(avito_id='2', factory='Другая фабрика')
        unknown = self.listing(avito_id='3', attributes={})
        bundle = self.listing(avito_id='4', title='Спальный гарнитур Лира')
        result = self.avito.listings({'factory': 'Регион 058', 'queue': 'suggested'})
        self.assertEqual([i['id'] for i in result['items']], [a['id']])
        self.assertEqual(result['counts'], {'suggested': 1, 'unassigned': 1, 'linked': 0, 'excluded': 1})
        self.assertEqual(self.avito.listings({'factory': 'Регион 058', 'queue': 'unassigned'})['items'][0]['id'], unknown['id'])
        self.assertEqual(self.avito.listings({'factory': 'Регион 058', 'queue': 'excluded'})['items'][0]['id'], bundle['id'])
        self.assertEqual(self.avito.listings({'factory': 'Регион 058', 'queue': 'suggested', 'type': 'Комод'})['total'], 0)

    def test_bundle_cannot_confirm_and_preexisting_link_is_not_planned(self):
        self.avito.setup()
        listing = self.listing()
        self.confirm(listing, self.region)
        listing = self.listing(title='Прихожая (комплект)')
        detail = self.avito.detail(listing['id'], {'factory': 'Регион 058', 'q': 'Тумба'})
        self.assertEqual(detail['category'], 'excluded')
        self.assertEqual(detail['candidates'], [])
        with self.assertRaises(Exception) as caught:
            self.confirm(listing, self.region)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.avito.prepare({'factory': 'Регион 058'})['items'], [])

    def test_manual_exclusion_history_restore_and_restart(self):
        self.avito.setup()
        listing = self.listing()
        self.confirm(listing, self.region)
        self.avito.decide(self.decision_payload(listing, self.region, 'exclude'))
        self.assertEqual(self.avito.state()['links'], 0)
        from backend.avito import Avito
        restarted = Avito(self.catalog, self.avito.error, self.avito.now)
        self.assertEqual(restarted.detail(listing['id'], {})['category'], 'excluded')
        self.assertEqual(restarted.listings({'queue': 'excluded'})['total'], 1)
        restarted.decide(self.decision_payload(listing, self.region, 'restore'))
        detail = restarted.detail(listing['id'], {})
        self.assertFalse(detail['excluded_reason'])
        self.assertEqual(len(detail['history']), 3)
        self.assertIsNone(detail['confirmed'])

    def test_description_model_and_manual_code_search_select_correct_dresser(self):
        self.avito.setup()
        self.catalog.edit_product(self.region['id'], {'name': 'Комод К-3.6 МДФ', 'collection': 'Комоды'})
        self.catalog.edit_product(self.other['id'], {'name': 'Комод К-6.3 МДФ', 'collection': 'Комоды'})
        listing = self.listing(title='Комод белый', description='Новый комод К-3.6 от фабрики Регион 058!', attributes={})
        detail = self.avito.detail(listing['id'], {})
        self.assertEqual([c['product']['id'] for c in detail['candidates']], [self.region['id']])
        self.assertEqual(detail['excluded']['model'], 1)
        self.assertEqual(self.avito.listings({'q': 'к3.6', 'queue': 'all'})['total'], 1)
        self.assertEqual(len(self.avito.detail(listing['id'], {'q': 'к3.6'})['candidates']), 1)

    def test_dimension_discrepancy_requires_explicit_review_and_preserves_it(self):
        self.avito.setup()
        self.catalog.edit_product(self.region['id'], {'name': 'Кровать Василиса NEW 1,4 Серый', 'collection': '', 'dimensions': 'Ш: 1450; В: 910; Г: 2050', 'color': 'Серый'})
        product = self.catalog.product(self.region['id'])
        listing = self.listing(title='Кровать двуспальная 140×200', description='Новая кровать Василиса от фабрики Регион 058! Габариты: Ширина: 1440 мм; Высота: 1125 мм; Глубина: 2060 мм. Цвет: Серый', attributes={'factory': 'Регион 058', 'color': 'Серый'})
        detail = self.avito.detail(listing['id'], {'factory': 'Регион 058', 'dimension_unit': 'mm'})
        check = detail['candidates'][0]['check']
        self.assertFalse(check['conflicts'])
        payload = self.decision_payload(listing, product)
        payload['dimension_unit'] = 'mm'
        with self.assertRaises(fixtures.ApiError):
            self.avito.decide(payload)
        payload['acknowledged_warnings'] = check['warnings']
        payload['reason'] = 'Вручную проверено: габариты объявления ошибочны, тот же товар'
        self.avito.decide(payload)
        detail = self.avito.detail(listing['id'], {})
        self.assertEqual(detail['dimension_unit'], 'mm')
        self.assertFalse(detail['confirmed']['check']['conflicts'])
        plan = self.avito.prepare({'factory': 'Регион 058'})
        self.assertNotEqual(plan['items'][0]['state'], 'conflict')
        self.assertTrue(any('215' in reason for reason in plan['items'][0]['reasons']))
        # Acknowledging sizes cannot bypass a different manufacturer.
        payload = self.decision_payload(listing, self.other)
        payload['acknowledged_warnings'] = check['warnings']
        with self.assertRaises(fixtures.ApiError):
            self.avito.decide(payload)
        self.catalog.edit_product(product['id'], {'dimensions': 'Ш: 1600; В: 910; Г: 2050'})
        self.assertEqual(self.avito.prepare({'factory': 'Регион 058'})['items'][0]['state'], 'conflict')

    def test_composite_color_can_be_confirmed_only_after_warning_review(self):
        self.avito.setup()
        self.catalog.edit_product(self.region['id'], {'color': 'Графит серый/ МДФ Графит Меланж / МДФ Меланж'})
        product = self.catalog.product(self.region['id'])
        listing = self.listing(attributes={'factory': 'Регион 058', 'color': 'Меланж'})
        detail = self.avito.detail(listing['id'], {'factory': 'Регион 058'})
        check = next(c['check'] for c in detail['candidates'] if c['product']['id'] == product['id'])
        self.assertFalse(check['conflicts'])
        payload = self.decision_payload(listing, product)
        with self.assertRaises(fixtures.ApiError):
            self.avito.decide(payload)
        payload['acknowledged_warnings'] = check['warnings']
        payload['reason'] = 'Вручную проверен декор Меланж среди вариантов каталога'
        self.avito.decide(payload)
        self.assertFalse(self.avito.detail(listing['id'], {})['confirmed']['check']['conflicts'])


