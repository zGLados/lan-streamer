import sys
from pathlib import Path

# the plugin module sits in the folder above, as in lan-streamer's plugin process
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
