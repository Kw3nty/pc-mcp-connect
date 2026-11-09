"""Создаёт build/icon.ico для exe (тот же рисунок, что и значок в трее)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pcmcp_connect import make_image  # noqa: E402

out = HERE.parent / "build" / "icon.ico"
out.parent.mkdir(parents=True, exist_ok=True)
img = make_image("online", 256)
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon:", out)
