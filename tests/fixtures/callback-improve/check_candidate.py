"""Independent behavioral oracle; keep outside the model's fixture workspace."""
import importlib.util
import json
import sys
from pathlib import Path


def verify(repo):
    spec = importlib.util.spec_from_file_location("fixture_candidate", Path(repo) / "stable_unique.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = module.stable_unique
    failures = []
    cases = [
        ("empty", [], []),
        ("stable order", ["Z", "a", "z", "B", "A"], ["Z", "a", "B"]),
        ("first spelling", ["Alpha", "ALPHA", "beta", "Beta"], ["Alpha", "beta"]),
        ("Unicode casefold", ["ß", "SS", "ss", "X"], ["ß", "X"]),
        ("empty string", ["", "A", "a", ""], ["", "A"]),
        ("generator", iter(["b", "A", "B", "a"]), ["b", "A"]),
    ]
    for name, values, expected in cases:
        try:
            actual = function(values)
            if actual != expected:
                failures.append({"case": name, "expected": expected, "actual": actual})
        except Exception as error:
            failures.append({"case": name, "error": type(error).__name__ + ": " + str(error)})
    for value in [None, 7, True, [], {}]:
        for label, values in [("mixed", ["ok", value]), ("singleton", [value])]:
            name = "reject " + label + " non-string " + repr(value)
            try:
                function(values)
                failures.append({"case": name, "error": "no TypeError"})
            except TypeError:
                pass
            except Exception as error:
                failures.append({"case": name, "error": type(error).__name__})
    values = ["b", "A", "b"]
    copy = values[:]
    try:
        function(values)
        if values != copy:
            failures.append({"case": "input preservation", "actual": values})
    except Exception as error:
        failures.append({"case": "input preservation", "error": type(error).__name__})
    return {"checks": len(cases) + 11, "failures": failures, "passed": not failures}


if __name__ == "__main__":
    result = verify(sys.argv[1])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
