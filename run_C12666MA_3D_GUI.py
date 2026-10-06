"""Launch the C12666MA GUI from outside the repository directory."""

import sys
from pathlib import Path

REPOSITORY = Path(
    r"C:\Users\tolsm012\OneDrive - Wageningen University & Research\Internship JII\workdir"
)
sys.path.insert(0, str(REPOSITORY))

from c12666ma.gui import main


if __name__ == "__main__":
    main()
