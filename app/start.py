"""Iridium Tracker entry point. On the first run the tracker opens in "click your location" mode."""
import os
import sys

# the orbit fits are small; one maths thread avoids OpenBLAS reserving a buffer for every CPU core
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iridium_tracker  # noqa: E402

if __name__ == '__main__':
    iridium_tracker.main()
