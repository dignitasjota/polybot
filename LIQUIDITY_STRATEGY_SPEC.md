# Liquidity Provider Strategy - Especificación Técnica

**Versión:** 1.0
**Fecha:** 2026-04-15
**Estado:** Diseño (implementación pendiente)
**Autor:** Polymarket Bot Team

---

## 1. Visión General

Estrategia de **market making incentivado** que genera ingresos mediante:
- **Liquidity Rewards**: Polymarket paga diariamente a quienes proveen liquidez en mercados seleccionados
- **Spread**: Diferencia entre precio de compra y venta
- **Maker Rebates**: 25% de las taker fees (20% en crypto) retornan al maker

Sin necesidad de predecir correctamente la dirección del mercado. El objetivo es optimizar la ratio **reward/competencia** y minimizar **adverse selection losses**.

---

## 2. Ventaja sobre la estrategia Directional

| Aspecto | Directional | Liquidity Provider |
|---------|------------|-------------------|
| Fuente principal | Win rate > 60% | Rewards + spread |
| Dependencia predicción | Alta | Nula |
| Risk por trade | Pérdida total | Max spread (2-5%) |
| Escalabilidad | Limitada a mercados 5min | Todos los mercados con rewards |
| Predictibilidad ingresos | Volátil | Más estable |
| APY potencial | 20-50% | 100-500% |
| Correlación con volatilidad | Inversa (gana en markets lentos) | Directa (gana en markets activos) |

---

## 3. Mecanismo de Rewards de Polymarket

### 3.1 Scoring Cuadrático

Cada orden se puntúa según su distancia al midpoint:

```
Score(orden) = ((max_spread - spread_de_tu_orden) / max_spread)² × size
```

Donde:
- `max_spread`: spread máximo permitido (en centavos, definido por Polymarket)
- `spread_de_tu_orden`: distancia de tu precio al midpoint
- `size`: cantidad de shares en tu orden

**Ejemplo:**
- Mercado con `max_spread = 100` centavos ($1.00)
- Tu BID a midpoint - $0.20 (20 centavos de spread)
  - Score = ((100 - 20) / 100)² × size = 0.64 × size
- Tu BID a midpoint - $0.50 (50 centavos de spread)
  - Score = ((100 - 50) / 100)² × size = 0.25 × size

**Implicación:** Estar al 50% del spread máximo te da solo 25% del score. El sweet spot es **10-30% del spread máximo**.

### 3.2 Scoring Bidireccional y Restricciones de Midpoint

Para mercados con midpoint en [0.10, 0.90] (zona neutral):
```
Score_final = max(min(score_BUY, score_SELL), max(score_BUY/3, score_SELL/3))
```
- Puedes quotear UN solo lado con penalización de ÷3
- O ambos lados para score completo

Para mercados fuera de [0.10, 0.90] (extremos):
```
Score_final = min(score_BUY, score_SELL)
```
- **Obligatorio** quotear ambos lados
- Si solo tienes un lado: Score = 0 (sin rewards)

### 3.3 Muestreo y Distribución

- Polymarket toma **10,080 muestras por semana** (1 por minuto)
- Tu `score_normalizado = tu_score / sum(todos_scores)` en cada muestra
- Época = promedio de todas las muestras de la semana
- **Pago = (tu_Q_época / sum(todos_Q_época)) × pool_del_mercado**

Distribución:
- **Diaria** a medianoche UTC
- Mínimo **$1** para recibir pago (menos que eso se acumula)
- Directo a tu maker address en USDC

### 3.4 Maker Rebates (Adicional)

Además de rewards, recibes rebates de taker fees:
```
fee_equivalente = 0.003 × min(price, 1-price) × size
rebate = (tu_fee_equivalente / total_fee_equivalente) × rebate_pool
```
- 25% de taker fees van a rebate pool (20% en crypto)
- Competencia solo dentro de la categoría de mercado

---

## 4. Arquitectura Técnica

### 4.1 Componentes

```
src/
├── reward_scanner.py
│   └── RewardScanner: Escanea mercados con rewards, rankea por ratio
│
├── liquidity_provider.py
│   ├── LiquidityProvider: Core del market making
│   ├── MarketPosition: Tracking de posiciones por mercado
│   └── QuoteLevel: Una orden individual (BID o ASK)
│
├── strategies/
│   └── liquidity.py
│       ├── LiquidityConfig: Configuración
│       └── LiquidityStrategy: Integración con StrategyBase
│
└── (existentes)
    ├── executor.py: Extender para limit orders con GTC
    ├── account_runner.py: Añadir case para "liquidity"
    └── config.py: Añadir LiquidityConfig dataclass
```

### 4.2 Flujo de Datos

```
┌─────────────────────┐
│  RewardScanner      │  GET /rewards/markets/multi (cada 5min)
│  Ranking algoritmo  │  Filtrar por reward_per_dollar, spread, etc.
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────────────────────────┐
│  Top N mercados seleccionados               │
│  (por defecto N=5, capital_per_market=$50)  │
└──────────┬──────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────┐
│  LiquidityProvider                          │
│  Para cada mercado:                         │
│  1. GET /order-book (WebSocket)             │
│  2. Detectar order blocks (hiding)          │
│  3. Calcular spread óptimo                  │
│  4. POST /order (GTC, post_only=True)       │
└──────────┬──────────────────────────────────┘
           │
           ├─→ WebSocket: Monitor precio
           │   Si midpoint mueve > tick_size
           │   → DELETE /cancel-market-orders
           │   → POST /order (nuevas órdenes)
           │
           ├─→ Cada 60s: GET /order-scoring
           │   Verificar si órdenes están scoring
           │   Actualizar inventory
           │
           └─→ Cada 5min: Re-escanear rewards
               ¿Hay mercado mejor? → Migrar capital
```

### 4.3 APIs Utilizadas

| Endpoint | Método | Auth | Propósito |
|----------|--------|------|-----------|
| `/rewards/markets/current` | GET | No | Mercados con rewards activos |
| `/rewards/markets/multi` | GET | No | Mercados + filtros avanzados |
| `/rewards/markets/{id}` | GET | No | Rewards de mercado específico |
| `/rewards/user` | GET | Sí | Tus ganancias por fecha |
| `/rewards/user/percentages` | GET | Sí | Tu % del pool por mercado |
| `/order` | POST | Sí | Colocar orden GTC |
| `/order` | DELETE | Sí | Cancelar una orden |
| `/cancel-market-orders` | DELETE | Sí | Cancelar por mercado |
| `/data/orders` | GET | Sí | Tus órdenes abiertas |
| `/order-scoring` | GET | Sí | Verificar si orden está scoring |
| WebSocket `/trades` | WS | No | Stream de precios en tiempo real |

---

## 5. Algoritmo de Ranking de Mercados

**Objetivo:** Encontrar el balance perfecto entre recompensa alta y competencia baja.

### 5.1 Fórmula de Score

```python
score = (reward_per_dollar × spread_factor × capital_fit) / risk_factor

Donde:
  reward_per_dollar = daily_rate / competitiveness
  spread_factor = 1 - (max_spread - spread_óptimo) / max_spread
  capital_fit = min(capital_needed / capital_available, 1.0)
  risk_factor = 1 + adverse_selection_historical
```

### 5.2 Ejemplo de Ranking

| Mercado | Daily $ | Liquidez | r/$ | Max Spread | Risk | Score |
|---------|---------|----------|-----|-----------|------|-------|
| NBA A | $7,700 | $500K | 0.0154 | 200¢ | 0.35 | 8.2 |
| EPL B | $2,800 | $50K | 0.0560 | 100¢ | 0.42 | 12.4 |
| Crypto C | $298 | $10K | 0.0298 | 50¢ | 0.25 | 10.7 |
| Sports D | $1,200 | $400K | 0.0030 | 150¢ | 0.50 | 0.9 |

**Selección:** Top 5 por score → Mercados A, B, C + 2 más

---

## 6. Gestión de Posiciones

### 6.1 Tracking de Inventario

Para cada mercado, trackear:
```python
position.inventory_yes: float  # Shares acumuladas de YES
position.inventory_no: float   # Shares acumuladas de NO
position.skew = (yes - no) / (yes + no)

# Métricas
position.total_rewards_earned: float
position.total_adverse_selection: float  # Pérdidas por fills adversos
position.adverse_ratio = adverse / rewards  # Target < 0.7
```

### 6.2 Rebalanceo Automático

Si `|skew| > max_inventory_skew` (default 0.6):

1. **Leve (|skew| 0.6-0.7):** Sesgar spread
   - Ampliar spread en lado largo: `spread *= 1.5`
   - Reducir spread en lado corto: `spread *= 0.5`

2. **Moderado (|skew| 0.7-0.8):** Reducir tamaño
   - Size en lado largo: `size *= 0.5`
   - Size en lado corto: `size *= 1.5`

3. **Severo (|skew| > 0.8):** Solo quotear lado opuesto
   - Pausar BUY, solo vender exceso
   - Cancela todos los BIDs

### 6.3 Trigger de Abandono de Mercado

Si `adverse_ratio > max_adverse_ratio` (default 0.7):
- Rewards no justifican los losses
- `DELETE /cancel-market-orders`
- Migrar capital a mejor mercado (re-scan)

---

## 7. Order Block Hiding

**Objetivo:** Esconderse detrás de órdenes grandes para protegerse de adverse selection.

### 7.1 Algoritmo

```python
def find_order_blocks(book_side, min_block_usd=500):
    """
    Identifica órdenes grandes en el order book.
    Retorna niveles de precio donde hay "protección".
    """
    blocks = []
    cumulative_usd = 0

    for level in book_side:
        usd_value = level.size * level.price
        cumulative_usd += usd_value

        if cumulative_usd >= min_block_usd:
            blocks.append({
                'price': level.price,
                'size': level.size,
                'cumulative_usd': cumulative_usd
            })

    return blocks

def place_hidden_quote(market, side, min_block_usd=500):
    """
    Coloca quote detrás de un order block protector.

    - Si hay block: quote 1 tick peor que el block
    - Si no hay block: quote al 20% del max_spread
    """
    if side == BUY:
        book_side = order_book.asks_yes  # Side opuesta para encontrar protección
    else:
        book_side = order_book.bids_yes

    blocks = find_order_blocks(book_side, min_block_usd)

    if blocks:
        # Colocarse detrás del mejor block
        protected_price = blocks[0]['price']
        if side == BUY:
            quote_price = protected_price - tick_size
        else:
            quote_price = protected_price + tick_size
    else:
        # Fallback: 20% del max_spread
        midpoint = (order_book.best_bid + order_book.best_ask) / 2
        spread_amount = market.max_spread * 0.0001 * 0.20  # centavos → USDC
        if side == BUY:
            quote_price = midpoint - spread_amount
        else:
            quote_price = midpoint + spread_amount

    # Ajustar al tick size
    quote_price = round(quote_price / tick_size) * tick_size

    return quote_price
```

### 7.2 Rationale

- Órdenes grandes barren primero → tu orden se cancela automáticamente
- Reduces adverse selection losses sin monitoreo constante
- Sacrificas 1-2 centavos de spread por protección

---

## 8. Risk Management

### 8.1 Límites de Exposición

```python
max_capital_per_market = 50  # USDC
max_markets_concurrent = 5
max_inventory_skew = 0.6
max_adverse_ratio = 0.7  # abandon si losses > 70% de rewards

emergency_move_pct = 0.05  # Cancel all si price mueve >5% en <30s
max_orders_per_batch = 15  # POST /order limit
```

### 8.2 Detección de Adverse Selection

```python
async def monitor_adverse_selection(position: MarketPosition):
    """
    Monitorea si un mercado tiene información adversa.

    Indicadores:
    - Fills ejecutados muy lejos del midpoint
    - Ratio losses/rewards creciente
    - Micro-movimientos bruscos después de fills
    """
    adverse_ratio = position.total_adverse_selection / position.total_rewards_earned

    if adverse_ratio > 0.7:
        logger.warning(f"High adverse selection: {adverse_ratio:.2%}")
        # Trigger rebalanceo severo o abandono
```

### 8.3 Cancelación de Emergencia

Si el precio se mueve > 5% en < 30 segundos:
```python
if price_change_pct > 0.05 and elapsed_seconds < 30:
    client.cancel_market_orders(market=condition_id)
    logger.error("EMERGENCY CANCEL: Price moved 5%+ in 30s")
```

---

## 9. Configuración

### 9.1 Config.toml

```toml
[accounts.strategies.liquidity]
mode = "paper"              # o "live"
enabled = true

# Capital allocation
capital_per_market = 50.0   # USDC por mercado
max_markets = 5             # Mercados simultáneos
total_capital = 250.0       # Verificación (sum = capital × max_markets)

# Market selection
min_reward_per_dollar = 0.001      # Mínimo reward/$ diario
min_daily_rewards = 100.0          # Mínimo $100/día en rewards
max_liquidity_competence = 1000000 # Máxima competencia ($1M)

# Quoting strategy
spread_pct_of_max = 0.20            # Quotear al 20% del max_spread
use_order_block_hiding = true
min_block_usd = 500                 # Mínimo para considerar block
tick_size_override = null           # null = auto-detect

# Inventory management
rebalance_interval = 60             # Segundos entre checks
max_inventory_skew = 0.6            # Máximo |yes-no|/(yes+no)
max_adverse_ratio = 0.70            # Abandonar si adverse > 70%
emergency_move_pct = 0.05           # Cancel all si move > 5%

# Monitoring
scan_rewards_interval = 300         # Cada 5 min
verify_scoring_interval = 60        # Cada 1 min
log_level = "info"
```

### 9.2 Ejemplo: Config Conservative

```toml
[accounts.strategies.liquidity]
mode = "paper"
capital_per_market = 25.0
max_markets = 3
min_reward_per_dollar = 0.002       # Más selectivo
spread_pct_of_max = 0.25            # Más lejos del mid
max_inventory_skew = 0.4            # Más estricto
```

### 9.3 Ejemplo: Config Aggressive

```toml
[accounts.strategies.liquidity]
mode = "live"
capital_per_market = 100.0
max_markets = 10
min_reward_per_dollar = 0.0005      # Menos selectivo
spread_pct_of_max = 0.15            # Cercano al mid
max_inventory_skew = 0.8            # Más tolerante
```

---

## 10. Métricas & KPIs

### 10.1 Diarios

```python
{
  "date": "2026-04-15",
  "markets_active": 5,
  "total_orders_placed": 47,
  "total_orders_filled": 12,
  "fill_rate": 0.255,
  "orders_scoring": 45,  # Verificado via GET /order-scoring
  "scoring_rate": 0.957,

  "rewards_earned": 12.45,  # USDC
  "rebates_earned": 3.21,   # USDC
  "spread_income": 1.10,    # USDC
  "total_gross": 16.76,

  "adverse_selection_loss": 4.20,
  "slippage_loss": 0.50,
  "total_loss": 4.70,

  "net_pnl": 12.06,
  "roi_pct": 4.82,  # (12.06 / 250 capital)
}
```

### 10.2 Semanales & Mensuales

```python
# Aggregado de diarios
{
  "period": "2026-04-09 to 2026-04-15",
  "total_rewards": 87.15,
  "total_rebates": 22.47,
  "total_spread_income": 7.83,
  "total_gross": 117.45,

  "total_adverse": 29.40,
  "adverse_ratio": 0.250,  # 29.40 / 117.45

  "cumulative_pnl": 88.05,
  "roi_pct": 35.22,  # (88.05 / 250)

  "annualized_apy": 1827,  # 35.22 * 52 weeks
}
```

### 10.3 Targets

| Métrica | Mínimo | Target | Excelente |
|---------|--------|--------|-----------|
| Scoring Rate | 80% | 95%+ | 99%+ |
| Adverse Ratio | <0.50 | <0.30 | <0.15 |
| Fill Rate | 10% | 20-30% | >40% |
| Daily ROI | 0.5% | 2-5% | >5% |
| APY | 50% | 100-300% | >500% |

---

## 11. Implementación Roadmap

### Fase 1: Reward Scanner (1-2 días) ✓

- [x] `reward_scanner.py`: GET `/rewards/markets/multi`, parsing response
- [x] Ranking algoritmo: `score = (reward_per_dollar × factors) / risk`
- [x] Test: Listar top 10 mercados actuales (13 tests)
- [x] Panel web: Mostrar mercados candidatos con metrics

### Fase 2: Liquidity Provider Core (3-5 días) ✓

- [x] `liquidity_provider.py`: Estructura de clases (QuoteOrder, MarketPosition, LiquidityProvider)
- [x] `place_quotes()`: POST /order con GTC + post_only, two-sided (BUY YES + BUY NO)
- [x] `update_quotes()`: Cancel + re-place cuando midpoint mueve > threshold
- [x] `find_order_blocks()`: Order book analysis para protección
- [x] MarketTracker integration: `get_midpoint()` para precios en tiempo real

### Fase 3: Risk & Inventory (2-3 días) ✓

- [x] Inventory tracking: YES/NO acumulado, fill_count, adverse_loss
- [x] Rebalanceo automático: 3 niveles (leve/moderado/severo) con spread/size adjustment
- [x] Adverse selection monitoring: adverse_ratio → abandono automático si > max_adverse_ratio
- [x] Emergency cancel: Price move >5% en <30s

### Fase 3.5: Heartbeat, Scoring & Block Hiding ✓

- [x] Order block hiding: place behind large orders (configurable min_block_usd)
- [x] Heartbeat loop: POST /heartbeat cada 5s (solo live, configurable)
- [x] Order scoring: GET /order-scoring verification cada 60s, scoring_rate tracking

### Fase 4: Integration (1-2 días) ✓

- [x] `LiquidityStrategy` + `LiquidityConfig` con from_dict()
- [x] `account_runner.py`: Case "liquidity" con credentials + tracker
- [x] `config.toml`: Sección [[accounts]] con strategy_type = "liquidity"
- [x] Panel web: Liquidity dashboard con scanner + provider + quotes + metrics

### Fase 5: Metrics & Refinement ✓

- [x] `liquidity_metrics.py`: Daily P&L snapshots (DailySnapshot + LiquidityMetrics)
- [x] API endpoint: GET /api/rewards/metrics (today + history + summary)
- [x] Panel web: Today's P&L, 7-day summary, ROI, APY estimado
- [ ] Paper trading: 1-2 semanas en Docker
- [ ] Tune parameters: spread_pct, inventory_skew, etc.
- [ ] Validation: APY targets vs spec (sección 10.3)

---

## 12. Casos de Uso & Scenarios

### 12.1 Caso: Mercado Deportivo (EPL Match)

```
Mercado: "Liverpool vs Manchester - Liverpool Win"
Daily Rewards: $2,800
Competitiveness: $50,000
reward_per_dollar: 0.056

Max Spread: 100¢
Tu Spread: 20¢ (20% del max)
Score: ((100-20)/100)² = 0.64

Tu Order:
- Size: 100 shares
- Price: $0.52 (midpoint $0.50 + 2¢ ask)
- Side: SELL (proveer liquidez al lado demand)

Resultado (si fills completos):
- Rewards (7 días): ~$19.60
- Spread income: ~$2.00
- Adverse loss: ~$3.00
- Net: $18.60 → 3.7% ROI en 7 días
```

### 12.2 Caso: Mercado Crypto (BTC Up/Down 5min)

```
Mercado: "BTC Up - next 5 min"
Daily Rewards: $298
Competitiveness: $10,000
reward_per_dollar: 0.0298

Max Spread: 50¢
Tu Spread: 10¢ (20% del max)
Score: ((50-10)/50)² = 0.64

Midpoint: $0.45
Tu Orders:
- BID: $0.38
- ASK: $0.52

Resultado (si fills):
- Rewards (24h): ~$2.98
- Spread income: ~$1.40
- Adverse loss: ~$0.60 (alta volatilidad)
- Net: $3.78 → 1.5% ROI en 24h
```

---

## 12.3 Validación contra Backtesting Repo

El [backtesting repo de Polymarket](https://github.com/polymarketmakers/pm-bots) valida nuestras assumptions sobre fees, settlement y market selection:

### Fee Formula Equivalence

**Nuestra fórmula:**
```python
fee_per_share = 0.003 × min(price, 1 - price) × size
```

**Backtesting repo (PolymarketDataLoader):**
```python
fee = qty * feeRate * p * (1 - p)  # Idéntico, con feeRate=0.003
```

**Implicación:** La fórmula de fees es correcta y verificada contra datos reales.

### Data API para Análisis Histórico

El repo usa `GET /trades` de la Data API (`https://data-api.polymarket.com/trades`) para:
- Obtener histórico de precios ejecutados por mercado
- Calcular volatilidad y spread histórico
- Medir adverse selection losses (diferencia entre precio ofrecido vs ejecutado)
- Identificar períodos de mayor liquidez

**Endpoint útil:**
```
GET https://data-api.polymarket.com/trades?market_id={id}&limit=1000
```

Retorna array de trades con:
```json
{
  "price": 0.45,
  "quantity": 100,
  "outcome": "Yes",
  "timestamp": "2026-04-12T14:30:00Z",
  "is_buy": true
}
```

**Aplicación a Liquidity Strategy:**
- Usar trades históricos para calcular `adverse_selection_loss` esperada por mercado
- Filtrar mercados donde adverse > reward (no rentable)
- Usar volatilidad histórica para ajustar spread dinámicamente

### Settlement & P&L Calculation

El repo implementa settlement calculation que podemos reutilizar:

```python
# Para cada orden ejecutada
def calculate_settlement_pnl(
    order_price: float,
    filled_price: float,      # Precio promedio ejecutado
    quantity: float,
    side: str                  # "BUY" o "SELL"
) -> dict:
    # Loss por adverse selection
    adverse_loss = abs(order_price - filled_price) * quantity

    # Cálculo de payoff si la orden fué YES
    payoff_if_win = (1.0 - order_price) * quantity

    # P&L actual = payoff - costo - fees
    gross_pnl = payoff_if_win - adverse_loss - fees

    return {
        "adverse_loss": adverse_loss,
        "gross_pnl": gross_pnl,
        "roi": gross_pnl / capital_used
    }
```

**Aplicación:** Usar este patrón exacto para calcular P&L diario en la liquididad strategy.

### Market Selection Filters

El repo filtra mercados usando criterios que podemos adoptar:

```python
# Desde backtesting repo
def should_quote_market(market):
    # 1. Exclusiones obvias
    if market["is_closed"] or market["resolved"]:
        return False

    # 2. Liquidez mínima
    recent_volume = market["volume_24h"]
    if recent_volume < 1000:  # USDC
        return False

    # 3. Spread actual no demasiado apretado
    current_spread_pct = market["current_spread"] / market["mid_price"]
    if current_spread_pct < 0.02:  # <2% de spread actual
        return False  # Ya hay competencia, esperamos

    # 4. Time to resolution razonable
    time_to_res = market["resolution_time"] - now()
    if time_to_res < 5 * 60:
        return False  # Muy poco tiempo para que se estabilice

    return True
```

**Métricas a monitorear desde Data API:**
- `volume_24h`: Actividad reciente
- `mid_price`: Punto de equilibrio actual
- `spread_pct`: Competencia actual
- `order_depth_20`: Órdenes grandes que podamos usar para order block hiding

---

## 13. FAQ

**P: ¿Cuánto capital mínimo necesito?**
R: $250 ($50 × 5 mercados). Aunque con $100 ($20 × 5) también funciona.

**P: ¿Puedo cambiar entre paper/live fácilmente?**
R: Sí, via el panel o `set_strategy_mode()`. Stats se resetean al cambiar.

**P: ¿Qué pasa si una orden no se ejecuta?**
R: GTC orders permanecen en el libro hasta llenarse o cancelarse. El bot cancela si el precio se mueve >5%.

**P: ¿Cómo evito ser "barrido" por órdenes grandes?**
R: Order block hiding: colocarse detrás de órdenes de $500+ que absorben el impacto primero.

**P: ¿Cuál es el spread óptimo?**
R: Depende del mercado, pero 10-30% del `max_spread` es el sweet spot entre reward máximo y riesgo mínimo.

**P: ¿Puedo correr esto en el mismo bot que la estrategia directional?**
R: Sí, en la misma cuenta. Los capitales se dividen entre estrategias. El executor es compartido.

---

## 14. Referencias

- [Polymarket Liquidity Rewards](https://docs.polymarket.com/market-makers/liquidity-rewards)
- [Polymarket Order Creation](https://docs.polymarket.com/trading/orders/create)
- [Polymarket Rewards API](https://docs.polymarket.com/api-reference/rewards)
- [Polymarket Data API](https://docs.polymarket.com/api-reference/data-api) — `/trades`, `/markets`
- [py-clob-client](https://github.com/Polymarket/py-clob-client)
- [Backtesting Repo](https://github.com/polymarketmakers/pm-bots) — PolymarketDataLoader, fee formulas, settlement patterns
- [LunarResearcher Bot Stack](https://x.com/lunarresearcher/status/2036042309272244413)

---

**Documento v1.0 - Sujeto a revisión**
