"""Install dependencies for the separate point-cloud processing worker."""
from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--target", str(root / "worker-deps"),
         "-r", str(root / "worker-requirements.txt")],
        check=True,
    )
    print("DELTA processing dependencies installed.")
