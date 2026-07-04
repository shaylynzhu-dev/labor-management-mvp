import unittest

from app.services.contract_safety_service import filter_valid_contracts, valid_contract_id


class EmergencyStabilizationTest(unittest.TestCase):
    def test_invalid_contract_ids_never_enter_ui_context(self):
        rows = [
            {"contract_id": None}, {"contract_id": ""}, {"contract_id": " null "},
            {"contract_id": "C-VALID"}, {},
        ]
        self.assertEqual(filter_valid_contracts(rows), [{"contract_id": "C-VALID"}])
        self.assertFalse(valid_contract_id(None))
        self.assertTrue(valid_contract_id("C-VALID"))


if __name__ == "__main__":
    unittest.main()
