"""Télécharge les modèles Piper listés dans voices.json.

Lancé automatiquement pendant la construction de l'image Docker.
Usage manuel :  python download_voices.py
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
ROOT = Path(__file__).resolve().parent
MODELS_DIR = Path(os.environ.get("MODELS_DIR", ROOT / "models"))


def model_url(model: str) -> str:
    # ex. fr_FR-siwis-medium -> fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx
    locale, name, quality = model.split("-", 2)
    lang = locale.split("_")[0]
    return f"{BASE_URL}/{lang}/{locale}/{name}/{quality}/{model}.onnx"


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  déjà présent : {dest.name}")
        return
    print(f"  téléchargement : {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    tmp.rename(dest)


def main() -> int:
    voices = json.loads((ROOT / "voices.json").read_text(encoding="utf-8"))
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for model in sorted({v["model"] for v in voices}):
        print(model)
        url = model_url(model)
        download(url, MODELS_DIR / f"{model}.onnx")
        download(url + ".json", MODELS_DIR / f"{model}.onnx.json")
    print("Toutes les voix sont prêtes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
