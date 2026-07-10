# CLAUDE.md — Scanner technique Binance Spot /USDC

Contexte et conventions du projet, à lire à chaque session.

## Projet
Outil semi-automatisé d'aide à la décision pour le trading **Spot, long uniquement**, sur les paires `*/USDC` de Binance. Il scanne l'univers des paires, calcule des indicateurs techniques sur plusieurs unités de temps, produit un **score d'opportunité long consolidé** par paire, puis assiste une revue fondamentale.

**Spécification de référence : `docs/CDC.md` (CDC v1.1). C'est la source de vérité.** En cas de doute sur une règle, un paramètre ou l'architecture, s'y référer avant de coder.

## État d'avancement
- **Lot 0 (fondations) : FAIT** — arborescence, `config.yaml`, validation pydantic v2, journalisation, CLI (`check` / `show`), 11 tests verts.
- **Prochain : Lot 1** — récupération des données (exchangeInfo + klines), gestion des rate limits, cache. Découpage des lots et critères d'acceptation : §8 du CDC.

## Développement incrémental (règle stricte)
- Ne jamais démarrer un lot tant que le critère d'acceptation du précédent n'est pas vérifié (tests verts).
- Chaque lot doit être livrable et testable isolément.
- Après toute modification significative : lancer les tests et corriger **avant** de considérer la tâche terminée (auto-vérification systématique).

## Architecture (dossier `scanner/`)
- `config.py` — modèles pydantic + chargement/validation *(fait)*
- `constants.py` — constantes partagées (intervalles Binance valides) *(fait)*
- `logging_setup.py` — journalisation console + fichier *(fait)*
- `cli.py` — point d'entrée typer *(fait)*
- À venir : `data_fetcher.py`, `rate_limiter.py`, `cache.py` (Lot 1) ; `indicators.py` (Lot 2) ; `scoring_engine.py` (Lot 3) ; `reporting.py` (Lot 4) ; `fundamentals.py` (Lot 5).

## Conventions de code
- **Commentaires et docstrings en français.**
- **Tout paramètre passe par `config.yaml`** — aucune constante « magique » en dur dans le code (auditabilité : l'outil ne doit pas être une boîte noire).
- Architecture **modulaire et réutilisable** ; rejeter la complexité inutile (pas de sur-ingénierie, pas de couches superflues).
- **Type hints systématiques** ; fonctions courtes à responsabilité unique ; séparation stricte des couches (une couche ignore l'implémentation des autres).
- Gérer explicitement les cas limites : données manquantes/insuffisantes (**dégradation gracieuse**), erreurs réseau, valeurs `NaN` de chauffe des indicateurs.
- Nommage explicite ; pas d'abréviations obscures.
- Ajouter/mettre à jour les tests `pytest` à chaque lot.

## Stack technique
- Python 3.14 ; pydantic v2, PyYAML, typer, rich *(en place)*.
- À venir selon les lots : `httpx` (API Binance), `pyarrow` (cache parquet), `pandas`/`numpy`, **TA-Lib** (indicateurs), `anthropic` (synthèse fondamentale, Mode B).
- **Ne jamais réimplémenter un indicateur à la main** : déléguer à TA-Lib.

## Commandes utiles (Windows / PowerShell)
- Activer l'environnement : `.\.venv\Scripts\Activate.ps1`
- Installer les dépendances : `pip install -r requirements.txt`
- Valider la configuration : `python -m scanner.cli check`
- Lancer les tests : `python -m pytest`

## Points de vigilance (rappels du CDC)
- Exclure systématiquement la **bougie en cours non clôturée** avant tout calcul (biais de look-ahead).
- Respecter les **rate limits Binance** : budget de poids/minute, backoff sur 429/418 (§2.4 du CDC).
- L'outil produit des **signaux, pas des décisions** ; aucun signal short (Spot long only).
- Sobriété des paramètres : plus de réglages = plus de risque de **sur-optimisation**. Ne jamais régler la config pour « battre » un backtest.

## Communication
- Répondre en **français**, ton professionnel et direct.
- Si plusieurs approches existent, les présenter avec avantages/inconvénients.
- En cas de doute sur une API ou un comportement, vérifier la **documentation officielle** avant de coder.
- Demander une clarification si une consigne est ambiguë, plutôt que de deviner.
