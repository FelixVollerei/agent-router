import json
from pathlib import Path

actual = json.loads(Path("variables.json").read_text(encoding="utf-8"))
expected = {f"NEW_{i:03d}": i for i in range(100)}
assert actual == expected, "Expected exactly NEW_000..NEW_099 with unchanged integer values"
print("PASS: all 100 variable names and values match")
