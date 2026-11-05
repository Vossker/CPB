import io
import pathlib
import unittest

suite = unittest.defaultTestLoader.discover("tests")
output = io.StringIO()
result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
text = output.getvalue()
print(text, end="")
pathlib.Path("test-results.txt").write_text(text, encoding="utf-8")
raise SystemExit(0 if result.wasSuccessful() else 1)
