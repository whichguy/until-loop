# Stable unique strings

`stable_unique(values)` accepts an iterable of strings and returns a list with duplicate strings removed case-insensitively using Unicode `casefold()` equivalence.

Preserve the first occurrence's spelling and the original order of first occurrences. Support generators as input. Do not mutate the input collection. Reject any non-string item with `TypeError`; do not silently skip it or convert it to text. Empty input returns an empty list.

Run the local tests with `python3 -m unittest discover -p 'test_*.py' -v`.
