"""python -m icomremote  ->  the connection test for now (the GUI arrives in step 2)."""

import sys

from .probe import main

if __name__ == "__main__":
    sys.exit(main())
