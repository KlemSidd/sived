import unittest

from app.estimate import estimate_prompt, estimate_words, gate, micros_to_cents, stripe_fee_cents


class EstimateTests(unittest.TestCase):
    def test_rounded_sheet(self):
        normal = estimate_prompt(126_000)
        long = estimate_prompt(246_000)
        self.assertEqual(normal["cost_usd_micros"], 828_000)
        self.assertEqual(long["cost_usd_micros"], 3_096_000)
        self.assertEqual(normal["token_cents"], 83)
        self.assertEqual(long["token_cents"], 310)

    def test_unrounded_words(self):
        normal = estimate_words(90_000)
        long = estimate_words(180_000)
        self.assertEqual(normal["prompt_base"], 125_700)
        self.assertEqual(long["prompt_base"], 245_400)
        self.assertEqual(normal["cost_usd_micros"], 826_200)
        self.assertEqual(long["cost_usd_micros"], 3_088_800)
        self.assertEqual(micros_to_cents(long["cost_usd_micros"]), 309)

    def test_four_dollar_gate_uses_the_fee_on_four_dollars(self):
        long = estimate_words(180_000)
        decision = gate(400, long["token_cents"])
        self.assertEqual(decision["stripe_fee_cents"], 42)
        self.assertEqual(stripe_fee_cents(1500), 74)
        self.assertLess(decision["stripe_fee_cents"] + decision["token_cents"], 400)
        self.assertTrue(decision["ok"])

    def test_fifteen_dollar_accrual_inputs(self):
        normal = estimate_prompt(126_000)
        decision = gate(1500, normal["token_cents"])
        self.assertEqual(decision["editor_net_cents"], 1343)
        self.assertTrue(decision["ok"])
