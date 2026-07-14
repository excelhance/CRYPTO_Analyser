# CLAUDE.md — Scanner technique Binance Spot /USDC

Contexte et conventions du projet, à lire à chaque session.

## Projet
Outil semi-automatisé d'aide à la décision pour le trading **Spot, long uniquement**, sur les paires `*/USDC` de Binance. Il scanne l'univers des paires, calcule des indicateurs techniques sur plusieurs unités de temps, produit un **score d'opportunité long consolidé** par paire, puis assiste une revue fondamentale.

**Spécification de référence : `docs/CDC.md` (CDC v1.1). C'est la source de vérité.** En cas de doute sur une règle, un paramètre ou l'architecture, s'y référer avant de coder.

## État d'avancement
- **Lot 0 (fondations) : FAIT** — arborescence, `config.yaml`, validation pydantic v2, journalisation, CLI (`check` / `show`).
- **Lot 1 (couche données) : FAIT** — `data_fetcher.py` (univers + klines), `rate_limiter.py` (gouverneur de poids, backoff 429/418), `cache.py` (parquet, incrémental).
- **Lot 2 (indicateurs) : FAIT** — `indicators.py`, wrap TA-Lib exclusivement (aucun indicateur réimplémenté à la main).
- **Lot 3 (scoring) : FAIT** — `scoring_engine.py`, score directionnel par TF (ADX modulé) + consolidation multi-échelles (approche B) + décomposition.
- **Lot 4 (restitution) : FAIT** — `reporting.py`, console (rich) + export CSV (une ligne par paire).
- **Lot 5 (fondamental) : FAIT, en Mode A** — `fundamentals.py` génère un prompt unique (données dures CoinGecko/DefiLlama + cadrage de sourçage) écrit en `.md` et affiché console, à coller manuellement dans l'interface Claude. Le **Mode B** (appel automatisé de l'API Anthropic, recherche web, estimateur de coût) avait été implémenté puis **abandonné** : complexité et coût de l'estimateur disproportionnés, choix de garder l'humain dans la boucle. Détail : `docs/CDC.md` §5.3 ("Historique de décision").
- **Lot 6 (options) : PAS COMMENCÉ** — dashboard `streamlit`, backtest léger. Non prioritaire (§6.3 du CDC).
- 136 tests verts (`python -m pytest`). Découpage des lots et critères d'acceptation : §8 du CDC.

## Développement incrémental (règle stricte)
- Ne jamais démarrer un lot tant que le critère d'acceptation du précédent n'est pas vérifié (tests verts).
- Chaque lot doit être livrable et testable isolément.
- Après toute modification significative : lancer les tests et corriger **avant** de considérer la tâche terminée (auto-vérification systématique).

## Architecture (dossier `scanner/`)
- `config.py` — modèles pydantic + chargement/validation *(fait, Lot 0)*
- `constants.py` — constantes partagées (intervalles Binance valides) *(fait, Lot 0)*
- `logging_setup.py` — journalisation console + fichier *(fait, Lot 0)*
- `data_fetcher.py` — univers `*/USDC` (`exchangeInfo`) + bougies (`klines`) *(fait, Lot 1)*
- `rate_limiter.py` — gouverneur de poids Binance, backoff 429/418 *(fait, Lot 1)*
- `cache.py` — persistance parquet, rafraîchissement incrémental *(fait, Lot 1)*
- `indicators.py` — indicateurs par TF, wrap TA-Lib *(fait, Lot 2)*
- `scoring_engine.py` — score par TF + consolidation multi-échelles + décomposition *(fait, Lot 3)*
- `scanner.py` — orchestrateur du pipeline (gates, appel des couches) *(fait, Lot 3/4)*
- `reporting.py` — restitution console (rich) + CSV + prompt fondamental *(fait, Lot 4/5)*
- `fundamentals.py` — résolution CoinGecko/DefiLlama + génération du prompt (Mode A) *(fait, Lot 5)*
- `cli.py` — point d'entrée typer (`check`/`show`/`scan`/`fundamentals`) *(fait)*

## Conventions de code
- **Commentaires et docstrings en français.**
- **Tout paramètre passe par `config.yaml`** — aucune constante « magique » en dur dans le code (auditabilité : l'outil ne doit pas être une boîte noire).
- Architecture **modulaire et réutilisable** ; rejeter la complexité inutile (pas de sur-ingénierie, pas de couches superflues).
- **Type hints systématiques** ; fonctions courtes à responsabilité unique ; séparation stricte des couches (une couche ignore l'implémentation des autres).
- Gérer explicitement les cas limites : données manquantes/insuffisantes (**dégradation gracieuse**), erreurs réseau, valeurs `NaN` de chauffe des indicateurs.
- Nommage explicite ; pas d'abréviations obscures.
- Ajouter/mettre à jour les tests `pytest` à chaque lot.

## Stack technique
- Python 3.14 ; pydantic v2, PyYAML, typer, rich *(en place, Lot 0)*.
- `httpx` (API Binance), `pyarrow` (cache parquet), `pandas`/`numpy` *(en place, Lot 1)*.
- **TA-Lib** (indicateurs) *(en place, Lot 2)*.
- Fondamental (Lot 5, Mode A) : `python-dotenv` (clé CoinGecko) — **aucune dépendance `anthropic`** (pas d'appel API, cf. état d'avancement ci-dessus).
- Lot 6 (option, pas commencé) : `streamlit` (dashboard).
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
