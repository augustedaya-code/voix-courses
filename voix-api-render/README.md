# API de voix — assistante de courses

Ta propre API de synthèse vocale. Elle est gratuite, tourne sur un simple processeur (pas besoin de carte graphique) et propose des voix françaises naturelles basées sur [Piper](https://github.com/OHF-Voice/piper1-gpl).

Ton application envoie un texte et reçoit un fichier MP3. Les phrases déjà prononcées sont gardées en cache et reviennent instantanément.

## Les voix

| id | Nom | Genre | Langue |
|---|---|---|---|
| `camille` (défaut) | Camille | femme | français |
| `pierre` | Pierre | homme | français |

Pour ajouter ou retirer des voix, modifie `voices.json`. Toutes les voix Piper sont listées sur [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices), avec des extraits à écouter sur [rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/). Avant un usage commercial, vérifie la licence de chaque voix : elle est indiquée dans le fichier `MODEL_CARD` de la voix.

---

## 1. Mettre en ligne gratuitement (Render)

1. Dépose ces fichiers dans un dépôt GitHub.
2. Sur [render.com](https://render.com) : **New → Web Service**, choisis ton dépôt, langage **Docker**, offre **Free**.
3. Ajoute la variable d'environnement `API_KEYS` (ta clé secrète), puis lance **Deploy**.
4. Ton API est en ligne à l'adresse `https://NOM.onrender.com`.

> Sur l'offre gratuite, le service s'endort après 15 minutes sans utilisation : la première requête au réveil prend environ une minute. Le petit processeur gratuit met quelques secondes par phrase (les phrases répétées sortent du cache instantanément).

## 2. Lancer sur ton ordinateur (facultatif)

```bash
pip install -r requirements.txt
python download_voices.py           # télécharge les voix dans ./models
API_KEYS=ma-cle uvicorn app:app --port 8000
# puis ouvre http://localhost:8000
```

Il faut aussi `ffmpeg` pour le MP3. Sans `ffmpeg`, utilise `"format": "wav"`.

Avec Docker :

```bash
docker build -t voix-api .
docker run -p 10000:10000 -e API_KEYS=ma-cle voix-api
```

---

## 3. Utiliser l'API

Toutes les requêtes demandent ta clé, à envoyer de l'une de ces façons :
- en-tête `Authorization: Bearer TA_CLE` (recommandé) ;
- ou en-tête `X-API-Key: TA_CLE` ;
- ou `?key=TA_CLE` dans l'URL, pratique pour donner directement une URL à un lecteur audio.

### `POST /v1/tts` — transformer un texte en voix

```json
{
  "text": "J'ai ajouté 2 kg de pommes. Total : 6,40 €.",
  "voice": "camille",
  "speed": 1.0,
  "format": "mp3"
}
```

| champ | obligatoire | valeurs |
|---|---|---|
| `text` | oui | 600 caractères max |
| `voice` | non | un `id` de `/v1/voices` (défaut `camille`) |
| `speed` | non | de `0.5` (lent) à `2` (rapide), défaut `1` |
| `format` | non | `mp3` (défaut) ou `wav` |

La réponse est directement le fichier audio. Les prix et quantités sont lus naturellement : « 6,40 € » devient « 6 euros 40 », « 2 kg » devient « 2 kilos », « 33cl » devient « 33 centilitres ».

### `GET /v1/tts?text=...&voice=...&key=...`

Même chose en GET, utile quand ton lecteur audio attend une simple URL.

### `GET /v1/voices` — liste des voix

### `GET /health` — vérifier que le service tourne

### Erreurs

Les erreurs sont toujours renvoyées au format `{"error": {"code": "...", "message": "..."}}` :
`401` clé invalide, `400` paramètre invalide, `404` voix inconnue, `413` texte trop long, `429` trop de requêtes (60 par minute par défaut).

---

## 4. Exemples depuis ton application

**JavaScript / site web**

```js
const res = await fetch("https://NOM.onrender.com/v1/tts", {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: "Bearer TA_CLE" },
  body: JSON.stringify({ text: "Ta liste est prête !", voice: "camille" }),
});
const audio = new Audio(URL.createObjectURL(await res.blob()));
audio.play();
```

**React Native (Expo, avec `expo-audio`)**

```js
import { createAudioPlayer } from "expo-audio";

export function parler(texte, voix = "camille") {
  const url = "https://NOM.onrender.com/v1/tts"
    + `?text=${encodeURIComponent(texte)}&voice=${voix}&key=TA_CLE`;
  const player = createAudioPlayer(url);
  player.play();
  return player;
}
```

**Flutter (avec `just_audio`)**

```dart
final player = AudioPlayer();
final uri = Uri.https("NOM.onrender.com", "/v1/tts",
    {"text": "Ta liste est prête !", "voice": "camille"});
await player.setUrl(uri.toString(), headers: {"Authorization": "Bearer TA_CLE"});
player.play();
```

**Python**

```python
import requests
r = requests.post("https://NOM.onrender.com/v1/tts",
                  headers={"Authorization": "Bearer TA_CLE"},
                  json={"text": "Ta liste est prête !", "voice": "pierre"})
open("reponse.mp3", "wb").write(r.content)
```

> **Sécurité :** une clé placée dans une application mobile peut être extraite par quelqu'un de motivé. C'est acceptable pour démarrer, et la limite de débit protège ton serveur. Quand ton appli grandira, fais passer les appels par ton propre backend, qui seul connaît la clé.

---

## 5. Réglages (variables d'environnement)

| variable | défaut | rôle |
|---|---|---|
| `API_KEYS` | *(vide = ouvert à tous)* | clés autorisées, séparées par des virgules |
| `DEFAULT_VOICE` | `camille` | voix utilisée si aucune n'est précisée |
| `RATE_LIMIT_PER_MIN` | `60` | requêtes max par minute et par clé |
| `MAX_TEXT_CHARS` | `600` | longueur max d'un texte |
| `CACHE_MAX_FILES` | `2000` | nombre de phrases gardées en cache |
| `CORS_ORIGINS` | `*` | sites web autorisés à appeler l'API |
| `TTS_CONCURRENCY` | `2` | synthèses en parallèle |
| `TTS_ENGINE` | `piper` | `fake` pour tester sans modèles |

## 6. Plus tard : changer de moteur sans toucher à l'appli

Ton application ne connaît que `/v1/tts`. Pour passer un jour à Azure, ElevenLabs ou une voix clonée, il suffit d'ajouter une classe de moteur dans `app.py`, à côté de `PiperEngine`. L'application, elle, ne change pas.
