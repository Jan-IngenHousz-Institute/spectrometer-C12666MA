"""Launch the C12666MA GUI from outside the repository directory.

    python path\\to\\spectrometer-C12666MA\\run_C12666MA_3D_GUI.py
"""

import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent
sys.path.insert(0, str(REPOSITORY))

from c12666ma.gui import main


if __name__ == "__main__":
    main()
