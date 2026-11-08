import unittest
from plate_centre import centre_problem


class CentreTest(unittest.TestCase):
    def test_centred_part_refused(self):
        self.assertIsNotNone(centre_problem([75, 86, 192, 170], 256, 30))   # the oil lamp top, centred

    def test_side_part_ok(self):
        self.assertIsNone(centre_problem([5, 86, 90, 170], 256, 30))

    def test_corner_near_circle_ok(self):
        self.assertIsNone(centre_problem([0, 0, 106, 106], 256, 30))      # corner 31 mm from centre

    def test_huge_part_ok(self):
        self.assertIsNone(centre_problem([10, 10, 246, 246], 256, 30))

    def test_off(self):
        self.assertIsNone(centre_problem([100, 100, 150, 150], 256, 0))


if __name__ == "__main__":
    unittest.main()
