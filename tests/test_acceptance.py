import unittest

from scenic_reservation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["shared_ledger_full"])
        self.assertTrue(result["overflow_rejected"])
        self.assertTrue(result["individual_confirmed_balance_unchanged"])
        self.assertTrue(result["expired_after_restart"])
        self.assertTrue(result["freed_quota_resold"])
        self.assertTrue(result["rescheduled_to_afternoon"])
        self.assertTrue(result["original_marked_rescheduled"])
        self.assertTrue(result["morning_final_after_cancel"])
        self.assertTrue(result["ledger_balances_monotonic_safe"])
        self.assertIn("expired", result["ledger_reasons"])
        self.assertIn("reschedule_out", result["ledger_reasons"])
        self.assertEqual(["reserved", "confirmed", "reschedule_out"],
                         result["sample_item_history"])


if __name__ == "__main__":
    unittest.main()
