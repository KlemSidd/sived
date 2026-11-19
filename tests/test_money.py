import unittest

from app.money import accrual_cents, parse_usd, split_payout, usd


class MoneyTests(unittest.TestCase):
    def test_prices_are_shown_in_dollars(self):
        self.assertEqual(usd(1500), "$15")
        self.assertEqual(usd(1343), "$13.43")
        self.assertEqual(usd(74), "$0.74")
        self.assertEqual(usd(0), "$0")
        self.assertEqual(parse_usd("15"), 1500)
        self.assertEqual(parse_usd("$15.00"), 1500)
        self.assertEqual(parse_usd("15.5"), 1550)
        with self.assertRaises(ValueError):
            parse_usd("1500 cents")

    def test_posted_cents(self):
        self.assertEqual(accrual_cents(1500, 74, 830_000), 1343)
        self.assertEqual(accrual_cents(1500, 74, 3_100_000), 1116)

    def test_no_negative_run(self):
        self.assertEqual(accrual_cents(1500, 74, 20_000_000), 0)

    def test_two_job_withhold(self):
        transfer, withheld = split_payout(2686)
        self.assertEqual(transfer, 2448)
        self.assertEqual(withheld, 238)

    def test_split_leaves_at_most_one_cent(self):
        for balance in range(2501, 4000):
            transfer, withheld = split_payout(balance)
            self.assertGreater(transfer, 0)
            self.assertLessEqual(transfer + withheld, balance)
            self.assertLessEqual(balance - (transfer + withheld), 1)

    def test_below_minimum(self):
        self.assertEqual(split_payout(1343), (0, 0))
