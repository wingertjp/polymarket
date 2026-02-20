# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires a `.env` file with:
```
PRIVATE_KEY=<your Polygon wallet private key>
```

## Running

```bash
# Live order book viewer (no auth required)
python main.py data

# Sniper (requires PRIVATE_KEY in .env)
python main.py snipe
```

## Architecture

Single-file bot (`main.py`) targeting the Polymarket CLOB (Central Limit Order Book) on Polygon. It operates on 5-minute BTC Up/Down binary markets.

**Two modes:**

- **`data` mode** — No auth. Uses the Gamma API to discover the current 5-minute market window (slug derived from clock: `btc-updown-5m-<unix_ts_rounded_to_300s>`), then opens a WebSocket to `wss://ws-subscriptions-clob.polymarket.com/ws/market` to stream live order book updates. Renders a terminal UI showing bids/asks for both the Up and Down tokens side-by-side. Automatically rolls over to the next window when the countdown hits zero.

- **`snipe` mode** — Requires auth. Builds an L2 `ClobClient` using `PRIVATE_KEY`, derives API credentials via `create_or_derive_api_creds()`, then monitors each market window via WebSocket. When the midpoint of either the Up or Down token reaches `SNIPE_PROB` (0.95) and fewer than `SNIPE_TIME` (120) seconds remain, fires a FOK market buy for `SNIPE_AMOUNT` (1.0) USDC. Rolls over to new market windows automatically.

**Key constants** (top of file, tune as needed):
- `SNIPE_AMOUNT = 1.0` — USDC per trade
- `SNIPE_PROB = 0.95` — midpoint threshold to trigger buy
- `SNIPE_TIME = 120` — only trigger if fewer than this many seconds remain

**APIs used:**
- `https://gamma-api.polymarket.com/events` — market discovery by slug
- `https://clob.polymarket.com` — CLOB REST (order placement, midpoint, market info)
- `wss://ws-subscriptions-clob.polymarket.com/ws/market` — real-time order book feed

**WebSocket message types:**
- `event_type == "book"` — full book snapshot for a token
- messages with `"price_changes"` key — incremental book updates (add/remove levels)

## Wallet setup (one-time, on-chain)

### Tokens
Polymarket CLOB uses **USDC.e** (bridged): `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
**Not** native USDC (Circle): `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`

Binance now withdraws native USDC on Polygon — must swap to USDC.e first.

### Swap native USDC → USDC.e
Use Uniswap V3 SwapRouter directly (`0xE592427A0AEce92De3Edee1F18E0157C05861564`), pool fee=100.
DEX aggregator APIs (1inch, ParaSwap, OpenOcean, KyberSwap) require API keys or block direct calls — don't bother.

### Contracts to approve for USDC.e
```
CTF Exchange:     0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
NegRisk Exchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a
NegRisk Adapter:  0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
```

> **Important** : toujours approuver avec `amount = max uint256` (pas un montant fixe).
> BTC Up/Down utilise le CTF Exchange — l'allowance se consomme à chaque trade et tombe à 0 après ~5 ordres si on avait approuvé seulement 5 USDC. Avec max uint256, c'est permanent.

### Full setup flow
1. Export private key from Polymarket (Profile → Wallet → Export)
2. Withdraw USDC from Polymarket web UI to EOA wallet on Polygon
3. Get MATIC for gas (Binance → Polygon, ~$1–2 is plenty)
4. Swap native USDC → USDC.e via Uniswap V3 (fee=100)
5. Approve the 3 contracts above for USDC.e
6. Run `python main.py snipe`

### Reliable Polygon RPC
`https://polygon-bor-rpc.publicnode.com`

## Réclamer les gains (CTF redemption)

Les gains ne sont **pas** automatiquement crédités. Après qu'un marché se résout, les tokens ERC1155 restent dans ton wallet et doivent être réclamés manuellement via `redeemPositions`.

### Contrats
```
CTF Token (Gnosis):  0x4D97DCd97eC945f40CF65F87097ACe5EA0476045  ← appeler redeemPositions ici
CTF Exchange:        0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E  ← NE PAS utiliser pour redeem
```

### Comment ça marche
1. Chaque trade reçoit des tokens ERC1155 (un par outcome : Up idx=1, Down idx=2)
2. Quand le marché se résout, `payoutNumerators[idx] / payoutDenominator = 1` pour le gagnant
3. Appeler `redeemPositions(collateral, parentCollectionId, conditionId, indexSets)` sur le CTF Token contract
4. Le contrat brûle les tokens et renvoie des USDC.e

### Paramètres de redeemPositions
```
collateral         = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174  (USDC.e)
parentCollectionId = 0x0000000000000000000000000000000000000000000000000000000000000000
conditionId        = récupéré depuis le marché Polymarket (champ condition_id)
indexSets          = [2] pour Up (idx=1), [4] pour Down (idx=2)
                     (indexSet = 2^(idx-1) ... Up=2^0=1? Non: idx=1→indexSet=2, idx=2→indexSet=4 sur Polymarket)
```

> **Attention** : indexSets sur Polymarket — outcome Up (token_id pair=0) → `[2]`, outcome Down (token_id pair=1) → `[4]`. À vérifier via `balanceOf(wallet, tokenId)` sur le CTF Token contract.

### Vérifier les positions à réclamer
```python
# Vérifier si une condition est résolue
# eth_call sur CTF Token : payoutDenominator(conditionId) → != 0 si résolu
# eth_call sur CTF Token : balanceOf(wallet, tokenId) → montant à réclamer
```

### Fréquence recommandée
Vérifier manuellement après chaque session de snipe, ou automatiser via un script de redemption (voir l'historique de session pour l'implémentation complète).
