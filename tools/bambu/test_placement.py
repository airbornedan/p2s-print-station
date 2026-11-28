import unittest

import numpy as np

import placement


def box(w, h):
    """Two triangles covering a w x h footprint at the origin."""
    return np.array([[[0, 0, 0], [w, 0, 0], [w, h, 1]], [[0, 0, 0], [w, h, 1], [0, h, 0]]], dtype=float)


class PickSpot(unittest.TestCase):
    def test_small_part_goes_to_the_next_quadrant(self):
        (cx, cy), nxt = placement.pick_spot(box(40, 40), 30, start=1)
        self.assertEqual((cx, cy, nxt), (192, 64, 2))

    def test_placed_part_clears_the_center(self):
        for start in range(4):
            spot, _ = placement.pick_spot(box(90, 60), 30, start)
            b = placement.bbox(placement.move(box(90, 60), *spot))
            self.assertFalse(placement._touches(b, 30), (start, b))
            self.assertTrue(placement.on_plate(b))

    def test_long_part_slides_along_an_edge(self):
        spot, _ = placement.pick_spot(box(200, 40), 30, 0)
        b = placement.bbox(placement.move(box(200, 40), *spot))
        self.assertFalse(placement._touches(b, 30))

    def test_part_too_big_to_avoid_the_center(self):
        spot, nxt = placement.pick_spot(box(170, 170), 30, 3)
        self.assertEqual((spot, nxt), (None, 3))


if __name__ == "__main__":
    unittest.main()
