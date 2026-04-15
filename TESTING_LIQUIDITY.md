# Testing Liquidity Strategy (Phases 1-5)

This document covers testing procedures for the complete Liquidity Strategy implementation: Scanner (Phase 1) through Metrics (Phases 4-5).

## Quick Verification

First, run the quick setup checker:

```bash
python3 verify_liquidity_setup.py
```

Expected output:
```
✅ ALL CHECKS PASSED — Ready for paper/dry_run testing
```

## Unit Tests

All phases are covered by 64 unit tests:

```bash
# Run all liquidity tests
python3 -m pytest tests/test_liquidity_metrics.py tests/test_liquidity_provider.py -v

# Run only metrics tests (Phases 4-5)
python3 -m pytest tests/test_liquidity_metrics.py -v

# Run only provider tests (Phases 2-3.5)
python3 -m pytest tests/test_liquidity_provider.py -v

# Run with coverage
python3 -m pytest tests/test_liquidity*.py --cov=src.liquidity_provider --cov=src.liquidity_metrics
```

### Test Coverage

| Phase | Tests | Coverage |
|-------|-------|----------|
| Phase 1 (Scanner) | Integrated in strategy tests | RewardScanner ✓ |
| Phase 2 (Provider) | 23 tests | Pricing, paper mode, lifecycle ✓ |
| Phase 3 (Risk & Inventory) | 12 tests | Skew, rebalancing, adverse ✓ |
| Phase 3.5 (Heartbeat/Scoring/Block) | 7 tests | Paper mode no-ops, fields ✓ |
| Phase 4-5 (Metrics) | 19 tests | Daily snapshots, summary, APY ✓ |

## Paper Mode Testing (No credentials needed)

Paper mode simulates the entire strategy without connecting to Polymarket:

### 1. Local Testing (Python REPL)

```python
from src.liquidity_provider import LiquidityProvider
from src.liquidity_metrics import LiquidityMetrics
from src.strategies.liquidity import LiquidityConfig
from unittest.mock import Mock

# Create config for paper mode
config = LiquidityConfig(
    mode="paper",
    capital_per_market=50.0,
    max_markets=3,
    use_heartbeat=False,  # Optional: disable heartbeat for testing
)

# Create provider
metrics = LiquidityMetrics(total_capital=500.0)
provider = LiquidityProvider(
    config=config,
    credentials=Mock(),
    tracker=Mock(),
    metrics=metrics,
)

# In paper mode, ClobClient is not initialized
assert provider._clob_client is None

# Pricing calculation works
bid, ask = provider._calculate_prices(midpoint=0.50, max_spread=0.05, inventory_skew=0.0)
print(f"Quote: bid=${bid:.4f} ask=${ask:.4f}")

# Paper trades can be simulated
provider.record_fill(
    condition_id="0xtest",
    fill_price=bid,
    size=10.0,
    is_yes=True,
    order_id="paper_order_1",
)

# Metrics are tracked
today = metrics.get_today()
print(f"Today: {today['orders_filled']} fills, {today['adverse_loss']:.2f} adverse loss")
```

### 2. Docker Paper Mode (Full stack)

1. **Set execution mode in config:**

```toml
# config/config.toml
[[accounts]]
name = "liquidity_rewards"
enabled = true
strategy_type = "liquidity"
execution_mode = "paper"  # ← Key setting

[accounts.liquidity]
scan_interval = 60        # Faster for testing
capital_per_market = 50.0
max_markets = 3
```

2. **Start bot in Docker:**

```bash
docker compose build && docker compose up -d

# View logs
docker compose logs -f polymarket_bot
```

3. **Access panel:**

Open browser: `http://localhost:8080`
- Username: `admin`
- Password: (from `PANEL_PASSWORD` env var)

4. **Monitor liquidity strategy:**

Navigate to `/panel/liquidity`:
- **Scanner section**: Shows top reward markets (real data from CLOB API)
- **Provider section**: Shows active quotes (simulated in paper mode)
- **Active Quotes table**: Simulated order placement
- **Today's P&L**: Simulated metrics
- **7-Day Summary**: Daily tracking

5. **Expected behavior in paper mode:**

- ✅ Scanner runs every 60s and fetches real reward markets from CLOB API
- ✅ Provider tries to place orders (simulated, no actual orders sent)
- ✅ Metrics track simulated fills, rewards, adverse selection
- ✅ No ClobClient initialization (no credentials needed)
- ✅ No balance changes (all trades are paper)
- ✅ Heartbeat loop runs but does nothing (paper mode is no-op)

### 3. Dry-Run Mode (Validation without execution)

Dry-run initializes ClobClient and validates orders but does NOT send them:

```toml
[[accounts]]
name = "liquidity_rewards"
execution_mode = "dry_run"  # ← Initializes client, validates, no-send
```

**Requirements:**
- Valid `PRIVATE_KEY` env var
- Valid wallet type (`WALLET_TYPE`)
- Valid `POLYMARKET_PROXY_ADDRESS` (if using Magic Link or Gnosis Safe)

**Testing:**
```bash
export PRIVATE_KEY=0x...
export WALLET_TYPE=2
export POLYMARKET_PROXY_ADDRESS=0x...
docker compose up -d

# Logs will show:
# "dry_run mode: order validation passed but NOT sent"
# "clobclient_initialized"
```

## Live Mode Testing (Real trades)

⚠️ **WARNING**: Live mode places real orders and uses real USDC. Do NOT enable without thorough testing.

### Prerequisites

1. **Valid credentials:**
   - Private key with test funds
   - API keys and passphrases (auto-derived or explicitly set)
   - Correct wallet type for your Polymarket account

2. **Start in paper mode first:**
   - Run 1-2 days of paper trading
   - Verify metrics accuracy (fill rates, rewards, P&L calculations)
   - Validate quote pricing logic

3. **Switch to dry-run:**
   - Change `execution_mode = "dry_run"` in config
   - Run another session
   - Verify no actual orders appear on blockchain

4. **Then switch to live (if confident):**
   - Change `execution_mode = "live"` in config
   - Start with small capital: `capital_per_market = 10.0`
   - Monitor closely for first 2-3 days

## Test Cases by Phase

### Phase 1: Scanner

**Unit test**: Scanner is tested in strategy integration tests.

**E2E test** (Docker):
```
1. Start bot in paper mode
2. Observe logs for "scanning_reward_markets"
3. Check /api/rewards/markets endpoint returns market list
4. Verify JSON structure has: daily_rate, competitiveness, spread, score
```

### Phase 2: Provider Core

**Unit tests**: 23 tests cover pricing, paper mode, lifecycle.

**E2E test** (Docker):
```
1. Start bot in paper mode
2. Check /panel/liquidity → Provider section
3. Verify "Status: RUNNING" (paper mode should show running)
4. Verify "Active Markets" shows 0-N positions
5. Check orders placed count increases (if scanner found markets)
```

**Manual test** (Python):
```python
from src.liquidity_provider import LiquidityProvider

provider = LiquidityProvider(...)
bid, ask = provider._calculate_prices(0.50, 0.05, 0.0)
assert 0.0 <= bid < 0.50 < ask <= 1.0
print("✓ Pricing logic correct")
```

### Phase 3: Risk & Inventory

**Unit tests**: 12 tests cover skew calculation, rebalancing, adverse selection.

**E2E test** (Docker):
```
1. Simulate multiple fills on same side (via logs/metrics)
2. Check /api/rewards/metrics → metrics_today → inventory_skew
3. For high skew (>0.6), verify spread adjustment in quotes
4. Verify "Adv. Ratio" stat reflects adverse losses
```

**Manual test** (Python):
```python
# Inventory skew
pos.fills_yes = 70.0
pos.fills_no = 30.0
assert abs(pos.inventory_skew - 0.4) < 0.01  # (70-30)/(70+30) = 0.4

# Severe skew quoting
bid, ask = provider._calculate_prices(0.50, 0.10, skew=0.85)
# Should only quote rebalancing side (e.g., only bid)
```

### Phase 3.5: Heartbeat, Scoring, Block Hiding

**Unit tests**: 7 tests (paper mode no-ops, field presence).

**E2E test** (Docker):
```
1. Check /panel/liquidity → Heartbeat & Scoring section
2. Verify "Heartbeat: OFF" in paper mode (or "ON" with 0 count if enabled)
3. Verify "Scoring Rate: 0%" in paper mode
4. Check logs for "heartbeat_loop_started" (if use_heartbeat=true)
```

### Phase 4-5: Metrics & KPIs

**Unit tests**: 19 tests cover snapshots, daily rollover, summary, APY.

**E2E test** (Docker):
```
1. Check /panel/liquidity → TODAY'S P&L section
2. Verify fields: Net P&L, Rewards, Adverse, Fill Rate, Daily ROI
3. Check /panel/liquidity → 7-DAY SUMMARY (after 1-2 days of data)
4. Verify: Cumulative P&L, Total Rewards, Total Adverse, Adv. Ratio, ROI, APY
5. Call /api/rewards/metrics endpoint
6. Verify JSON: today, history (7 days), summary (aggregated)
```

**Manual test** (Python):
```python
from src.liquidity_metrics import LiquidityMetrics

metrics = LiquidityMetrics(total_capital=1000.0)
metrics.record_rewards(10.0)
metrics.record_fill(adverse_amount=2.0)

today = metrics.get_today()
assert today["net_pnl"] == 8.0  # 10 - 2
assert today["roi_pct"] == 0.8  # 8/1000 * 100

summary = metrics.get_summary(days=7)
assert summary["days"] == 1
assert summary["cumulative_pnl"] == 8.0
```

## Verification Checklist

- [ ] All 64 unit tests pass: `pytest tests/test_liquidity*.py`
- [ ] Setup verification passes: `python3 verify_liquidity_setup.py`
- [ ] Config has liquidity account with all parameters
- [ ] Paper mode starts without errors
- [ ] Scanner fetches markets every scan_interval seconds
- [ ] Provider section shows in panel (status: RUNNING)
- [ ] Metrics track fills, rewards, adverse loss
- [ ] Daily P&L section displays correctly
- [ ] 7-day summary aggregates data correctly
- [ ] `/api/rewards/metrics` endpoint returns valid JSON
- [ ] Emergency cancel button works in panel
- [ ] Dry-run mode initializes ClobClient (if testing)

## Troubleshooting

### Scanner returns 0 markets

```bash
# Check logs
docker compose logs polymarket_bot | grep "scanning_reward_markets"

# Market list changes frequently, re-run manually
curl -s http://localhost:8080/api/rewards/markets?limit=10 | python3 -m json.tool
```

### Provider shows 0 active markets

```bash
# Normal in paper mode if scanner found 0 markets
# Or if capital_per_market is too large for available markets

# Reduce thresholds in config:
min_daily_rate = 0.5        # Lower minimum reward
capital_per_market = 20.0   # Less capital per market
max_markets = 10            # More markets allowed
```

### Metrics not updating

```bash
# Check if provider is actually recording fills (should be 0 in paper)
# Manually record a fill:
provider.record_fill(
    condition_id="0xtest",
    fill_price=0.48,
    size=10.0,
    is_yes=True,
    order_id="manual_test",
)

# Verify metrics update:
today = metrics.get_today()
print(f"Fills: {today['orders_filled']}")  # Should be 1
```

### Heartbeat loop errors in live mode

```bash
# Verify API key/secret/passphrase:
export POLYMARKET_API_KEY=...
# Or let it auto-derive from PRIVATE_KEY

# Check logs for "heartbeat_failed"
docker compose logs polymarket_bot | grep heartbeat
```

## Next Steps (Phase 5+)

Once paper mode is validated:

1. **Monitor 7 days of data** to validate APY calculation
2. **Parameter tuning**: adjust spreads, quote refresh interval, inventory skew threshold
3. **Dry-run testing**: initialize ClobClient with test wallet
4. **Live launch**: start with 1-2 days of low capital, scale up gradually

See `LIQUIDITY_STRATEGY_SPEC.md` section 11 (Roadmap) for Phase 5+ milestones.
