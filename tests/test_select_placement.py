import json
import shutil
import subprocess
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "demos" / "static" / "select.js"


def place(field, list_height, viewport_height):
    script = (
        f"import {{ place }} from {json.dumps(MODULE.as_uri())};"
        f"console.log(JSON.stringify(place({json.dumps(field)}, {list_height}, {viewport_height})));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "needs node")
class SelectPlacementTests(unittest.TestCase):
    def test_opens_directly_below_the_field(self):
        self.assertEqual(place({"top": 100, "bottom": 134}, 80, 800), {"top": 138, "maxHeight": 80})

    def test_flips_above_when_only_the_space_above_fits(self):
        self.assertEqual(place({"top": 700, "bottom": 734}, 200, 800), {"top": 496, "maxHeight": 200})

    def test_stays_below_when_it_fits_exactly(self):
        placed = place({"top": 100, "bottom": 134}, 654, 800)
        self.assertEqual(placed, {"top": 138, "maxHeight": 654})

    def test_scrolls_on_the_roomier_side_when_neither_fits(self):
        self.assertEqual(place({"top": 100, "bottom": 134}, 900, 800), {"top": 138, "maxHeight": 654})
        placed = place({"top": 600, "bottom": 634}, 900, 800)
        self.assertEqual(placed, {"top": 8, "maxHeight": 588})


if __name__ == "__main__":
    unittest.main()
