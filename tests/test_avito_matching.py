"""Regression cases for collection-led search and furniture types."""
import unittest

from backend.avito_matching import (collection_evidence, collection_in, evaluate,
                                   furniture_type, match_context, search_matches, compatibility)


class MatchingEvidence(unittest.TestCase):
    def product(self, name="НКМ Версаль-5 секция №3 Кровать 1600", collection="Спальня ВЕРСАЛЬ-5"):
        return {"name": name, "collection": collection, "factory": "Росток", "color": "Белый"}

    def listing(self, description='Новая коллекция &#34;Версаль-5&#34; от фабрики Росток!', title="Кровать двуспальная 160×200"):
        return {"title": title, "description": description, "attributes": {}}

    def test_html_encoded_collection_matches_catalog_prefix(self):
        check = evaluate(self.listing(), self.product())
        self.assertTrue(check["collection_match"])
        self.assertFalse(check["collection_mismatch"])
        self.assertIn("Версаль-5", " ".join(check["reasons"]))

    def test_unquoted_collection_is_recognized(self):
        listing = self.listing("<p>Новая коллекция Роза от фабрики ДСВ-мебель!</p>")
        self.assertEqual(collection_evidence(listing)[0]["name"], "Роза")
        self.assertTrue(evaluate(listing, self.product("МС Роза Кровать 1600", "МС Роза"))["collection_match"])

    def test_collection_only_in_product_name_is_used(self):
        self.assertTrue(evaluate(self.listing(), self.product(collection="МОДУЛЬНЫЕ СИСТЕМЫ"))["collection_match"])

    def test_wrong_collection_is_conflict_even_when_other_fields_match(self):
        listing = self.listing()
        listing["attributes"] = {"factory": "Росток", "color": "Белый"}
        check = evaluate(listing, self.product("Кровать 160×200", "НКМ Амалия"))
        self.assertTrue(check["collection_mismatch"])
        self.assertEqual(check["category"], "conflict")

    def test_dresser_in_right_collection_is_wrong_type(self):
        check = evaluate(self.listing(), self.product("НКМ Версаль-5 Комод 160×200"))
        self.assertTrue(check["type_mismatch"])
        self.assertEqual(check["category"], "conflict")

    def test_accessories_and_secondary_nouns_do_not_replace_subject(self):
        for title, kind in (("Кровать без матраса", "bed"), ("Прихожая со шкафом", "hallway"),
                            ("Тумба прикроватная для спальни", "bedside"), ("Тумба под ТВ", "tv_stand"),
                            ("Стол журнальный в спальню", "coffee_table"), ("Фасад для шкафа", "part")):
            with self.subTest(title=title):
                self.assertEqual(furniture_type(title)["kind"], kind)

    def test_group_category_does_not_imply_bed(self):
        listing = self.listing(title="Модуль 1000", description="")
        listing["source_details"] = {"characteristics": {"Вид товара": "Кровати, диваны и кресла"}}
        self.assertIsNone(match_context(listing)["item_type"])

    def test_generic_collection_phrases_do_not_become_names(self):
        for description in ("Новая коллекция от фабрики NN-мебель!", "Коллекция кроватей от фабрики Пирамида!",
                            "Наши коллекции под разные планировки, сочетать декоры удобно."):
            with self.subTest(description=description):
                self.assertEqual(collection_evidence(self.listing(description)), [])

    def test_dictionary_fallback_ignores_color_palettes_and_category_headings(self):
        listing = self.listing("Белый фасад. Модульные системы из коллекций на любой вкус. Мебель Лира.", title="Кровать")
        result = collection_evidence(listing, ["Белый", "МОДУЛЬНЫЕ СИСТЕМЫ", "Лира", "Белый, Серый"])
        self.assertEqual([c["name"] for c in result], ["лира"])

    def test_numbered_collections_are_not_merged(self):
        self.assertTrue(collection_in("МСП 1", "МСП-1"))
        self.assertFalse(collection_in("Спальня Версаль-5", "Версаль"))
        self.assertFalse(collection_in("Кровать Версаль-5", "Версаль-6"))

    def test_multiple_labelled_collections_require_review(self):
        listing = self.listing('Коллекция «Версаль-5» от фабрики. Коллекция «Амалия» от фабрики.')
        self.assertIn("разные коллекции", " ".join(evaluate(listing, self.product())["conflicts"]))

    def test_model_in_description_is_not_lost_in_generic_collection(self):
        listing = self.listing('Новый комод К-3.6 от фабрики «Росток»!', title='Комод белый')
        self.assertEqual(match_context(listing)['model']['name'], 'К-3.6')
        self.assertTrue(evaluate(listing, self.product('Комод К-3.6 МДФ', 'Комоды'))['model_match'])
        self.assertTrue(evaluate(listing, self.product('Комод К-6.3', 'Комоды'))['model_mismatch'])

    def test_model_search_handles_punctuation_without_swapping_codes(self):
        self.assertTrue(search_matches('Комод К-3.6 МДФ', 'к3.6'))
        self.assertTrue(search_matches('Комод К-3.6 МДФ', 'комод к 3.6'))
        self.assertFalse(search_matches('Комод К-6.3 МДФ', 'К3.6'))
        self.assertFalse(search_matches('Комод К-3.60 МДФ', 'К3.6'))
        self.assertFalse(search_matches('Кровать 1900', '900'))

    def test_manual_search_short_designations_are_whole_words(self):
        for query, matching, unrelated in [
                ('кр', 'КР-01 Кровать', 'Тумба прикроватная'),
                ('кп', 'Кровать КП-554', 'КПТ 215'),
                ('шк', 'ШК-5 белый', 'Шкаф белый'),
                ('мсп', 'МСП-1 Модуль', 'МСПП-1 Модуль')]:
            with self.subTest(query=query):
                self.assertTrue(search_matches(matching, query.upper()))
                self.assertFalse(search_matches(unrelated, query))
        self.assertTrue(search_matches('КР-01 Кровать Лира', 'кр лира'))
        self.assertFalse(search_matches('Кровать Лира', 'кр лира'))
        self.assertTrue(search_matches('Тумба прикроватная', 'кроват'))

    def test_sleep_width_ranks_correct_bed_without_guessing_unlabelled_units(self):
        listing = self.listing('Новая кровать Калипсо от фабрики Росток. Спальное место — 90 × 200 см', title='Кровать детская')
        small = evaluate(listing, self.product('Кровать Калипсо 900', 'Калипсо'))
        large = evaluate(listing, self.product('Кровать Калипсо 1600', 'Калипсо'))
        self.assertEqual(match_context(listing)['sleep_width_mm'], 900)
        self.assertGreater(small['rank'], large['rank'])
        listing['description'] = 'Спальное место 90×200'
        self.assertIsNone(match_context(listing)['sleep_width_mm'])

    def test_color_grammar_does_not_block_same_color_but_shades_stay_distinct(self):
        listing = self.listing(description='', title='Комод')
        listing['attributes'] = {'color': 'Белая'}
        self.assertFalse(evaluate(listing, self.product('Комод'))['conflicts'])
        product = self.product('Комод'); product['color'] = 'Белый жемчуг'
        self.assertFalse(evaluate(listing, product)['conflicts'])
        self.assertTrue(evaluate(listing, product)['warnings'])
        listing['attributes']['color'] = 'Чёрный/белый'; product['color'] = 'Черный / Белый'
        self.assertFalse(evaluate(listing, product)['conflicts'])

    def test_vasilisa_outer_dimensions_and_new_are_reviewable_warnings(self):
        listing = {'title': 'Кровать двуспальная 140×200', 'description': 'Новая кровать Василиса от фабрики Алёнушка! Габариты: Ширина — 1440 мм\nВысота — 1125 мм\nГлубина — 2060 мм\nЦвет: Серый. Спальное место 140×200 см',
                   'attributes': {'factory': 'Алёнушка', 'color': 'Серый'}, '_dimension_unit': 'mm'}
        product = {'name': 'Кровать Василиса NEW 1,4 Серый', 'factory': 'Алёнушка', 'color': 'Серый', 'dimensions': 'Ш: 1450; В: 910; Г: 2050'}
        check = evaluate(listing, product)
        self.assertFalse(check['conflicts'])
        self.assertEqual([r['difference_mm'] for r in check['dimension_check']['rows']], [10, 215, 10])
        self.assertEqual(len(check['warnings']), 4)
        self.assertTrue(any('NEW' in w for w in check['warnings']))
        snapshot = {**product, '_dimension_unit': 'mm', '_reviewed_warnings': check['warnings']}
        self.assertFalse(compatibility(listing, product, snapshot)['conflicts'])
        self.assertTrue(compatibility(listing, product, product)['conflicts'])
        listing['_dimension_unit'] = ''
        unknown = evaluate(listing, product)
        self.assertTrue(all(r['difference_mm'] is None for r in unknown['dimension_check']['rows']))
        self.assertTrue(any('единицы' in w for w in unknown['warnings']))

    def test_size_units_axes_and_sleeping_place_are_not_guessed(self):
        listing = self.listing('Габариты: Ширина: 145 см; Высота: 91 см; Глубина: 205 см. Цвет: белый. Спальное место 140×200 см', title='Кровать')
        product = self.product('Кровать', '')
        product['dimensions'] = 'Г: 2050 мм; Ш: 1450 мм; В: 910 мм'
        check = evaluate(listing, product)
        self.assertFalse(check['conflicts'])
        self.assertEqual([r['difference_mm'] for r in check['dimension_check']['rows']], [0, 0, 0])
        listing['description'] = 'Спальное место 140×200 см'
        self.assertTrue(all(r['listing_mm'] is None for r in evaluate(listing, product)['dimension_check']['rows']))

    def test_sentence_punctuation_does_not_turn_millimetres_into_metres(self):
        listing = self.listing('Габариты: Ширина — 1440 мм, Высота — 1125 мм, Глубина — 2060 мм. Цвет: белый', title='Кровать')
        product = self.product('Кровать', '')
        product['dimensions'] = 'Ш: 1440 мм; В: 1125 мм; Г: 2060 мм.'
        check = evaluate(listing, product)
        self.assertEqual([r['listing_mm'] for r in check['dimension_check']['rows']], [1440, 1125, 2060])
        self.assertEqual([r['difference_mm'] for r in check['dimension_check']['rows']], [0, 0, 0])

    def test_decor_in_composite_color_requires_review_instead_of_blocking(self):
        listing = self.listing(description='', title='Комод')
        listing['attributes'] = {'color': 'Меланж'}
        product = self.product('Комод', '')
        product['color'] = 'Графит серый/ МДФ Графит Меланж / МДФ Меланж'
        check = evaluate(listing, product)
        self.assertFalse(check['conflicts'])
        self.assertEqual(len(check['warnings']), 1)
        self.assertTrue(any('составном' in w for w in check['warnings']))
        self.assertNotIn('Цвет: совпадает явное значение', check['reasons'])
        self.assertTrue(compatibility(listing, product, product)['conflicts'])

    def test_different_colors_require_review_and_are_not_declared_equal(self):
        listing = self.listing(description='', title='Комод')
        product = self.product('Комод', '')
        for external, internal in [('Меланж', 'Графит серый'), ('Серый', 'Графит серый'),
                                   ('Белый', 'Белый жемчуг'), ('Белый / Чёрный', 'Белый / Серый')]:
            with self.subTest(external=external, internal=internal):
                listing['attributes'] = {'color': external}; product['color'] = internal
                check = evaluate(listing, product)
                self.assertFalse(check['conflicts'])
                self.assertTrue(check['warnings'])
                self.assertNotIn('Цвет: совпадает явное значение', check['reasons'])
