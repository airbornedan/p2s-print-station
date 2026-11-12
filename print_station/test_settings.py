import os, tempfile, tomllib, unittest
import settings

CONF = '''# Private site settings
printer_ip = "192.0.2.10"
stall_minutes = 20   # minutes

# Stringing fix
[filament_overrides."PLA"]
nozzle_temperature = 210
'''


class Test(unittest.TestCase):
    def test_replace_and_insert_before_table(self):
        out = settings.apply_text(CONF, {"stall_minutes": 30, "delete_timelapse": False})
        d = tomllib.loads(out)
        self.assertEqual((d["stall_minutes"], d["delete_timelapse"]), (30, False))
        self.assertEqual(d["filament_overrides"]["PLA"]["nozzle_temperature"], 210)
        self.assertNotIn("delete_timelapse", d["filament_overrides"]["PLA"])
        self.assertIn("# Private site settings", out)
        self.assertEqual(out.count("stall_minutes"), 1)
        self.assertIn("# Stringing fix\n[filament", out)

    def test_empty_file(self):
        self.assertEqual(tomllib.loads(settings.apply_text("", {"phone_alerts": True})), {"phone_alerts": True})

    def test_validate(self):
        self.assertEqual(settings.validate({"stall_minutes": "30", "phone_alerts": False}),
                         {"stall_minutes": 30, "phone_alerts": False})
        for bad in ({"stall_minutes": 1}, {"stall_minutes": "x"}, {"phone_alerts": "yes"}, {"printer_ip": "1.2.3.4"}):
            with self.assertRaises(ValueError):
                settings.validate(bad)

    def test_save_keeps_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            open(p, "w").write(CONF); os.chmod(p, 0o600)
            settings.save(p, {"stick_max_age_days": 7})
            self.assertEqual(tomllib.load(open(p, "rb"))["stick_max_age_days"], 7)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
