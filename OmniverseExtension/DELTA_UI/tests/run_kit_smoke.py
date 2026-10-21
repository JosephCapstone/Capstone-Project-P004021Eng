from pathlib import Path
import os, subprocess, json, sys

root = Path(__file__).resolve().parents[1]
script = Path(sys.argv[1]).name if len(sys.argv) > 1 else "kit_smoke.py"
result_path = root / "tests" / (Path(script).stem + "_result.json")
result_path.unlink(missing_ok=True)
isaac = Path(
    os.environ.get(
        "ISAAC_SIM_ROOT",
        str(Path.home() / "Downloads/isaac-sim-standalone-6.1.0-windows-x86_64"),
    )
)
args = [
    str(isaac / "kit/kit.exe"),
    str(root / "delta.robot.app.kit"),
    "--no-window",
    "--/app/window/width=1600",
    "--/app/window/height=1000",
]
for name in ("apps", "exts", "extscache", "extsUser", "extsDeprecated"):
    args += ["--ext-folder", str(isaac / name)]
args += [
    "--ext-folder",
    str(root / "exts"),
    "--exec",
    str(root / "tests" / script),
    "--/app/extensions/registryEnabled=false",
]
log = root / "tests" / (Path(script).stem + ".log")
with log.open("w", encoding="utf-8") as stream:
    result = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=150)
print(result_path.read_text())
sys.exit(result.returncode)
