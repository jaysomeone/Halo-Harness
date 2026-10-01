"""`python -m halo_harness ...` entry point -- delegates straight to cli.main()."""

import sys

from halo_harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
