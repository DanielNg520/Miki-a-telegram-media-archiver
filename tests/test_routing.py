import unittest

from miki_sorter_bot.routing import Route, choose_route, extract_terms, route_candidates


ROUTES = [
    Route(name="First", thread_id=10, keywords=["CR", "GONA"]),
    Route(name="Second", thread_id=20, keywords=["FC"]),
]


class RoutingTests(unittest.TestCase):
    def test_extract_terms_normalizes_and_preserves_hyphenated_tokens(self) -> None:
        self.assertEqual(
            extract_terms("New CR item, fc-2 and 日本"),
            {"new", "cr", "item", "fc-2", "and", "日本"},
        )

    def test_candidates_are_exact_tokens_and_limited_to_configured_keywords(self) -> None:
        self.assertEqual(
            route_candidates("CR and FC are present; CROWN is not CR.", ROUTES),
            {"cr", "fc"},
        )
        self.assertEqual(route_candidates("CROWN only", ROUTES), set())

    def test_choose_route_uses_configuration_order(self) -> None:
        decision = choose_route({"fc", "gona"}, ROUTES)
        self.assertIsNotNone(decision)
        self.assertEqual(
            (decision.route_name, decision.thread_id, decision.reason),
            ("First", 10, "database:gona"),
        )

    def test_choose_route_returns_none_without_confirmed_match(self) -> None:
        self.assertIsNone(choose_route(set(), ROUTES))


if __name__ == "__main__":
    unittest.main()
