"""Shared test bootstrap.

Puts <repo>/src on sys.path so tests can `import wisp.*` without installing the
package. This is the pytest entry point; the unittest test modules also insert
the path themselves so `python -m unittest discover -s tests` works standalone.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
