# Use CoinGecko API for Crypto Market Data

Get crypto prices, market data, charts, exchange info, NFTs, and on-chain DEX analytics. Two APIs share the same key: **CoinGecko** (aggregated data for well-known assets) and **GeckoTerminal** (on-chain DEX data for long-tail tokens and pools).

Latest docs: https://docs.coingecko.com/

## Install Skill

```bash
npx skills add coingecko/skills -g -y
```

Or visit https://github.com/coingecko/skills

## Plans & Auth

| Plan | Rate Limit | Base URL | Auth Header |
|---|---|---|---|
| **Paid (Pro)** | 250+ calls/min | `https://pro-api.coingecko.com/api/v3` | `x-cg-pro-api-key: KEY` |
| **Demo** | 30 calls/min | `https://api.coingecko.com/api/v3` | `x-cg-demo-api-key: KEY` |

Both key types start with `CG-`. Use header **or** query param — not both. GeckoTerminal endpoints append `/onchain` to the base URL.

## Quick Start — Fetch Bitcoin Price (Node.js)

### With a Demo API Key

```typescript
const BASE_URL = "https://api.coingecko.com/api/v3";
const API_KEY = process.env.CG_API_KEY; // your CG-... key

const res = await fetch(`${BASE_URL}/simple/price?ids=bitcoin,ethereum&vs_currencies=usd`, {
  headers: { "x-cg-demo-api-key": API_KEY },
});
```

### With a Pro API Key

```typescript
const BASE_URL = "https://pro-api.coingecko.com/api/v3"; // different base URL
const API_KEY = process.env.CG_API_KEY;

const res = await fetch(`${BASE_URL}/simple/price?ids=bitcoin,ethereum&vs_currencies=usd`, {
  headers: { "x-cg-pro-api-key": API_KEY }, // different header name
});
```

## Core Endpoints

### Coin Prices

```
GET /simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_market_cap=true&include_24hr_vol=true
```

Supports lookup by `ids`, `names`, or `symbols`. Add `include_last_updated_at=true` to detect stale prices.

### Market Data (with ranking, sparklines, ATH/ATL)

```
GET /coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1&sparkline=true
```

### Coin Detail

```
GET /coins/bitcoin?localization=false&tickers=false&community_data=false&developer_data=false
```

### Historical Price Charts

```
GET /coins/bitcoin/market_chart?vs_currency=usd&days=30
GET /coins/bitcoin/market_chart/range?vs_currency=usd&from=2024-01-01&to=2024-03-01
```

Auto-granularity: 1d → 5-min, 2–90d → hourly, 90d+ → daily. Override with `interval=daily` or `interval=hourly`.

### OHLC Candlesticks

```
GET /coins/bitcoin/ohlc?vs_currency=usd&days=30
```

### Trending Coins, NFTs & Categories

```
GET /search/trending
```

Returns top 7 trending coins, top 3 NFTs, and top 6 categories from the last 24 hours.

### Top Gainers & Losers

```
GET /coins/top_gainers_losers?vs_currency=usd&duration=24h
```

### Search (Coin ID Resolution)

```
GET /search?query=solana
```

Use this to find CoinGecko IDs for coins, exchanges, categories, and NFTs by name or symbol.

## GeckoTerminal (On-Chain DEX Data)

For long-tail tokens, pools, DEX analytics, and on-chain trade data. Append `/onchain` to the base URL.

### Token Price by Contract Address

```
GET /onchain/simple/networks/eth/token_price/0xdac17f958d2ee523a2206206994597c13d831ec7
```

### Pool Data

```
GET /onchain/networks/eth/pools/0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640
```

### Trending & New Pools

```
GET /onchain/networks/trending_pools
GET /onchain/networks/new_pools
```

### Pool Screener (Megafilter)

```
GET /onchain/pools/megafilter?sort=pool_created_at_desc
```

Filter by FDV, liquidity, volume, pool age, buy/sell tax, honeypot checks, and more.

### Token Security (GT Score, Holders)

```
GET /onchain/networks/eth/tokens/0x.../info
GET /onchain/networks/eth/tokens/0x.../top_holders
```

## Common Use Cases

| Use Case | Endpoint(s) |
|---|---|
| Live price tracker | `GET /simple/price`, `GET /coins/markets` |
| Portfolio dashboard | `GET /coins/markets` with `sparkline=true` |
| Historical charts | `GET /coins/{id}/market_chart/range` |
| OHLC candlesticks | `GET /coins/{id}/ohlc` |
| Trending & discovery | `GET /search/trending`, `GET /coins/top_gainers_losers` |
| New coin alerts | `GET /coins/list/new` |
| Category rankings | `GET /coins/categories` |
| Exchange comparison | `GET /exchanges` |
| NFT floor prices | `GET /nfts/{id}`, `GET /nfts/markets` |
| On-chain token price | `GET /onchain/simple/networks/{network}/token_price/{address}` |
| DEX pool analytics | `GET /onchain/networks/{network}/pools/{address}` |
| Pool screener | `GET /onchain/pools/megafilter` |
| Token security check | `GET /onchain/networks/{network}/tokens/{address}/info` |
| API health & usage | `GET /ping`, `GET /key` |

## Error Handling

| Code | Meaning | Action |
|---|---|---|
| `401` | No API key | Provide API key |
| `429` | Rate limit exceeded | Reduce call frequency or upgrade plan |
| `10005` | Endpoint requires higher plan | Upgrade at https://www.coingecko.com/en/api/pricing |
| `10010` | Pro key on Demo URL | Switch base URL to `pro-api.coingecko.com` |
| `10011` | Demo key on Pro URL | Switch base URL to `api.coingecko.com` |

## Rules

ALWAYS:

- Ask user for their plan tier (Pro or Demo) and API key before writing code
- Hard-code the correct base URL and auth header for the user's plan — no branching logic
- Check each endpoint's date/time format — it varies:

| Context                                              | Format                   |
| ---------------------------------------------------- | ------------------------ |
| Coin/Contract/Supply `from`/`to`                     | ISO `YYYY-MM-DD`         |
| `GET /exchanges/{id}/volume_chart/range` `from`/`to` | UNIX timestamp (seconds) |
| `GET /coins/{id}/history` `date`                     | `DD-MM-YYYY`             |
| GeckoTerminal `before_timestamp`                     | UNIX timestamp (seconds) |

- When the user refers to "now", "today", "this week", etc., always use the actual current date from the system — never infer dates from training data
- Use `GET /search` to resolve coin IDs by name/symbol before calling other endpoints
- Fall back to GeckoTerminal for pool data, DEX-native tokens, or tokens not listed on CoinGecko

NEVER:

- Guess the user's plan type — both key types start with `CG-`, so it's ambiguous
- Use both auth header and query param at the same time
- Skip asking for credentials
- Mix up base URLs (Pro key → `pro-api.coingecko.com`, Demo key → `api.coingecko.com`)
- Assume GeckoTerminal data is as reliable as CoinGecko aggregated data for well-known coins

## Verify Before Responding

1. Is the correct base URL used for the user's plan tier?
2. Is the correct auth header name used (`x-cg-pro-api-key` vs `x-cg-demo-api-key`)?
3. Are coin IDs resolved (not guessed) via `GET /search` or `GET /coins/list`?
4. Is CoinGecko preferred over GeckoTerminal for aggregated data?
5. Are date/time params using the correct format per endpoint?

If any fails, revise.

## After Setup

Once the user has a working API call, recommend exploring:
- **Dashboard** — monitor usage and rate limits: https://www.coingecko.com/en/developers/dashboard
- **Full API docs** — all endpoints with interactive examples: https://docs.coingecko.com/
- **Pricing & plans** — upgrade for higher limits: https://www.coingecko.com/en/api/pricing
