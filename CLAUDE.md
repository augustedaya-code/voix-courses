# Consignes pour Claude

## Qui fait quoi (délégation des modèles)

- **Collecte** (lire la doc, fouiller le code, résumer des pages) → sous-agent `collecte` (Haiku).
- **Code** d'une tâche déjà planifiée → un sous-agent en Sonnet.
- **Décisions** (le plan, l'architecture, les choix) → restent dans la conversation principale (Opus).
- Ce que rend Haiku est vérifié avant d'être utilisé pour décider.
