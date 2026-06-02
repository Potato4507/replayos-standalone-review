from __future__ import annotations

import unittest

from replayos.car_meta import car_name_from_body_id, car_profile


class CarMetaTests(unittest.TestCase):
    def test_car_name_from_body_id_uses_known_catalog(self) -> None:
        self.assertEqual(car_name_from_body_id(23), "Octane")
        self.assertEqual(car_name_from_body_id(4284), "Fennec")

    def test_car_profile_infers_visual_family(self) -> None:
        self.assertEqual(car_profile(car_body_id=403)["car_family"], "dominus")
        self.assertEqual(car_profile(car_name="Road Hog XL")["car_family"], "merc")
        self.assertEqual(car_profile(car_name="Batmobile")["car_family"], "plank")


if __name__ == "__main__":
    unittest.main()
