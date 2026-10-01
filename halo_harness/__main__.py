"""`python -m rolo_claude ...` entry point -- delegates straight to cli.main()."""

import sys

from rolo_claude.cli import main

if __name__ == "__main__":
    sys.exit(main())
