"""API de voix (synthèse vocale) pour l'assistante de courses.

Endpoints :
  GET  /                -> page de test
  GET  /health          -> état du service
  GET  /v1/voices       -> liste des voix
  POST /v1/tts          -> JSON {text, voice, speed, format} -> fichier audio
  GET  /v1/tts?text=..  -> même chose en GET (pratique pour lire une URL directement)

Variables d'environnement :
  API_KEYS            clés autorisées, séparées par des virgules (vide = API ouverte, à éviter en prod)
  TTS_ENGINE          "piper" (défaut) ou "fake" (tests sans modèle)
  MODELS_DIR          dossier des modèles .onnx (défaut ./models)
  CACHE_DIR           dossier du cache audio (défaut ./cache)
  CACHE_MAX_FILES     nombre max de fichiers en cache (défaut 2000)
  MAX_TEXT_CHARS      longueur max d'un texte (défaut 600)
  RATE_LIMIT_PER_MIN  requêtes max par minute et par client (défaut 60)
  DEFAULT_VOICE       id de la voix par défaut (défaut camille)
  CORS_ORIGINS        origines autorisées, séparées par des virgules (défaut *)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import wave
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent
MODELS_DIR = Path(os.environ.get("MODELS_DIR", ROOT / "models"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ROOT / "cache"))
CACHE_MAX_FILES = int(os.environ.get("CACHE_MAX_FILES", "2000"))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "600"))
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))
ENGINE = os.environ.get("TTS_ENGINE", "piper").lower()
API_KEYS = {k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()}
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

VOICES: list[dict] = json.loads((ROOT / "voices.json").read_text(encoding="utf-8"))
VOICES_BY_ID = {v["id"]: v for v in VOICES}
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", VOICES[0]["id"])
HAS_FFMPEG = shutil.which("ffmpeg") is not None
FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}

CACHE_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Normalisation du texte (vocabulaire des courses)
# --------------------------------------------------------------------------
_UNITS = [
    (r"kg", "kilos"),
    (r"g", "grammes"),
    (r"mg", "milligrammes"),
    (r"[lL]", "litres"),
    (r"cl", "centilitres"),
    (r"ml", "millilitres"),
]


def normalize_text(text: str, lang: str = "fr-FR") -> str:
    """Rend lisibles à voix haute les prix et quantités : « 3,50 € » -> « 3 euros 50 »."""
    text = re.sub(r"\s+", " ", text).strip()
    if not lang.startswith("fr"):
        return text
    # Prix : 3,50 € / 3.50€ / 3€50
    text = re.sub(r"(\d+)[,.](\d{2})\s?€", r"\1 euros \2", text)
    text = re.sub(r"(\d+)\s?€\s?(\d{2})\b", r"\1 euros \2", text)
    text = re.sub(r"\b1\s?€", "1 euro", text)
    text = re.sub(r"(\d+)\s?€", r"\1 euros", text)
    text = text.replace("€", " euros")
    # Décimales françaises : 1,5 kg -> 1 virgule 5 kg
    text = re.sub(r"(\d+),(\d+)", r"\1 virgule \2", text)
    # Unités collées à un nombre : 500g, 2 kg, 1L, 33cl
    for unit, word in _UNITS:
        text = re.sub(rf"(\d)\s?{unit}\b", rf"\1 {word}", text)
    # Quantités : x3 -> fois 3
    # Singulier : « 1 litres » -> « 1 litre », « 1 euros 50 » -> « 1 euro 50 »
    text = re.sub(r"(?<![\d,.])\b1 (kilo|gramme|milligramme|litre|centilitre|millilitre|euro)s\b", r"1 \1", text)
    text = re.sub(r"\bx\s?(\d+)\b", r"fois \1", text)
    text = text.replace("&", " et ").replace("%", " pour cent")
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Moteurs de synthèse
# --------------------------------------------------------------------------
class PiperEngine:
    """Synthèse avec Piper (https://github.com/OHF-Voice/piper1-gpl), sur CPU."""

    def __init__(self) -> None:
        from piper import PiperVoice, SynthesisConfig  # import tardif : seulement en prod

        self._PiperVoice = PiperVoice
        self._SynthesisConfig = SynthesisConfig
        self._models: dict[str, object] = {}
        self._load_lock = threading.Lock()
        # Un modèle ONNX utilise déjà plusieurs cœurs : on limite les synthèses en parallèle.
        self._sem = threading.Semaphore(int(os.environ.get("TTS_CONCURRENCY", "2")))

    def _model(self, name: str):
        with self._load_lock:
            if name not in self._models:
                path = MODELS_DIR / f"{name}.onnx"
                if not path.exists():
                    raise FileNotFoundError(
                        f"Modèle {name} absent de {MODELS_DIR}. Lance : python download_voices.py"
                    )
                self._models[name] = self._PiperVoice.load(str(path))
            return self._models[name]

    def _speaker_id(self, model, speaker, model_name: str) -> int | None:
        if speaker is None:
            return None
        if isinstance(speaker, int):
            return speaker
        mapping = getattr(getattr(model, "config", None), "speaker_id_map", None)
        if not mapping:  # repli : lecture directe du fichier de configuration du modèle
            cfg = MODELS_DIR / f"{model_name}.onnx.json"
            mapping = json.loads(cfg.read_text(encoding="utf-8")).get("speaker_id_map", {})
        if speaker not in mapping:
            raise ValueError(f"Locuteur « {speaker} » introuvable dans le modèle.")
        return mapping[speaker]

    def warmup(self) -> None:
        for name in {v["model"] for v in VOICES}:
            self._model(name)

    def synthesize(self, text: str, voice: dict, speed: float) -> bytes:
        model = self._model(voice["model"])
        config = self._SynthesisConfig(
            speaker_id=self._speaker_id(model, voice.get("speaker"), voice["model"]),
            length_scale=1.0 / speed,  # length_scale > 1 = plus lent
        )
        buf = io.BytesIO()
        with self._sem, wave.open(buf, "wb") as wav_file:
            model.synthesize_wav(text, wav_file, syn_config=config)
        return buf.getvalue()


class FakeEngine:
    """Génère un simple bip dont la durée dépend du texte. Sert aux tests sans modèle."""

    def warmup(self) -> None:
        pass

    def synthesize(self, text: str, voice: dict, speed: float) -> bytes:
        rate = 22050
        seconds = min(0.25 + len(text) * 0.06 / speed, 30)
        freq = 220 + 40 * (sum(map(ord, voice["id"])) % 10)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(rate * seconds))
        )
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)
        return buf.getvalue()


engine = FakeEngine() if ENGINE == "fake" else PiperEngine()


def wav_to_mp3(wav: bytes) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
         "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        input=wav, capture_output=True, check=True,
    )
    return proc.stdout


# --------------------------------------------------------------------------
# Cache disque : une phrase déjà dite n'est jamais recalculée
# --------------------------------------------------------------------------
def cache_key(text: str, voice_id: str, speed: float, fmt: str) -> str:
    raw = json.dumps([text, voice_id, round(speed, 2), fmt], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_get(key: str, fmt: str) -> bytes | None:
    path = CACHE_DIR / f"{key}.{fmt}"
    if path.exists():
        path.touch()  # marque comme récemment utilisé
        return path.read_bytes()
    return None


def cache_put(key: str, fmt: str, data: bytes) -> None:
    tmp = CACHE_DIR / f"{key}.{fmt}.tmp"
    tmp.write_bytes(data)
    tmp.replace(CACHE_DIR / f"{key}.{fmt}")
    files = [p for p in CACHE_DIR.iterdir() if p.suffix in (".mp3", ".wav")]
    if len(files) > CACHE_MAX_FILES:
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - CACHE_MAX_FILES]:
            p.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Sécurité : clé d'API + limite de débit
# --------------------------------------------------------------------------
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def get_api_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or request.query_params.get("key")


def check_access(request: Request) -> JSONResponse | None:
    key = get_api_key(request)
    if API_KEYS and key not in API_KEYS:
        return error(401, "unauthorized", "Clé d'API manquante ou invalide.")
    client = key or (request.client.host if request.client else "anonyme")
    now = time.monotonic()
    with _hits_lock:
        q = _hits[client]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            return error(429, "rate_limited", "Trop de requêtes, réessaie dans une minute.")
        q.append(now)
    return None


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
async def index(request: Request) -> Response:
    return FileResponse(ROOT / "static" / "index.html")


async def health(request: Request) -> Response:
    return JSONResponse({
        "status": "ok",
        "engine": ENGINE,
        "voices": len(VOICES),
        "mp3": HAS_FFMPEG,
        "auth": bool(API_KEYS),
    })


async def list_voices(request: Request) -> Response:
    if (denied := check_access(request)) is not None:
        return denied
    return JSONResponse({
        "default": DEFAULT_VOICE,
        "voices": [
            {k: v[k] for k in ("id", "name", "gender", "lang", "description")} for v in VOICES
        ],
    })


async def tts(request: Request) -> Response:
    if (denied := check_access(request)) is not None:
        return denied

    if request.method == "POST":
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return error(400, "invalid_json", "Le corps doit être un objet JSON.")
    else:
        body = dict(request.query_params)

    text = str(body.get("text", "")).strip()
    voice_id = str(body.get("voice") or DEFAULT_VOICE)
    fmt = str(body.get("format") or ("mp3" if HAS_FFMPEG else "wav")).lower()
    try:
        speed = float(body.get("speed", 1.0))
    except (TypeError, ValueError):
        return error(400, "invalid_speed", "speed doit être un nombre entre 0.5 et 2.")

    if not text:
        return error(400, "missing_text", "Le champ text est obligatoire.")
    if len(text) > MAX_TEXT_CHARS:
        return error(413, "text_too_long", f"Texte limité à {MAX_TEXT_CHARS} caractères.")
    if voice_id not in VOICES_BY_ID:
        return error(404, "unknown_voice", f"Voix inconnue : {voice_id}. Voir /v1/voices.")
    if not 0.5 <= speed <= 2.0:
        return error(400, "invalid_speed", "speed doit être entre 0.5 et 2.")
    if fmt not in FORMATS:
        return error(400, "invalid_format", "format doit être mp3 ou wav.")
    if fmt == "mp3" and not HAS_FFMPEG:
        return error(501, "mp3_unavailable", "ffmpeg n'est pas installé : utilise format=wav.")

    voice = VOICES_BY_ID[voice_id]
    spoken = normalize_text(text, voice["lang"])
    key = cache_key(spoken, voice_id, speed, fmt)

    started = time.perf_counter()
    audio = await run_in_threadpool(cache_get, key, fmt)
    cached = audio is not None
    if not cached:
        try:
            def render() -> bytes:
                wav = engine.synthesize(spoken, voice, speed)
                return wav_to_mp3(wav) if fmt == "mp3" else wav

            audio = await run_in_threadpool(render)
        except FileNotFoundError as exc:
            return error(503, "model_missing", str(exc))
        except Exception as exc:  # erreur moteur inattendue
            return error(500, "synthesis_failed", f"Échec de la synthèse : {exc}")
        await run_in_threadpool(cache_put, key, fmt, audio)

    return Response(
        audio,
        media_type=FORMATS[fmt],
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="voix-{voice_id}.{fmt}"',
            "X-Cache": "HIT" if cached else "MISS",
            "X-Voice": voice_id,
            "X-Render-Ms": str(int((time.perf_counter() - started) * 1000)),
        },
    )


@contextlib.asynccontextmanager
async def lifespan(app):
    # Charge les modèles en arrière-plan pour que la 1re requête soit rapide.
    asyncio.get_running_loop().run_in_executor(None, _safe_warmup)
    yield


def _safe_warmup() -> None:
    try:
        engine.warmup()
        print("Voix chargées.")
    except Exception as exc:
        print(f"Préchargement des voix impossible : {exc}")


app = Starlette(
    routes=[
        Route("/", index),
        Route("/health", health),
        Route("/v1/voices", list_voices),
        Route("/v1/tts", tts, methods=["GET", "POST"]),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=CORS_ORIGINS,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
            expose_headers=["X-Cache", "X-Voice", "X-Render-Ms"],
        )
    ],
    lifespan=lifespan,
)

if not API_KEYS:
    print("ATTENTION : API_KEYS est vide, l'API est ouverte à tous.")
