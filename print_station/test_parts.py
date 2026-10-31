import json, os, tempfile, unittest, zipfile

from parts import plate_number, read_parts


def _plate(path, gcode_head, info="", layout=None, plate=1):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/plate_%d.gcode" % plate, gcode_head + "\nG1 X0\n" * 100)
        if info:
            z.writestr("Metadata/slice_info.config", info)
        if layout is not None:
            z.writestr("Metadata/plate_%d.json" % plate, json.dumps(layout))


class PartsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "job.gcode.3mf")

    def tearDown(self):
        self.dir.cleanup()

    def test_gui_slice_uses_identify_id_names(self):
        info = ('<metadata key="label_object_enabled" value="true"/>'
                '<object identify_id="174" name="Blade &amp; tip" skipped="false" />'
                '<object identify_id="185" name="Guard.stl" skipped="false" />')
        layout = {"bbox_objects": [{"id": 308, "name": "x", "bbox": [1, 2, 3, 4]},
                                   {"id": 309, "name": "y", "bbox": [5, 6, 7, 8]}]}
        _plate(self.path, "; HEADER\n; model label id: 174,185\n", info, layout)
        self.assertEqual(read_parts(self.path), [
            {"id": 174, "name": "Blade & tip", "bbox": [1, 2, 3, 4]},
            {"id": 185, "name": "Guard.stl", "bbox": [5, 6, 7, 8]}])

    def test_cli_slice_takes_names_from_layout_in_order(self):
        layout = {"bbox_objects": [{"id": 71, "name": "guard_0.stl", "bbox": [10, 10, 20, 20]},
                                   {"id": 72, "name": "guard_1.stl", "bbox": [30, 30, 40, 40]}]}
        _plate(self.path, "; model label id: 47,58\n", '<metadata key="label_object_enabled" value="true"/>', layout)
        parts = read_parts(self.path)
        self.assertEqual([(p["id"], p["name"]) for p in parts], [(47, "guard_0.stl"), (58, "guard_1.stl")])

    def test_mismatched_layout_is_not_paired(self):
        layout = {"bbox_objects": [{"id": 71, "name": "only.stl", "bbox": [10, 10, 20, 20]}]}
        _plate(self.path, "; model label id: 47,58\n", "", layout)
        self.assertEqual(read_parts(self.path), [{"id": 47, "name": "Part 1", "bbox": None},
                                                 {"id": 58, "name": "Part 2", "bbox": None}])

    def test_unlabelled_or_unsliced(self):
        _plate(self.path, "; model label id: 1,2\n", '<metadata key="label_object_enabled" value="false"/>')
        self.assertEqual(read_parts(self.path), [])
        _plate(self.path, "; no labels here\n")
        self.assertEqual(read_parts(self.path), [])
        self.assertIsNone(read_parts(self.path, plate=2))

    def test_plate_number(self):
        self.assertEqual(plate_number("/data/Metadata/plate_3.gcode"), 3)
        self.assertEqual(plate_number(None), 1)


if __name__ == "__main__":
    unittest.main()
