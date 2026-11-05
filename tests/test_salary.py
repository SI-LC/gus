"""A stated salary under your line lowers the score, in proportion. Anything else leaves it alone."""
import unittest

from radar import salary


class Reading(unittest.TestCase):
    def test_uses_the_top_of_a_range(self):
        self.assertEqual(salary.stated({"description": "Salary: £65,000 - 100,000 per year"}), 100_000)
        self.assertEqual(salary.stated({"description": "Base £70k to £85k plus equity"}), 85_000)

    def test_a_figure_from_the_source_comes_first(self):
        self.assertEqual(salary.stated({"salary_max": 90_000, "description": "Salary £50,000"}), 90_000)

    def test_ignores_money_that_isnt_a_salary(self):
        self.assertIsNone(salary.stated({"description": "We raised £800,000 last year"}))
        self.assertIsNone(salary.stated({"description": "Salary: £800 per day"}))
        self.assertIsNone(salary.stated({"description": "Competitive salary"}))
        self.assertIsNone(salary.stated({"description": ""}))


class Scoring(unittest.TestCase):
    CFG = {"salary_floor": 100_000, "salary_symbol": "£"}

    def test_the_drop_grows_with_the_gap(self):
        self.assertEqual([salary.drop(t, 100_000) for t in (100_000, 95_000, 85_000, 75_000, 20_000)], [0, 1, 2, 3, 5])

    def test_no_stated_salary_changes_nothing(self):
        self.assertEqual(salary.apply({"description": "Great role"}, 8, self.CFG), (8, ""))

    def test_off_when_no_line_is_set(self):
        self.assertEqual(salary.apply({"salary_max": 40_000}, 8, {"salary_floor": 0}), (8, ""))

    def test_explains_the_change_and_never_goes_below_1(self):
        score, note = salary.apply({"salary_max": 75_000}, 8, self.CFG)
        self.assertEqual(score, 5)
        self.assertIn("£75,000", note)
        self.assertEqual(salary.apply({"salary_max": 30_000}, 3, self.CFG)[0], 1)


if __name__ == "__main__":
    unittest.main()
