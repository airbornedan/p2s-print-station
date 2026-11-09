import json, os, sys, tempfile, unittest, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "bambu"))
import slicecheck

CANTILEVER = "It seems object x.stl has floating cantilever. Please re-orient the object or enable support generation."


def result(**kw):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(kw, f)
    f.close()
    return f.name


def threemf(files):
    f = tempfile.NamedTemporaryFile(suffix=".gcode.3mf", delete=False)
    with zipfile.ZipFile(f, "w") as z:
        for n, data in files.items():
            z.writestr(n, data)
    return f.name


class ReadResult(unittest.TestCase):
    def test_warning(self):
        r = result(return_code=0, sliced_plates=[{"warning_message": CANTILEVER}])
        self.assertEqual(slicecheck.read_result(r), ([CANTILEVER], None))

    def test_clean(self):
        self.assertEqual(slicecheck.read_result(result(return_code=0, sliced_plates=[{"warning_message": ""}])), ([], None))

    def test_slicer_error(self):
        warns, err = slicecheck.read_result(result(return_code=-6, error_string="can not be parsed"))
        self.assertEqual((warns, err), ([], "can not be parsed"))

    def test_missing(self):
        self.assertTrue(slicecheck.read_result("/nonexistent/result.json")[1])


class HasModel(unittest.TestCase):
    def test_object_file(self):
        self.assertTrue(slicecheck.has_model(threemf({"3D/Objects/object_1.model": "<mesh/>"})))

    def test_inline_mesh(self):
        self.assertTrue(slicecheck.has_model(threemf({"3D/3dmodel.model": "<model><mesh></mesh></model>"})))

    def test_gcode_only(self):
        f = threemf({"3D/3dmodel.model": "<model/>", "Metadata/plate_1.gcode": "G1"})
        self.assertFalse(slicecheck.has_model(f))
        self.assertEqual(slicecheck.slicer_warnings(f, "/bin/false")[0], [])


if __name__ == "__main__":
    unittest.main()
