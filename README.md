# Scanner technique Binance Spot /USDC

Outil semi-automatisé d'aide à la décision (trading **Spot, long uniquement**).
Ce dépôt correspond au **Lot 0 — Fondations** du cahier des charges v1.1 :
arborescence, dépendances, configuration validée (`pydantic`) et journalisation.

## Prérequis
- Python ≥ 3.11

## Installation
```bash
python -m venv .venv
# Windows :        .venv\Scripts\activate
# macOS / Linux :  source .venv/bin/activate
pip install -r requirements.txt
```

## Utilisation
Valider la configuration et afficher un résumé :
```bash
python -m scanner.cli check
```
Afficher la configuration normalisée (JSON) :
```bash
python -m scanner.cli show
```
Une configuration invalide est rejetée avec un message clair (champ + raison)
et un code de sortie 1.

## Tests
```bash
python -m pytest
```

## Structure
```
binance-usdc-scanner/
├── config.yaml            # configuration (CDC §4.6)
├── requirements.txt
├── conftest.py            # racine du projet sur sys.path (tests)
├── scanner/
│   ├── constants.py       # intervalles Binance valides
│   ├── config.py          # modèles pydantic + chargement/validation
│   ├── logging_setup.py   # journalisation console + fichier
│   └── cli.py             # point d'entrée (check / show)
└── tests/
    └── test_config.py     # validation (critère d'acceptation du Lot 0)
```

## Configuration
Tout paramètre vit dans `config.yaml` (aucune constante en dur dans le code).
Le fichier est validé au démarrage : clés inconnues refusées, cohérences
vérifiées (somme des poids = 1, `watch` < `signal`, `fast` < `slow`,
intervalles des tiers présents dans `intervals`, etc.).

**À confirmer :** la période du Momentum (`indicators.momentum.period`, défaut 10)
doit correspondre à celle affichée sur votre graphique Binance/TradingView.

## Lots suivants
- **Lot 1** — récupération des données (klines, exchangeInfo), rate limiting, cache
- **Lot 2** — indicateurs (TA-Lib)
- **Lot 3** — scoring + consolidation multi-échelles (approche B)
- **Lot 4** — restitution CSV
- **Lot 5** — analyse fondamentale (Mode B, API Anthropic)
- **Lot 6** — dashboard (option), backtest léger
