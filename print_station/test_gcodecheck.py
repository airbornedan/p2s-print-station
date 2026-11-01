import os, sys, tempfile, unittest, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "bambu"))
import gcodecheck

FULL = "; EXECUTABLE_BLOCK_START\nM109 S220\n  M620 S0A\nG29\n; MACHINE_START_GCODE_END\nG1 X1\n"
GENERIC = "; EXECUTABLE_BLOCK_START\nG28\nG29 ;Home\nM109 S205;\n; MACHINE_START_GCODE_END\nM620 S0A\n"


def sliced(gcode):
    f = tempfile.NamedTemporaryFile(suffix=".gcode.3mf", delete=False)
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/plate_1.gcode", gcode)
    return f.name


class StartBlock(unittest.TestCase):
    def test_full(self):
        self.assertEqual(gcodecheck.start_problems(sliced(FULL)), [])

    def test_generic_has_no_ams_load(self):
        # An AMS load after the start block doesn't count.
        self.assertIn("M620 S", gcodecheck.start_problems(sliced(GENERIC))[0])

    def test_no_start_block(self):
        self.assertTrue(gcodecheck.start_problems(sliced("G1 X1\n")))

    def test_not_a_slice(self):
        self.assertTrue(gcodecheck.start_problems(__file__))


if __name__ == "__main__":
    unittest.main()
