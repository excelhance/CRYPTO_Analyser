# NOTES.md — Carnet de bord du projet

Journal des décisions différées, points d'observation et arbitrages à revoir.
Ce fichier est documentaire : il n'est lu ni par le code ni par le scoring.

---

## ⏳ Décisions différées (à trancher après observation, PAS maintenant)

### Calibrage du scoring — à revoir après 1 à 2 semaines d'observation
**Constat (jour 1, marché baissier) :** sur 46 paires passant le gate D6, **0 signal**, 0 watch, score max = 36,5 (ZECUSDC). 31 paires à 0.
**Ce n'est PAS un bug** — mécanique conforme au CDC §4.4 : un tier baissier impose `contradiction` (m=0,2). Un signal exige un alignement haussier simultané des 3 tiers (H4/H12 + 1D + 1W/1M), rare par construction.
**À faire :** observer sur plusieurs jours de marchés variés avant tout ajustement. Ne JAMAIS calibrer sur un instantané (piège du §7 — sur-optimisation).
**Leviers d'ajustement possibles (le jour venu, tous dans `config.yaml`, sans toucher au code) :**
1. abaisser `thresholds.signal` / `thresholds.watch` (le plus simple) ;
2. adoucir `alignment_multiplier.contradiction` (0,2 → 0,35 ?) pour qu'un tier baissier pénalise sans annihiler ;
3. revoir `classification.neutral_band` (une bande large classe trop de tiers en « baissier »).

### Gate de liquidité D6 — seuil provisoire
Réglé à **500 000 USDC/24 h** → ~46 paires (sur 284). Choisi sur la distribution observée (250k→74, 500k→46, 1M→29, 5M→7).
**À revoir à l'usage :** si le flux de signaux est trop maigre, descendre ; trop bruyant, remonter. Paramètre : `gates.min_quote_volume_24h`.

### Squeeze de Bollinger — option en réserve
La largeur des bandes (`bb_width`) est actuellement **non-scorante** (exposée en métadonnée seulement) — décision validée : la largeur mesure la volatilité, pas la direction.
**Idée en réserve pour une v1.1 du scoring :** l'utiliser comme *multiplicateur de confiance* sur `percent_b_reversion` (un %B extrême pendant un squeeze = signal plus fort). Écartée pour l'instant : ajouterait un mécanisme non prévu au CDC. À reconsidérer après observation du comportement réel.

### Momentum (MOM) — normalisation
Colonne `mom` conservée en valeur brute (affichage). Le scoring utilise **signe + pente uniquement**, jamais la valeur absolue (non comparable entre paires, §3.2). Point de vigilance permanent si on retouche la règle `momentum_signe_pente`.

---

## ✅ Historique des lots

- **Lot 0 — Fondations.** Config + validation pydantic, logging, CLI. Validé (11 tests).
- **Lot 1 — Couche données.** `data_fetcher` (exchangeInfo + ticker/24hr + klines), `rate_limiter`, `cache` parquet incrémental. Validé en conditions réelles (poids consommé = 102 = 20+80+2, univers = 284 paires, bougie en cours exclue).
- **Lot 2 — Indicateurs (TA-Lib).** Jeu §3.2 + dérivés, dégradation gracieuse. Validé : **RSI et MACD de BTCUSDC 1d confirmés identiques à Binance** (la validation qui prouve la justesse de toute la chaîne). Correctif encodage UTF-8 (Windows/cp1252) ajouté dans `scanner/__init__.py`.
- **Lot 3 — Scoring + consolidation.** 14 règles / 5 catégories, modulation ADX, consolidation approche B, décomposition complète. Validé : mécanique (102 tests) + **ordre du classement jugé correct à l'œil sur données réelles** (ZEC en tête). Bug d'infini (`percent_b` sur bande nulle, memecoins) corrigé.

---

## 🐛 Bugs notables résolus (pour mémoire)

- **Encodage Windows (cp1252)** : les caractères `✓ ≥ →` faisaient planter la sortie quand elle était capturée. Corrigé globalement (`_force_utf8_streams` dans `scanner/__init__.py`).
- **Division par zéro Bollinger** : sur un prix ultra-faible (PEPEUSDC ~1e-6), `bb_upper == bb_lower` → `percent_b = x/0 = inf`, non attrapé par le garde-fou `NaN`. Corrigé : `band_range == 0` → bbands omis ; garde-fou `_missing()` durci pour attraper aussi `inf`.

---

## 📌 Rappels de méthode

- **Un commit avant chaque tâche confiée à Claude Code** = un point de restauration.
- Les tests unitaires prouvent la **cohérence**, pas la **justesse** : confronter au réel (Binance, œil humain) à chaque lot.
- Tout paramètre reste dans `config.yaml` — aucune constante magique.
- Ne jamais calibrer un réglage sur un instantané de marché (§7 du CDC — sur-optimisation).


## Lot 4 — trou de spéc « référence 1D absente ». 
Le CDC §5 gérait le biais manquant (1M/1W) mais pas l'absence de la référence 1D elle-même. Décision (option 2) : garder la paire au classement mais la marquer (reference_1d_absente, drapeau distinct de « contexte insuffisant ») et plafonner son niveau à watch (gates.max_level_without_reference_1d). 1 paire concernée au scan du 11/07 (GRAMUSDC).