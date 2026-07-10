# Cahier des charges technique — v1.1
## Outil semi-automatisé d'aide à la décision — scan technique Binance Spot (paires /USDC)

**Version :** 1.1 (décisions D1–D10 verrouillées) · **Cible :** Python via Claude Code · **Exécution :** locale, manuelle, à la demande

> **Nature de l'outil :** trading **Spot, long uniquement**. Le « score » est donc un **score d'opportunité à l'achat (long)** sur 0–100 : une configuration baissière ne génère pas de signal short (impossible en Spot), elle produit simplement un score bas. Voir §4 et §7.

### Paramètres clés verrouillés

| Sujet | Valeur retenue |
|---|---|
| Unités de temps (D1) | `4h`, `12h`, `1d`, `1w`, `1M` |
| Consolidation multi-échelles (D1) | Approche **B (descendante : contexte → déclenchement)** |
| Profondeur d'historique (D2) | `limit=1000` par requête + dégradation gracieuse |
| Cadence (D3) | Incrémental par défaut + option `--force-refresh` |
| Moyennes mobiles (D4) | EMA **20 / 50 / 200** |
| Indicateurs actifs (D5) | EMA, RSI (30/70), MACD, SAR, Bollinger + largeur, Momentum (MOM 10), Volume, **ADX**, chandeliers ; ATR pour le risque |
| Gate de liquidité (D6) | ≥ **1 M USDC** / 24 h (volume de la paire /USDC) |
| Config scoring (D7) | Fournie par défaut (§4.6), à caler **par observation, pas par backtest** |
| Sources fondamentales (D8) | **CoinGecko** (clé démo) + **DefiLlama** ; actus FR+EN via recherche web de Claude |
| Synthèse (D9) | **Mode B** (API Anthropic), modèle `claude-sonnet-5`, sortie JSON |
| Restitution (D10) | **CSV** unique, **une ligne par paire** |

---

## 0. Cadrage

**Objet.** Scanner l'ensemble des paires `*/USDC` du Spot Binance, calculer les indicateurs sur 5 unités de temps, produire un **score d'opportunité long consolidé** par paire selon des règles explicites et configurables, et restituer un classement. Pour les meilleurs candidats, assister une revue fondamentale (Mode B).

**Ce que l'outil fait :** produit des **signaux hiérarchisés** et une matière première d'analyse.
**Ce que l'outil ne fait pas :** aucun ordre, aucun portefeuille, aucune décision, aucun signal short.

**Hypothèses :** données de marché publiques Binance (sans authentification), exécution locale, Python ≥ 3.11.

**Stack :**

| Couche | Choix retenu |
|---|---|
| HTTP | `httpx` en direct |
| Données | `pandas`, `numpy` ≥ 2.x |
| Indicateurs | **TA-Lib ≥ 0.6.x** (wheels) |
| Config | `pydantic` + `YAML` |
| Cache | `parquet` (via `pyarrow`) |
| CLI | `typer` |
| Restitution | `rich` (console) + CSV |
| Synthèse fondamentale | API Anthropic (`claude-sonnet-5`) + recherche web |

---

## 1. Architecture

### 1.1 Modules

```
                        ┌──────────────────────┐
                        │   config (YAML)      │   intervalles, poids, seuils,
                        │   validée par pydantic│   gates, tiers, sources
                        └──────────┬───────────┘
                                   │ (injectée partout)
                                   ▼
┌─────────────┐   symboles   ┌──────────────┐   OHLCV   ┌───────────────┐
│ data_fetcher│─────────────▶│   scanner    │◀─────────│  cache (disk) │
│ (exchangeInfo│              │ (orchestrateur)│         └───────────────┘
│  + klines)  │◀────────────▶│              │
└──────┬──────┘  gouverne    └──────┬───────┘
       │ le débit                   │ OHLCV par symbole × 5 TF
       ▼                            ▼
┌─────────────┐            ┌──────────────┐   colonnes indicateurs / TF
│ rate_limiter│            │  indicators  │──────────────┐
│ (poids/min) │            │ (wrap TA-Lib)│              │
└─────────────┘            └──────────────┘              ▼
                                        ┌──────────────────────────┐  score consolidé
                                        │ scoring_engine           │  + décomposition
                                        │ (score/TF → consolidation)│──────────┐
                                        └──────────────────────────┘          ▼
                              shortlist top N                          ┌──────────────┐
                        ┌────────────────────────────────────────────▶│  reporting   │
                        ▼                                              │ console/CSV  │
                 ┌──────────────┐                                      └──────┬───────┘
                 │ fundamentals │  données marché/tokenomics/TVL              │
                 │ (CoinGecko,  │                                             ▼
                 │  DefiLlama)  │                                    ┌──────────────────┐
                 └──────┬───────┘   appel API + recherche web        │ Claude Sonnet 5  │
                        └──────────────────────────────────────────▶│ (synthèse JSON)  │
                                                                     └──────────────────┘
```

| Module | Responsabilité |
|---|---|
| `config` | Charger et **valider** tous les paramètres (aucune constante en dur ailleurs) |
| `data_fetcher` | Lister l'univers (`exchangeInfo`) et récupérer les bougies (`klines`) sur les 5 TF |
| `rate_limiter` | Gouverneur de débit basé sur le poids Binance, backoff 429/418 |
| `cache` | Persister les bougies (parquet), rafraîchissement incrémental |
| `indicators` | Calculer les indicateurs par TF (couche mince au-dessus de TA-Lib) |
| `scoring_engine` | Score par TF → **consolidation multi-échelles** → score + décomposition |
| `scanner` | Orchestrer le pipeline, appliquer les *gates* |
| `fundamentals` | Enrichir la shortlist + appeler la synthèse Claude (Mode B) |
| `reporting` | Restituer (console + CSV) |

### 1.2 Flux de données

1. `config` charge et valide les paramètres.
2. `scanner` récupère l'univers `*/USDC` actif.
3. Par symbole et par TF : bougies via cache, sinon `data_fetcher` sous contrôle du `rate_limiter`.
4. `indicators` enrichit chaque DataFrame (par TF).
5. `scoring_engine` : score directionnel par TF → consolidation → score long 0–100 + décomposition.
6. `reporting` trie par score ; les *N* premiers passent par `fundamentals` puis la synthèse Claude.

### 1.3 Principes transverses

- **Configuration externalisée** : tout paramètre vit dans le YAML versionnable. Condition de l'auditabilité (l'outil n'est pas une boîte noire).
- **Séparation stricte des couches**, chacune testable isolément.
- **Journalisation** (`logging`) : requêtes, poids consommé, symboles/TF retirés (données insuffisantes), erreurs API.
- **Déterminisme** : à données identiques, score identique.

---

## 2. Récupération des données

### 2.1 Univers dynamique des paires /USDC

- **Endpoint :** `GET https://api.binance.com/api/v3/exchangeInfo`
- **Filtrage :** conserver chaque `symbol` où `quoteAsset == "USDC"`, `status == "TRADING"` et `isSpotTradingAllowed == true`. Jamais de liste codée en dur (l'univers change).
- **Poids élevé** (de l'ordre de 20) : **un seul appel par session**, mis en cache. `rateLimits` et `filters` sont conservés pour de futures fonctions.

### 2.2 Bougies (OHLCV)

- **Endpoint :** `GET /api/v3/klines` — `symbol`, `interval`, `limit` (**max 1000**), `startTime`/`endTime` (optionnels).
- **Format** (12 champs) : `[openTime, open, high, low, close, volume, closeTime, quoteVolume, nbTrades, takerBuyBase, takerBuyQuote, ignore]` → `DataFrame` typé (prix/volumes `float`, temps `datetime` UTC).
- **⚠️ Bougie en cours non clôturée** : **exclure systématiquement la dernière bougie** (vérifier `closeTime < now`) avant tout calcul — sinon indicateurs faussés et biais de look-ahead.
- **Profondeur (D2)** : `limit=1000` pour chaque paire/TF (une requête, poids 2, autant d'historique que disponible). Couverture par TF : H4 ≈ 166 j, H12 ≈ 500 j, 1D ≈ 2,7 ans, 1W/1M = tout l'historique disponible (plafonné par l'âge de la paire).

### 2.3 Pagination

- Nécessaire **seulement** pour un historique > 1000 bougies (backtest ultérieur). Pour le scan courant : **une requête par paire/TF, pas de pagination**.
- Pour un gros historique : préférer les dumps plats de `data.binance.vision` (pas de rate limit).

### 2.4 Rate limits

- **Limite : 6000 de poids/minute par IP** (partagée). `klines` = poids **2** ; `exchangeInfo` ≈ 20.
- **Suivi :** lire `X-MBX-USED-WEIGHT-1M` sur chaque réponse.
- **Erreurs :** `429` → backoff (respecter `Retry-After`) ; `418` → IP **bannie** (2 min à 3 jours) — à éviter absolument.
- **Gouverneur (`rate_limiter`)** : budget de poids/minute plafonné volontairement **sous 6000** (ex. 4000–4500), qui étale les requêtes et se recale sur l'en-tête.
- **Ordre de grandeur** : scan à froid ≈ 400 paires × 5 TF × poids 2 = **~4000 de poids**. Le **premier** scan (cache vide) est le plus lourd ; grâce au cache incrémental, les scans suivants sont négligeables. Le gouverneur sert surtout de garde-fou contre les **boucles de retry**.
- **Séquentiel + gouverneur d'abord** ; parallélisation bornée = optimisation ultérieure.

### 2.5 Cache (D3)

- Persister les bougies (`parquet` par `symbole_intervalle`).
- **Mode incrémental par défaut** : ne re-télécharger que les bougies manquantes depuis la dernière close en cache. Option **`--force-refresh`** pour ignorer le cache.
- Cohérent avec une cadence d'un scan par jour au maximum.

---

## 3. Indicateurs techniques

### 3.1 Librairie

**TA-Lib ≥ 0.6.x.** Wheels officiels sur PyPI (dont Windows) → `pip install TA-Lib` sans compilateur ni bibliothèque C. Référence du domaine, rapide (C), **seule** à fournir la suite complète des figures en chandeliers (`CDL*`). **Aucun indicateur réimplémenté à la main.**

### 3.2 Jeu d'indicateurs verrouillé (D4/D5)

Périodes par défaut = conventions crypto usuelles ; **toutes surchargeables** dans le YAML.

| Indicateur | Fonction TA-Lib | Paramètres | Rôle / catégorie |
|---|---|---|---|
| Moyennes mobiles | `EMA` | 20 / 50 / 200 | Tendance (alignement, croisements) |
| RSI | `RSI` | 14, seuils **30/70** | Momentum |
| MACD | `MACD` | 12 / 26 / 9 | Momentum / tendance |
| Parabolic SAR | `SAR` | 0,02 / 0,20 | Tendance / timing |
| Bandes de Bollinger | `BBANDS` | 20, ±2σ | Volatilité |
| Largeur des bandes | *(dérivé)* | `(sup−inf)/moyenne` | Volatilité / *squeeze* |
| Momentum (MOM) | `MOM` | **10** | Momentum |
| Volume vs MM volume | `SMA` sur volume | 20 | Confirmation |
| ADX (+ DI) | `ADX`, `PLUS_DI`, `MINUS_DI` | 14, seuil tendance 25 | **Filtre de régime / force de tendance** |
| Figures chandeliers | `CDLENGULFING`, `CDLHAMMER`, `CDLMORNINGSTAR`, `CDLSHOOTINGSTAR`, `CDLDOJI`… | — | Patterns |
| **ATR** | `ATR` | 14 | **Risque/sortie uniquement** (stops + colonne ATR%), **hors score** |

**Dérivés :** %B `(close−inf)/(sup−inf)`, largeur des bandes, ATR% `ATR/close`, alignement EMA `EMA20>EMA50>EMA200`.

**Note Momentum (MOM).** Sortie = différence de prix **non bornée** (unités de prix), donc non comparable d'une paire à l'autre : sa contribution au score se fait sur le **signe et la pente** (positif et croissant = momentum haussier), pas sur la valeur brute. La période **10** correspond au réglage courant de la version intégrée Binance/TradingView — **à confirmer/ajuster si votre chart affiche une autre longueur** (paramètre `indicators.momentum.period`).

### 3.3 Disponibilité des indicateurs selon le TF (point structurant)

Les bougies hautes sont rares sur un univers /USDC souvent jeune. Conséquence sur les indicateurs longs :

| Indicateur | H4 / H12 / 1D | 1W | 1M |
|---|---|---|---|
| RSI, MACD, SAR, Bollinger, ADX, MOM | ✅ disponibles | ✅ (paires > ~1 an) | ⚠️ grossiers (paires anciennes) |
| EMA 50 | ✅ | ✅ souvent | ⚠️ rare (~4 ans) |
| **EMA 200** | ✅ | ⚠️ partiel (~3,8 ans) | ❌ jamais (~17 ans) |

**Règles :** un indicateur dont la période dépasse l'historique disponible est **omis** (jamais remplacé par 0) ; `EMA200` n'est calculée que si ≥ 200 bougies (`gates.ema200_min_bars`). La période de chauffe (`NaN`) est exclue du scoring. Types `float64` contigus avant appel TA-Lib.

---

## 4. Moteur de scoring — consolidation multi-échelles (approche B)

### 4.1 Principe

Deux niveaux, tous deux transparents (jamais de boîte noire) :
1. **Score directionnel par TF** `s_t ∈ [−1, +1]` (règles explicites, ADX en modulateur).
2. **Consolidation descendante** : les TF hauts fixent le **biais**, les TF bas le **timing** ; le score final est un **score long 0–100**.

### 4.2 Score directionnel par TF

Pour chaque TF, chaque **règle** est une fonction pure qui lit les dernières valeurs d'indicateurs et ses paramètres, et renvoie une **contribution ∈ [−1, +1]** + un libellé lisible (ex. *« MACD croise au-dessus du signal »*).

```
s_t = Σ_catégorie ( poids_catégorie × Σ_règle ( poids_règle × contribution ) )
```

| Catégorie | Règles (exemples) |
|---|---|
| **Tendance** | alignement EMA20>50>200 ; SAR sous le prix ; prix > EMA200 |
| **Momentum** | MACD > signal & histogramme croissant ; RSI sortant de survente ; MOM positif et croissant |
| **Volatilité** | %B bas (proche/​sous bande inf.) ; *squeeze* (largeur faible → pré-cassure) |
| **Volume** | volume > MM volume |
| **Patterns** | `CDL*` haussier / baissier |

**Modulation ADX :** la catégorie **Tendance** est pondérée par un facteur croissant avec l'ADX (ADX élevé → tendance renforcée ; ADX faible → régime de range, la tendance pèse moins et le score s'appuie davantage sur momentum/volatilité). C'est ce qui remplace la logique d'archétypes : un **modèle unique**, orientable vers continuation / achat de repli / cassure en ajustant les poids de catégories, sans machinerie séparée.

### 4.3 Tiers de consolidation

| Tier | TF | Rôle |
|---|---|---|
| **Biais de fond** | `1M` (0,3) + `1W` (0,7) | Structure haussière/baissière à grande échelle |
| **Référence** | `1D` | Tendance que les trades suivent |
| **Déclenchement** | `H4` (0,6) + `H12` (0,4) | Timing d'entrée (opportunité long) |

- **Biais** `B` = classe (haussier / neutre / baissier) de `0,7·s_1W + 0,3·s_1M`.
- **Référence** `R` = classe de `s_1D`.
- **Déclenchement** `D` = `0,6·s_H4 + 0,4·s_H12` (seule la part **positive** alimente une opportunité long).
- Classification par bande neutre : `|s| < 0,1` → neutre (`classification.neutral_band`).

### 4.4 Formule du score consolidé (long)

```
score_brut = max(D, 0) × m
score_final = normalisation_0_100(score_brut) × facteur_contexte
```

`m` = **multiplicateur d'alignement**, appliqué au déclenchement selon (B, R) :

| Situation (vs déclenchement haussier) | `m` |
|---|---|
| Biais **et** référence haussiers | **1,0** |
| Un seul des deux haussier | 0,7 |
| Biais et référence neutres | 0,5 |
| Biais **ou** référence baissier (contradiction) | 0,2 |

**Dégradation gracieuse :**
- Un TF sous `gates.min_bars_per_tf` est **retiré** de son tier.
- Si `1M` insuffisant → le biais repose sur `1W` seul.
- Si `1M` **et** `1W` insuffisants → biais **indéterminé** : la paire reste scorée sur référence/déclenchement mais le score est minoré (`context_insufficient_factor`, ex. 0,5) et marquée « contexte insuffisant ».

Cette mécanique opérationnalise le principe « ne pas acheter contre la tendance supérieure » : un signal court haussier n'est retenu à plein que si le contexte ne le contredit pas.

### 4.5 Filtres durs (*gates*) — avant scoring

- **Liquidité (D6)** : `quoteVolume` 24 h **< 1 M USDC** → paire exclue (le volume est celui de la paire **/USDC** ; un token liquide surtout en /USDT mais peu en /USDC est écarté — voulu).
- **Historique** : aucune donnée exploitable sur le tier déclenchement → exclue.
- **Liste d'exclusion** manuelle (stablecoins entre eux, etc.).

### 4.6 Config par défaut (D7)

**Valeurs de départ robustes et rondes — à caler par observation, JAMAIS par optimisation sur backtest** (cf. §7).

```yaml
# === Univers & données ===
universe: { quote_asset: USDC, status: TRADING }
intervals: [4h, 12h, 1d, 1w, 1M]
history: { limit: 1000 }
cache: { mode: incremental }        # incremental | force_refresh

# === Gates ===
gates:
  min_quote_volume_24h: 1000000     # 1 M USDC / 24 h
  min_bars_per_tf: 50               # TF sous ce seuil = retiré
  ema200_min_bars: 200

# === Indicateurs (périodes) ===
indicators:
  ema: [20, 50, 200]
  rsi:      { period: 14, oversold: 30, overbought: 70 }
  macd:     { fast: 12, slow: 26, signal: 9 }
  sar:      { step: 0.02, max: 0.20 }
  bbands:   { period: 20, stddev: 2.0 }
  momentum: { period: 10 }          # MOM = close - close[10] (aligner sur votre chart)
  volume_ma:{ period: 20 }
  adx:      { period: 14, trend_threshold: 25 }
  atr:      { period: 14 }          # risque/sortie uniquement

# === Score par TF : poids des catégories ===
category_weights:
  trend: 0.35        # ADX module ce poids
  momentum: 0.30
  volatility: 0.15
  volume: 0.15
  patterns: 0.05

classification: { neutral_band: 0.10 }

# === Consolidation multi-échelles (approche B) ===
tiers:
  biais_fond:    { timeframes: { "1w": 0.7, "1M": 0.3 } }
  reference:     { timeframe: "1d" }
  declenchement: { timeframes: { "4h": 0.6, "12h": 0.4 } }
alignment_multiplier: { full_align: 1.0, partial: 0.7, neutral: 0.5, contradiction: 0.2 }
context_insufficient_factor: 0.5

# === Seuils de restitution ===
thresholds: { watch: 55, signal: 70 }

# === Fondamental (Mode B) ===
fundamentals:
  enabled: true
  top_n: 10
  model: claude-sonnet-5
  web_search: true
  sources:
    coingecko: { demo_key_env: COINGECKO_DEMO_KEY }
    defillama: { enabled: true }

# === Sortie ===
output: { format: csv, one_row_per: pair, timestamped: true }
```

Validation `pydantic` (types, bornes, cohérence). Tout comportement passe par ce fichier, pas par le code.

### 4.7 Sortie du moteur

Par paire : `score consolidé 0–100`, `niveau` (neutre / watch / signal), les **sous-scores par TF** (`s_1M … s_H4`), les **classes** biais/référence, le multiplicateur `m`, le **drapeau contexte**, et la **liste des règles déclenchées**. La décomposition est une exigence, pas une option.

---

## 5. Analyse fondamentale (Mode B)

Déclenchée **uniquement** sur la shortlist technique (`top_n`), pour ne pas solliciter les API tierces sur des centaines de tokens.

### 5.1 Données à récupérer

| Donnée | Intérêt | Source |
|---|---|---|
| Catégorie / narratif (L1, DeFi, IA, RWA…) | Contextualise le mouvement | CoinGecko |
| Capitalisation & rang | Taille, maturité | CoinGecko |
| Volume 24 h & ratio volume/cap | Liquidité réelle | CoinGecko |
| Supply (circulante / totale / max), FDV | Tokenomics de base, dilution | CoinGecko |
| TVL (si DeFi) | Usage réel du protocole | DefiLlama |
| Actualités / narratif récent | Catalyseurs, risques | Recherche web de Claude (FR+EN) |

### 5.2 Sources & accès (D8)

- **CoinGecko** — catégories, market data, supply : tier gratuit avec **clé démo** (variable d'environnement `COINGECKO_DEMO_KEY`) ; **vérifier les conditions/limites en vigueur** à l'implémentation.
- **DefiLlama** — TVL, gratuit, sans clé.
- **Actus/narratif** — pas d'API dédiée : la synthèse s'appuie sur la **recherche web de Claude** (Cryptoast / Journal du Coin en FR ; CoinDesk / The Block / Messari en EN).

### 5.3 Synthèse par Claude — Mode B (D9)

- Le script appelle l'**API Anthropic** (`claude-sonnet-5`) avec : les données structurées (CoinGecko + DefiLlama) **et** l'**outil de recherche web** activé pour l'actualité.
- **Prérequis :** clé API Anthropic (distincte de l'abonnement Claude) + coût par exécution assumé.
- **Sortie :** synthèse en **JSON structuré** (ex. `resume`, `points_positifs`, `points_vigilance`, `catalyseurs`, `sources`) réinjectée dans le rapport. Parsing défensif (strip des éventuels délimiteurs ``` avant `json.loads`).
- **Garde-fou :** une synthèse LLM peut halluciner et les actus peuvent être datées → chaque donnée est **horodatée** et **revérifiée à la source primaire** avant toute décision. La synthèse est une aide à la lecture, pas une vérité.

---

## 6. Restitution (D10)

### 6.1 Console

Tableau trié par score décroissant (`rich`) : symbole, score, niveau, sous-scores clés, prix, volume 24 h, ATR%. Coloration par niveau.

### 6.2 Export CSV — **une ligne par paire**

> **Cohérence avec D1 :** puisque le score est **consolidé** (un seul par paire), la sortie est **une ligne par paire** (format large), et non un format long avec colonne `timeframe`. Les sous-scores par TF deviennent des **colonnes**, ce qui conserve tout le détail pour une analyse ultérieure.

CSV horodaté (ex. `scan_20260705_1430.csv`). Colonnes suggérées :

`symbole, score, niveau, s_1M, s_1W, s_1D, s_H12, s_H4, classe_biais, classe_reference, multiplicateur_m, drapeau_contexte, regles_declenchees, close, quote_volume_24h, atr_pct, rsi_1d, adx_1d, horodatage`

(`enabled` colonnes ajustables ; l'idée est de conserver score consolidé **et** décomposition pour toute analyse de dérive.)

### 6.3 Dashboard (option, lot ultérieur)

**Streamlit** (tri/filtre interactifs, mini-graphiques par paire). Confort réel, mais la valeur est dans les couches 1 à 5.

---

## 7. Garde-fous

**Limites de l'analyse technique.** L'AT est **probabiliste, pas prédictive**. Sur crypto, signal très bruité : faible liquidité, *wash trading*, manipulations, nouvelles exogènes ignorées par construction. Un score élevé signale une **configuration historiquement favorable**, jamais une issue.

**Sur-optimisation — le risque n°1.**
- **Data snooping** : scanner des centaines de paires et retenir le sommet du classement fait remonter des configurations qui « marchent » **par hasard**.
- **Piège du réglage** : ce CDC introduit **beaucoup de paramètres** (indicateurs × 5 TF, poids de catégories, tiers, multiplicateurs). Plus de paramètres = plus de surface de sur-ajustement. **Ne réglez pas la config pour battre un backtest** ; gardez les valeurs par défaut robustes et n'ajustez qu'avec parcimonie, en observation.
- **Biais associés** : survivance (paires délistées absentes), look-ahead (bougie en cours) — d'où l'exclusion stricte de la bougie non clôturée (§2.2).

**Gestion du risque — obligatoire, hors périmètre de l'outil.**
- **Stop-loss** avant l'entrée, cohérent avec l'outil : basé **ATR** (ex. `entrée − k·ATR`), adapté à la volatilité de chaque paire.
- **Dimensionnement** : risquer un **% fixe et modeste** du capital par position (montant risqué = distance au stop × taille).
- **Ratio rendement/risque** défini (ex. ≥ 2:1) avant d'entrer.
- **Spot, long uniquement, pas de levier.**

**Nature de l'outil :** il produit des **signaux, pas des décisions**. Décision, exécution et risque restent **humains**. Ceci n'est pas un conseil en investissement. Respecter les rate limits et les **restrictions réglementaires** de Binance selon la juridiction.

---

## 8. Roadmap — lots incrémentaux pour Claude Code

Chaque lot est **livrable et testable seul**. Ne pas démarrer un lot sans le critère d'acceptation du précédent.

| Lot | Contenu | Critère d'acceptation |
|---|---|---|
| **0 — Fondations** | Arborescence, `venv`, dépendances, `config` + validation `pydantic`, `logging` | Une config invalide est rejetée avec un message clair |
| **1 — Couche données** | `data_fetcher` (univers + klines 5 TF), `rate_limiter`, `cache` incrémental | Univers `*/USDC` et bougies récupérés en respectant le débit ; bougie en cours exclue |
| **2 — Indicateurs** | `indicators` (wrap TA-Lib) + dérivés, gestion des indicateurs omis selon TF | Valeurs cohérentes vs référence (contrôle sur 1 paire) ; `NaN`/omissions gérés |
| **3 — Scoring** | (a) score directionnel par TF (ADX modulé) ; (b) **consolidation multi-échelles** + dégradation ; décomposition | Modifier un poids YAML change le score sans toucher au code ; décomposition lisible ; dégradation 1M→1W vérifiée |
| **4 — Restitution** | `reporting` console + CSV (une ligne/paire) | CSV réexploitable, tri par score correct |
| **5 — Fondamental (Mode B)** | `fundamentals` (CoinGecko + DefiLlama) + appel `claude-sonnet-5` avec recherche web, parsing JSON | Synthèse structurée par token, données horodatées, parsing robuste |
| **6 — Options** | Dashboard `streamlit`, backtest léger (`data.binance.vision`) | Selon priorités |

---

## Annexe — Décisions verrouillées

| Réf. | Décision | Valeur retenue |
|---|---|---|
| **D1** | Unités de temps + consolidation | `4h,12h,1d,1w,1M` · Approche **B** (contexte→déclenchement) |
| **D2** | Profondeur d'historique | `limit=1000` + dégradation gracieuse |
| **D3** | Cadence & rafraîchissement | Incrémental par défaut + `--force-refresh` |
| **D4** | Moyennes mobiles | EMA 20 / 50 / 200 |
| **D5** | Indicateurs & seuils | Jeu §3.2 · RSI 30/70 · ADX conservé · MOM 10 · MM dans le score |
| **D6** | Gate de liquidité | ≥ 1 M USDC / 24 h |
| **D7** | Config scoring | Défauts §4.6, à caler par observation (pas par backtest) |
| **D8** | Sources fondamentales | CoinGecko (clé démo) + DefiLlama · actus FR+EN via recherche web |
| **D9** | Synthèse | Mode B · `claude-sonnet-5` · JSON structuré |
| **D10** | Restitution | CSV unique, une ligne par paire |

---

*Document de spécification v1.1. Les éléments techniques (endpoints, poids Binance, wheels TA-Lib, longueur du Momentum, conditions CoinGecko) ont été vérifiés sur sources à la date d'édition ; ils évoluent — revérifier à l'implémentation. La longueur du Momentum (10) est à confirmer sur votre propre graphique.*
