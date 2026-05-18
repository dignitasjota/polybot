# Polymarket Multi-Strategy Trading Bot

## Resumen

Bot de trading automatizado para Polymarket que ejecuta cuatro estrategias independientes en paralelo:
- **Directional**: detecta oportunidades de arbitraje en mercados crypto de 5 minutos
- **Copy Trade**: copia trades de wallets rentables con sistema de roles (primary/confirmation)
- **Completeness Arbitrage**: compra YES+NO cuando la suma < $1.00 para profit garantizado (sin riesgo)
- **Liquidity** (Fases 1-5): market making con rewards — scanner, provider, risk management, métricas

Corre en Docker (VPS Alemania), desplegado via Portainer. Soporta **paper trading** (simulado) y **live trading** (real contra Polymarket CLOB).

---

## Arquitectura

```
src/
  main.py              # Bot: orquestador principal, arranca cuentas + web
  config.py            # Dataclasses de configuración (cargadas desde TOML)
  account_runner.py    # Runner independiente por cuenta (directional, copy_trade, liquidity)
  detector.py          # ClosingArbitrageDetector: detecta oportunidades directional
  copy_trader.py       # CopyTrader: monitorea wallets y genera oportunidades copy
  reward_scanner.py    # RewardScanner: escanea CLOB API por mercados con rewards
  liquidity_provider.py # LiquidityProvider: market making engine (quotes, inventory, risk)
  liquidity_metrics.py  # LiquidityMetrics: daily P&L tracking y KPIs
  completeness_scanner.py # CompletenessScanner: arbitraje YES+NO < $1.00
  weather_scanner.py   # WeatherScanner: predicción meteorológica con ensemble ECMWF
  fees.py              # Fees centralizadas V2: taker_fee(), maker_rebate(), por categoría
  executor.py          # Executor: ejecuta trades (paper/dry_run/live)
  market_tracker.py    # MarketTracker: estado in-memory de mercados via WebSocket
  websocket_client.py  # WebSocket a Polymarket para precios en tiempo real
  gamma_client.py      # Cliente REST para Gamma API (descubrimiento de mercados)
  price_checker.py     # Consulta precio Binance para confirmar dirección crypto
  logger.py            # Setup structlog (JSON a archivo + consola)
  db.py                # SQLite: usuarios, wallet_overrides, audit_log
  web/
    __init__.py         # create_app(): factory de aiohttp con Jinja2
    session.py          # Sesión HMAC-SHA256 con cookie (sin dependencias externas)
    middleware.py       # Auth middleware (redirige a /login si no autenticado)
    auth.py             # Handlers login/logout
    routes_dashboard.py # Dashboard principal (read-only, auto-refresh)
    routes_api.py       # APIs JSON (/api/report, /api/opportunities)
    routes_panel.py     # Panel de control (copy-trade, directional, settings)
    config_manager.py   # Hot-reload: muta config in-memory + persiste a TOML
  templates/            # Jinja2 templates (estética terminal: fondo oscuro, neon)
    base.html           # Layout con nav tabs
    login.html
    dashboard.html
    panel/
      copy_trade.html   # Gestión wallets + parámetros copy trading
      directional.html  # Kill switch + market filter + parámetros directional
      weather.html      # Bet sizing, edge thresholds, timing + forecasts activos
      settings.html     # Cambio password + audit log
  static/
    htmx.min.js         # htmx vendored (interactividad sin SPA)
config/
  config.toml           # Configuración principal (modificable en caliente via panel)
data/
  panel.db              # SQLite (usuarios, wallet overrides, audit log)
logs/
  bot.jsonl             # Logs estructurados JSON
```

---

## Modos de ejecución

Cada cuenta tiene un `execution_mode` independiente:

| Modo | Descripción |
|------|-------------|
| `paper` | Simulado. No interactúa con Polymarket. Balance y trades son ficticios. |
| `dry_run` | Inicializa el cliente CLOB, valida órdenes, pero no las envía. |
| `live` | Trading real contra Polymarket CLOB V2. Usa balance pUSD real. |

### Cambio de modo (paper ↔ live)

Se cambia desde el panel web (Settings). Al cambiar de modo:

1. **Paper → Live**: Se inicializa el cliente CLOB V2 con credenciales, se ejecuta `cancel_all()` para liberar órdenes huérfanas, se refresca el balance pUSD real, se **resetean todas las stats y apuestas del período paper** (bets, wins, losses, P&L), y se establece el balance real como nuevo `starting_balance`. Esto garantiza una vista limpia de la operativa live.
2. **Live → Paper**: Se resetean stats y se vuelve al `simulated_balance` del config.
3. **Cualquier cambio de modo** (paper↔dry_run↔live): Todas las estrategias resetean sus stats y trades internos via `set_mode()`. Cada modo empieza con datos limpios.
4. **Métodos de reset por estrategia**:
   - `CopyTrader.reset_stats()`, `ClosingArbitrageDetector.reset_stats()`, `Executor.reset_trades()` — directional/copy
   - `CompletenessScanner.reset_stats()` — borra trades, cooldowns, contadores
   - `LiquidityProvider.reset_stats()` + `LiquidityMetrics.reset()` — borra posiciones, órdenes, métricas diarias
   - `WeatherScanner.reset_stats()` — borra trades, cache forecasts, contadores, persiste estado vacío

### Balance en modo live

- El executor consulta `get_balance_allowance(COLLATERAL)` de la API CLOB para obtener pUSD libre (antes USDC.e, cambiado en CLOB V2).
- **pUSD libre ≠ portfolio total**: solo devuelve pUSD disponible para apostar, no el valor de posiciones abiertas.
- Se refresca automáticamente cada hora (`BALANCE_REFRESH_INTERVAL = 3600s`).
- Se refresca forzosamente al cambiar a modo live.
- Si el balance real es $0, el bot no puede colocar órdenes (el copy_trader lo bloquea con `bet_size < 0.10`).

---

## Credenciales y tipos de wallet

Polymarket soporta tres tipos de wallet. El bot debe configurarse con el tipo correcto según cómo accedes a Polymarket:

| Tipo | `WALLET_TYPE` | `signature_type` | Descripción |
|------|---------------|-------------------|-------------|
| **Magic Link** | `magic_link` (default) | 1 (POLY_PROXY) | Login con email. Polymarket crea un proxy wallet. |
| **MetaMask EOA** | `metamask` | 0 (EOA) | Wallet externa SIN proxy. El propio wallet firma y tiene los fondos. |
| **MetaMask + Gnosis Safe** | `2` o `gnosis_safe` | 2 (GNOSIS_SAFE) | MetaMask conectado a Polymarket. Polymarket crea un proxy Gnosis Safe. **Recomendado para MetaMask.** |

### Cómo saber qué tipo tengo

- **Magic Link**: Te logueaste en Polymarket con email (Google, etc.)
- **MetaMask + Gnosis Safe (tipo 2)**: Te logueaste con MetaMask y depositaste fondos a través de la UI de Polymarket. En Polymarket → Settings → Account ves una dirección proxy diferente a la de MetaMask. **Este es el caso más común con MetaMask.**
- **MetaMask EOA (tipo 0)**: Usas MetaMask directamente sin proxy. Los fondos están en tu wallet de MetaMask, no en un proxy. Raro en la práctica.

> **Regla simple**: Si en Polymarket Settings ves una dirección diferente a la de tu wallet de MetaMask, usa tipo `2` (GNOSIS_SAFE). Si es la misma, usa tipo `0` (EOA).

### Configuración por tipo de cuenta

#### Tipo 1: Magic Link (email login)

**Dónde obtener los datos en Polymarket:**
- **Private Key**: Settings → Advanced → Export Private Key
- **Proxy Address**: Settings → Account (la dirección que ves ahí)
- **API Key**: Settings → Claves API del relayer → `relayer_api_key`
- **Secret/Passphrase**: Se auto-derivan de la private key (no hace falta configurarlos)

**Variables de entorno:**
```yaml
- WALLET_TYPE=magic_link              # o "1"
- PRIVATE_KEY=0x...                   # Exportada desde Polymarket Settings
- POLYMARKET_PROXY_ADDRESS=0x...      # Dirección de Settings → Account
- POLYMARKET_API_KEY=...              # Opcional: relayer_api_key (se auto-deriva si vacío)
```

**config.toml:**
```toml
[accounts.credentials]
private_key_env = "PRIVATE_KEY"
signature_type_env = "WALLET_TYPE"
proxy_address_env = "POLYMARKET_PROXY_ADDRESS"
api_key_env = "POLYMARKET_API_KEY"
```

---

#### Tipo 2: MetaMask + Gnosis Safe (recomendado para MetaMask)

**Dónde obtener los datos en Polymarket:**
- **Private Key**: Exportar desde MetaMask → Account Details → Export Private Key (debe tener 64 caracteres hex después de `0x`)
- **Proxy Address**: Polymarket → Settings → Perfil → la dirección que aparece (puede decir "solo para uso de API")
- **API Key**: Settings → Claves API del relayer → `relayer_api_key`
- **Builder keys**: Settings → Códigos del constructor → `builder_api_key`, `builder_secret`, `builder_passphrase`
- **Secret/Passphrase**: Se auto-derivan de la private key (no hace falta configurarlos)

**Variables de entorno:**
```yaml
- WALLET_TYPE=2                       # GNOSIS_SAFE
- PRIVATE_KEY=0x...                   # Exportada desde MetaMask (64 chars hex)
- POLYMARKET_PROXY_ADDRESS=0x...      # Dirección del Perfil en Polymarket
- POLYMARKET_API_KEY=...              # relayer_api_key de Polymarket Settings
- BUILDER_API_KEY=...                 # Del "Códigos del constructor"
- BUILDER_SECRET=...                  # Del "Códigos del constructor"
- BUILDER_PASSPHRASE=...              # Del "Códigos del constructor"
```

**config.toml:**
```toml
[accounts.credentials]
private_key_env = "PRIVATE_KEY"
signature_type_env = "WALLET_TYPE"
proxy_address_env = "POLYMARKET_PROXY_ADDRESS"
api_key_env = "POLYMARKET_API_KEY"
builder_key_env = "BUILDER_API_KEY"
builder_secret_env = "BUILDER_SECRET"
builder_passphrase_env = "BUILDER_PASSPHRASE"
```

> **IMPORTANTE**: Con MetaMask, los fondos están en el proxy de Polymarket, NO en tu wallet de MetaMask. Si en MetaMask ves $0 pero en Polymarket ves saldo, es correcto — el pUSD (antes USDC.e) está en el proxy.

---

#### Tipo 0: MetaMask EOA (sin proxy)

**Solo usar si NO tienes proxy en Polymarket** (la dirección en Polymarket Settings es la misma que en MetaMask).

**Variables de entorno:**
```yaml
- WALLET_TYPE=0                       # o "metamask"
- PRIVATE_KEY=0x...                   # Exportada desde MetaMask
- POLYMARKET_API_KEY=...              # relayer_api_key
- BUILDER_API_KEY=...                 # Del "Códigos del constructor"
- BUILDER_SECRET=...                  # Del "Códigos del constructor"
- BUILDER_PASSPHRASE=...              # Del "Códigos del constructor"
```

> **Nota**: Con EOA, el funder es tu propia dirección MetaMask. No se necesita `POLYMARKET_PROXY_ADDRESS`. Necesitas POL (MATIC) en tu wallet para pagar gas.

---

### Resumen rápido de variables por tipo

| Variable | Magic Link (1) | MetaMask+Safe (2) | MetaMask EOA (0) |
|----------|:-:|:-:|:-:|
| `WALLET_TYPE` | `magic_link` | `2` | `metamask` |
| `PRIVATE_KEY` | ✅ (de Polymarket) | ✅ (de MetaMask) | ✅ (de MetaMask) |
| `POLYMARKET_PROXY_ADDRESS` | ✅ (Settings) | ✅ (Perfil) | ❌ |
| `POLYMARKET_API_KEY` | Opcional* | Opcional* | Opcional* |
| `POLYMARKET_SECRET` | Auto-deriva* | Auto-deriva* | Auto-deriva* |
| `POLYMARKET_PASSPHRASE` | Auto-deriva* | Auto-deriva* | Auto-deriva* |
| `BUILDER_API_KEY` | ❌ | ✅ | ✅ |
| `BUILDER_SECRET` | ❌ | ✅ | ✅ |
| `BUILDER_PASSPHRASE` | ❌ | ✅ | ✅ |

\* Se auto-derivan de la private key si no se configuran manualmente.

### Flujo de credenciales

1. El executor lee la private key desde env var (`PRIVATE_KEY` o `COPY_PRIVATE_KEY`)
2. Intenta leer API key/secret/passphrase de env vars
3. Si no están definidas → **auto-deriva** las API keys desde la private key usando `ClobClient.create_or_derive_api_key()`
4. El `signature_type` se determina desde env var `WALLET_TYPE` (o `COPY_WALLET_TYPE` para la cuenta copy)

### Configuración en `CredentialsConfig`

```python
@dataclass
class CredentialsConfig:
    private_key_env: str = "PRIVATE_KEY"
    api_key_env: str = "POLYMARKET_API_KEY"
    api_secret_env: str = "POLYMARKET_SECRET"
    passphrase_env: str = "POLYMARKET_PASSPHRASE"
    signature_type_env: str = "WALLET_TYPE"     # env var para tipo de wallet
    proxy_address_env: str = "POLYMARKET_PROXY_ADDRESS"
    signature_type: int = -1                     # -1=auto-detect desde env var
```

Auto-detección en `__post_init__`:
- Lee `signature_type_env` (e.g. `COPY_WALLET_TYPE`)
- Si vacío, fallback a `WALLET_TYPE`
- Si vacío, default `magic_link` → `signature_type=1`
- Valores aceptados: `magic_link`/`poly_proxy`/`1` → 1, `gnosis`/`gnosis_safe`/`safe`/`2` → 2, `metamask`/`eoa`/`0` → 0

---

## Estrategia 1: Directional (Closing Arbitrage + Up/Down)

### Closing Arbitrage
Compra tokens que cotizan a $0.97+ cuando el mercado está cerca de resolverse. Si el token gana, paga $1.00. Margen = $1.00 - precio - fees.

**Flujo:**
1. `GammaClient` descubre mercados activos (tag="crypto", poll cada 30s)
2. `WebSocketClient` se suscribe a precios en tiempo real
3. En cada update de precio, `detector.check(token_id)` evalúa O(1) ese mercado
4. Si precio >= probabilidad mínima según tier temporal → oportunidad
5. `Executor` registra el paper trade o coloca orden real (según modo)

**Tiers de probabilidad** (cuanto menos tiempo queda, menos probabilidad exigimos):
- < 5 min: min 0.97
- < 15 min: min 0.98
- < 30 min: min 0.99
- < 1h: min 0.99

### Up/Down Directional
Para mercados tipo "Will BTC go up in the next 5 minutes?":
1. Detector ve un mercado Up/Down con precio entre `min_price_updown` (default 0.10) y `max_price` (default 0.60)
2. Consulta precio actual de Binance via `PriceChecker`
3. Si el cambio de precio en Binance confirma la dirección con buffer >= `min_buffer_pct` (default 3%) → oportunidad
4. Límite de `max_concurrent_bets` (default 3) para evitar drawdowns correlacionados

### Parámetros configurables (hot-reload via panel)
| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| kill_switch | false | Detiene todo el trading inmediatamente |
| min_margin_net | 0.008 | Margen mínimo por share después de fees (Up/Down) |
| min_margin_closing | 0.005 | Margen mínimo para closing arb (separado porque gross margin a p=0.98 es solo $0.02) |
| max_price | 0.60 | Precio máximo para bets Up/Down |
| min_price_updown | 0.10 | Precio mínimo para bets Up/Down (rechaza tokens near-zero como $0.001) |
| min_buffer_pct | 0.03 | Cambio mínimo en Binance (%) para confirmar dirección |
| max_concurrent_bets | 3 | Máximo bets simultáneas en ventana de 5 min |
| max_bet_per_trade | 200 | Tope absoluto en $ por trade |
| max_daily_loss | 100 | Stop-loss diario |
| crypto_only | true | Solo mercados con tag "crypto" |
| max_markets_monitored | 200 | Máximo mercados suscritos via WebSocket |

---

## Estrategia 2: Copy Trade

Monitorea wallets de traders rentables en Polymarket y copia sus trades BUY.

### Flujo
1. `CopyTrader._poll_loop()` consulta la API de actividad cada `poll_interval_ms` (500ms)
2. Para cada wallet habilitada, busca trades recientes tipo BUY
3. Aplica filtros: precio mínimo, latencia máxima, concurrent bets
4. Aplica lógica de roles (ver abajo)
5. Registra el paper trade o coloca orden real (según modo) y monitorea resolución

### Sistema de Roles de Wallets

Cada wallet tiene un **rol** almacenado en SQLite (`wallet_overrides.role`):

**Primary**: Se copia directamente. Es la fuente de señal principal.

**Confirmation**: Se le asigna una wallet primary (`confirms_wallet`). Lógica:
- Si la primary asignada apostó **mismo lado** en el mismo mercado → **copia** (modo "double", señal reforzada)
- Si la primary asignada apostó **lado opuesto** → **skip** (conflicto, evita apuesta cruzada que garantiza pérdida en un lado)
- Si la primary **no apostó** en ese mercado → **copia** (modo "solo", la confirmation opera independiente)

**Motivación del sistema de roles**: Sin roles, si vidarx apuesta YES y para888 apuesta NO en el mismo mercado, copiamos ambos y uno siempre pierde ($-5). Con roles, la confirmation (para888) se bloquea solo cuando contradice a su primary (vidarx).

### Wallets actuales
| Wallet | Alias | Rol | Notas |
|--------|-------|-----|-------|
| 0x2d8b... | vidarx | Primary | Trader principal, opera a precios medios ($0.38-$0.60) |
| 0x674b... | para888 | Confirmation (→ vidarx) | Solo copia si no contradice a vidarx |
| 0x45bc... | 0xbbc5z | Disabled | Closing arbitrage a $0.93+ — redundante con estrategia directional |

### Parámetros configurables (hot-reload via panel)
| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| fixed_bet_size | 5.0 | Cantidad fija en $ por trade copiado |
| poll_interval_ms | 500 | Frecuencia de consulta a la API de actividad |
| min_price | 0.40 | Ignora trades con precio < 0.40 (WR muy bajo debajo) |
| max_concurrent_bets | 3 | Máximo bets abiertas en ventana de 5 min |
| max_bet_per_trade | 50 | Tope absoluto por trade |
| max_daily_loss | 50 | Stop-loss diario |
| max_latency_ms | 120000 | Ignora trades más antiguos que esto (120s en paper) |

### Settlement
El CopyTrader resuelve bets de dos formas:
1. **REDEEM events**: Consulta la API de actividad buscando REDEEMs de las wallets target
2. **CLOB API fallback**: Si pasaron >5 min sin REDEEM, consulta el estado del mercado directamente

---

## Estrategia 3: Completeness Arbitrage (YES+NO < $1.00)

Arbitraje sin riesgo direccional: cuando la suma de best asks de todos los outcomes es < $1.00, comprar todos y hacer redeem por $1.00.

### Flujo
1. `MarketTracker` recibe precios via WebSocket (compartido con directional)
2. Cada 5s (scan loop) o en cada price update (reactivo via WebSocket callback): evalúa `best_ask_YES + best_ask_NO` de cada mercado
3. Si `1.00 - sum > fees + gas` → oportunidad detectada
4. Compra ambos tokens en paralelo (órdenes enviadas simultáneamente)
5. Si ambas compras exitosas → redeem inmediato = $1.00 garantizado
6. Si una falla → cancela las demás (partial execution recovery)

### Profit garantizado
```
profit = (1.00 × shares) - (price_YES × shares) - (price_NO × shares) - fees - gas
```

### Umbrales por categoría de fees
| Categoría | Fee 2 lados (p≈0.50) | Gap mínimo rentable |
|-----------|---------------------|---------------------|
| Geopolitics | $0.000 | ~$0.005 (solo gas) |
| Sports | ~$0.008 | ~$0.012 |
| Crypto | ~$0.036 | ~$0.040 |

### Parámetros configurables
| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| scan_interval | 5.0 | Segundos entre scans periódicos (gaps son fugaces) |
| min_profit_per_share | 0.005 | Min $0.005 neto por share para ejecutar |
| min_shares | 5.0 | Min shares para que valga la pena |
| max_cost_per_trade | 50.0 | Máximo $ por trade |
| cooldown_s | 30.0 | Segundos antes de reintentar mismo mercado |
| category | crypto | Categoría de fees (auto-detectada si viene del keyset endpoint) |

### Auto-detección de categoría de fees
El endpoint keyset de Gamma API no devuelve `feeSchedule` pero sí `feeType` (e.g. `"crypto_fees_v2"`, `"sports_fees_v2"`, `"politics_fees"`, `"general_fees"`, `"culture_fees"`, `"weather_fees"`) y `feesEnabled` (bool). `gamma_client.py` infiere el `fee_rate` a partir de `feeType`+`feesEnabled` cuando `feeSchedule` está ausente, usando `fee_rate_from_fee_type()` de `src/fees.py`. Esto permite evaluar correctamente mercados de geopolítica (0% fees, gap mínimo ~$0.005) que antes se rechazaban erróneamente al asumir fees de crypto (7.2%, gap mínimo ~$0.04).

### Detección reactiva
Además del scan periódico, el scanner recibe callbacks del WebSocket cada vez que un precio cambia. Esto permite detectar gaps efímeros que desaparecen en <5 segundos. Se comparte el WebSocket dispatch con el detector directional via `_ws_dispatch`.

**Wiring para cuentas standalone**: Si la cuenta completeness no comparte runner con directional, `account_runner.py` wirea explícitamente el callback `scanner.check` al WebSocket client en el bloque `elif strat_name == "completeness"`.

### Fallback de sizing
Cuando el WebSocket solo envía eventos `best_bid_ask` (frecuentes) sin `book` completo (raro), el order book puede estar vacío pero con `best_ask_yes/no > 0`. En ese caso, el scanner usa fallback sizing: `size = max_cost_per_trade / best_ask_price` para no descartar oportunidades válidas.

### Ejecución atómica
Las órdenes para ambos tokens se envían en paralelo (`asyncio.gather`). Si una falla, se cancelan las demás para evitar quedar con posición direccional no deseada.

### Paper mode
Simula todo sin ClobClient. Registra trades con profit simulado para evaluar frecuencia y rentabilidad antes de ir a live.

---

## Estrategia 4: Liquidity Rewards (Fases 1-5)

Market making incentivado: ganar rewards de Polymarket por proveer liquidez SIN FILLS.

### Filosofía de diseño

La estrategia prioriza **rewards netos (rewards - pérdidas por fills)** sobre rewards brutos:
- Estar lo más cerca posible del midpoint para maximizar Q-score (rewards)
- Pero reaccionar rápido (cada 15s) a cualquier movimiento para huir antes de ser filled
- Seleccionar mercados donde OTROS makers cotizan más tight (nos protegen) y tienen baja volatilidad

### Fase 1: RewardScanner con selección inteligente

**Fórmula de scoring (v2 - fill-safe):**
```
reward_per_dollar = daily_rate / max(competitiveness, 1)

Competencia (comp_factor):
  comp ≤ $0      → 0.3  (somos el book, fills seguros ❌)
  comp < $1      → 0.5
  comp < $5      → 1.0
  comp < $20     → 1.3 ✅ (others absorb flow, nos protegen)
  comp < $100    → 1.0
  comp ≥ $100    → 0.5

Spread natural (spread_penalty):
  Si spread > nuestro_distance × 2  → 5.0 ❌ (somos top-of-book)
  Si spread > nuestro_distance      → 2.5
  Si spread > 5¢                    → 1.5
  Si spread ≤ 5¢                    → 1.0 ✅ (otros más tight, estamos protegidos)

Volatilidad (volume_factor) — 5 tiers graduales:
  vol > 100k    → 0.05 (extremo, evitar)
  vol > 50k     → 0.15 (muy alto, risky)
  vol > 20k     → 0.4  (alto, riesgo moderado)
  vol > 10k     → 0.6  (moderado, aceptable)
  vol > 5k      → 0.8  (normal, OK)
  vol < 500     → 1.2  (tranquilo, bonus)

score = (reward_per_dollar × comp_factor × volume_factor) / (risk_factor × spread_penalty)
```

**Resultado**: Selecciona mercados como "Starmer out by May 15" ($200/day, comp=$18, spread=1¢, vol=5k)
en lugar de "WTI $95 in April" ($100/day, comp=$0, spread=26¢, vol=3k) que causa fills masivos.

### Fase 2: LiquidityProvider con quoting agresivo + fast escape

**Posicionamiento:**
- `spread_pct_of_max = 0.50` → órdenes a **~2.3¢ del midpoint** (vs 3¢ antes, 4¢ original)
- Esto da ~4× más Q-score que el original → ~4× más rewards
- Pero si el midpoint se mueve 0.5¢ hacia nosotros, cancelamos en max 15s

**Monitoreo y reacción:**
- `quote_refresh_s = 15` → chequea cada 15s si el midpoint se movió
- `reprice_threshold = 0.005` (0.5¢) → cancela+replace si midpoint se acerca
- Si midpoint no se mueve, la orden se mantiene cobrando rewards

**Two-sided quoting:**
- BUY YES a `bid_price` (~2.3¢ abajo del midpoint)
- BUY NO a `(1.0 - ask_price)` (~2.3¢ arriba del midpoint)
- Ambas con `post_only=True` (maker, 0% fees)

**Redeem automático:**
- Si fills_yes > 0 Y fills_no > 0 → mercado.redeem() = YES+NO=$1 profit
- Reinicia desde cero en ese mercado

### Fase 2.5: Inicialización limpia (cancel_all al startup)

En live mode, al arrancar:
1. Llama `client.cancel_all()` → cancela TODAS las órdenes huérfanas del run anterior
2. Libera USDC bloqueado
3. Comienza fresh con 3 mercados nuevos

Esto evita que órdenes antiguas bloqueen el capital y causen fills no esperadas.

### Fase 3: Risk & Inventory

- **Inventory skew**: `(fills_yes - fills_no) / (fills_yes + fills_no)`, rango [-1, 1]
- **Rebalanceo automático** (3 niveles según |skew| vs `max_inventory_skew`=0.6):
  - Mild (0.6-0.7): spread ×1.5 lado largo, ×0.8 lado corto
  - Moderate (0.7-0.8): size ×0.5 largo / ×1.5 corto + ajuste spread
  - Severe (>0.8): solo cotiza lado rebalanceador
- **Adverse selection**: estimada como `|fill_price - midpoint| × size`; mercado abandonado si ratio > 0.7
- **Emergency cancel**: si midpoint mueve >5% en <30s → cancela todo en ese mercado
- **Ghost fill defense**: cada ciclo de refresh (30s), `_check_order_status()` verifica que cada orden activa realmente existe en el CLOB. Si `get_order()` devuelve `None` o status `CANCELLED/EXPIRED` sin que nosotros la cancelemos → la orden fue eliminada silenciosamente (ataque ghost fill). Se marca como `cancelled`, se limpia la referencia y `_refresh_quotes` la recoloca inmediatamente. Contador `ghost_removals` en stats para monitoreo.

### Fase 4: Metrics & Real Rewards Tracking

**Tracking de rewards reales (v2):**
- Consulta `GET https://data-api.polymarket.com/activity?user=<address>&type=REWARD` cada 5 min
- Obtiene REWARD events reales (sin auth requerida, solo dirección pública)
- Actualiza `metrics_today.rewards_earned` con datos reales de Polymarket
- **Resultado**: El `net_pnl` refleja la realidad (rewards - losses) no simulaciones

**KPIs y snapshots:**
- `LiquidityMetrics`: snapshots diarios con rollover a medianoche UTC, retención 90 días
- Tracking: rewards (reales), adverse loss, **maker rebate** (V2), net P&L, ROI, APY estimado
- `total_gross = rewards + spread_income + maker_rebate` — el rebate se suma como ingreso
- Panel web: P&L del día + resumen 7 días + quotes activas + botón emergency cancel

### Fase 3.5 (optional): Heartbeat, Order Scoring

- **Heartbeat**: POST `/heartbeat` (desactivado por default, requiere monitoreo 24/7)
- **Order scoring**: GET `/order-scoring?order_id=X` (endpoint no fiable — siempre dice "not scoring" aunque sí gana rewards. Verificar en UI de Polymarket.)

### Configuración optimizada (actual)

| Parámetro | Anterior | Actual | Razón |
|-----------|----------|--------|-------|
| `capital_per_market` | 50 | 34 | $34 × N mercados según capital |
| `max_markets` | 5 | **15** | Más diversificación, capital como límite real |
| `spread_pct_of_max` | 0.85 (4¢) | **0.50 (~2.3¢)** | ~4× más Q-score |
| `quote_refresh_s` | 120 | **15** | Reacciona 8× más rápido |
| `reprice_threshold` | 0.01 (1¢) | **0.005 (0.5¢)** | Huye ante mínimo movimiento |
| `max_min_size` | (manual) | **auto-calc** | total_capital/3/1.2 → accede a más mercados |

**Auto-calc de max_min_size:**
- Si `max_min_size=0` en config, se calcula automáticamente: `total_capital / 3 / 1.2`
- Con $500 total → max_min_size = 138 shares
- Filtra a mercados con min_size ≤ 138 (evita barreras de entrada altas)

### Parámetros configurables (hot-reload via panel)
| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| scan_interval | 300 | Segundos entre scans de mercados con rewards |
| min_daily_rate | 1.0 | Mínimo $/día para considerar un mercado |
| min_reward_per_dollar | 0.001 | Ratio mínimo reward/competencia |
| capital_per_market | 34.0 | pUSD a asignar por mercado |
| max_markets | 15 | Máximo mercados cotizando (capital es el límite real) |
| quote_refresh_s | 15 | Refresh cada 15s para reaccionar rápido |
| spread_pct_of_max | 0.50 | 50% de max_spread → ~2.3¢ del midpoint |
| use_heartbeat | false | Activar heartbeat loop (requiere 24/7 uptime) |
| heartbeat_interval | 5 | Segundos entre heartbeats |
| scoring_check_interval | 60 | Segundos entre checks de scoring (endpoint no fiable) |

### Resultados esperados

Con los cambios recientes (scoring v3 + quoting ultra-agresivo):
- **Rewards**: ~$10-30/día (estimado conservador en mercados políticos estables)
- **Fill rate**: <1% (vs 18% con configuración anterior)
- **Fill losses**: ~$0 por día (vs -$7.66 con WTI/Iran markets)
- **Net P&L**: **+$10-30/día** (rewards sin pérdidas masivas)
- **APY**: ~40-100% anualizado en $500 de capital

### Monitoreo recomendado

1. **Cada vez que despliegues**: Ver log `startup_cancel_all` confirmar que libera capital
2. **Dashboard en vivo**: Monitorear quote refresh (cada 15s), ver si `reprice_threshold` se activa
3. **Semanalmente**: Revisar `net_pnl` y comparar contra rewards reales en Polymarket UI
4. **Mensualmente**: Analizar `adverse_ratio` y si hay patrones de fills — si sube, revisar selección de mercados

---

## Estrategia 5: Weather Prediction (Pronóstico Meteorológico)

Predicción de temperatura usando 50 modelos ensemble ECMWF IFS vs precios de Polymarket.

### Estructura de mercados en Polymarket

Los mercados de temperatura son **eventos** que contienen N **mercados binarios** (Yes/No), uno por rango de temperatura:
- Evento: "Highest temperature in Atlanta on May 6?"
- Mercado 1: "Will it be 57°F or below?" → Yes/No
- Mercado 2: "Will it be between 58-59°F?" → Yes/No
- Mercado N: "Will it be 76°F or higher?" → Yes/No

**Descubrimiento**: `tag_id=103040` (Daily Temperature) en endpoint `/events/keyset` de Gamma API (con cursor pagination). El parámetro `tag=weather` NO funciona. El endpoint legacy `/events` fue deprecado el 10 abr 2026 — devuelve HTTP 200 con array vacío silenciosamente.

### Arquitectura de archivos

```
src/
  weather_scanner.py       # WeatherScanner: descubrimiento, forecast, edge detection, ejecución
  strategies/weather.py    # WeatherStrategy: wrapper que registra el scanner como estrategia
```

- `WeatherStrategy` extiende `BaseStrategy`, crea un `WeatherScanner` interno y un `_resolution_task`
- Se registra via `register_strategy("weather", WeatherStrategy, WeatherConfig)`
- `WeatherConfig.from_dict()` parsea la sección `[accounts.weather]` del TOML

### Flujo completo
1. `_discover_markets()`: Busca eventos con `tag_id=103040` en Gamma API `/events/keyset` (con cursor pagination), parsea slug para extraer ciudad+fecha
2. `_parse_temperature_event()`: Para cada evento, extrae los mercados binarios individuales con sus `clobTokenIds`, `outcomePrices`, `condition_id`, y el label del outcome (via `_extract_outcome_label()` o fallback a `groupItemTitle`)
3. `_get_forecast()`: Consulta Open-Meteo ensemble API (50 miembros ECMWF IFS), cachea 1h. Usa datos **hourly** (`temperature_2m`) y calcula el **max diario** por miembro
4. `_build_distribution()`: Convierte 50 predicciones de max_temp → distribución de probabilidad sobre los buckets del mercado
5. `_evaluate_market()`: Compara distribución forecast vs precios de mercado → detecta edge (`forecast_prob - market_price`)
6. `_execute_trade()`: Si edge > 8%, ejecuta con Kelly sizing (quarter-Kelly). Paper/dry_run simula, live usa ClobClient
7. `check_resolutions()`: Loop periódico (cada `resolution_check_interval`) que resuelve trades pendientes consultando la temperatura real via Open-Meteo daily API

### Open-Meteo Ensemble API
- **Endpoint**: `https://ensemble-api.open-meteo.com/v1/ensemble`
- **Model**: `ecmwf_ifs025` (50 miembros, 15 días). La API dice "51 members" pero devuelve `member01..member50` (50 reales) + `temperature_2m` (media/control)
- **Datos usados**: Hourly `temperature_2m` por cada miembro → max diario calculado internamente
- **Devuelve °C siempre**. Si el mercado usa °F, el scanner convierte antes de asignar buckets
- Gratis, sin API key

### Manejo de unidades (°C / °F)
- Open-Meteo siempre devuelve °C
- Mercados de EEUU usan °F con rangos ("66-67°F"), mercados de Asia/Europa usan °C con grados individuales ("18°C")
- `_build_distribution()` detecta la unidad desde los outcomes y convierte: `member_temps = [t * 9 / 5 + 32 for t in max_temps]`
- `_determine_winner()` también convierte para resolución correcta

### Parsing de buckets (temperatura → rango)

Cada outcome se parsea a un rango `[low_incl, high_excl)`:

| Formato outcome | Ejemplo | Rango resultante |
|----------------|---------|------------------|
| "X°F or below" | "57°F or below" | `(-999, 58)` (≤57 en integer) |
| "X°C or below" | "17°C or below" | `(-999, 17.5)` |
| "X-Y°F" | "66-67°F" | `[66, 68)` |
| "X°C" (individual) | "18°C" | `[17.5, 18.5)` |
| "X°F or higher" | "76°F or higher" | `[76, 999)` |

**Variantes reconocidas**: "or below"/"or less"/"or under"/"or lower" (bajo), "or higher"/"or more"/"or above"/"+" (alto).

**Extracción de label**: Primero intenta `_extract_outcome_label(question)` que parsea el texto de la pregunta y normaliza (e.g. "or above" → "or higher"). Si falla, usa `groupItemTitle` de la Gamma API como fallback.

**Importante**: Los campos `clobTokenIds`, `outcomePrices`, `outcomes` de Gamma API vienen como **JSON strings** (no arrays nativos). Requieren `json.loads()` para parsear.

### Protección contra fallback de temperatura fuera de rango

Si la temperatura predicha está **fuera de todos los buckets**, solo se asigna si existe un bucket edge apropiado:
- Temp por debajo de todos → solo si el primer bucket es tipo "or below" (bound -999)
- Temp por encima de todos → solo si el último bucket es tipo "or higher" (bound 999)
- Si no existe edge bucket apropiado → el miembro queda como `unmatched` y no infla ningún bucket

**Motivación**: Sin esta protección, si el bucket "29°C or higher" no se parsea correctamente (formato no reconocido), los 50 miembros a ~30°C se asignarían al último bucket regular ("28°C") dando una probabilidad falsa del 100% y un edge ficticio del 86%.

### Ciudades soportadas
75+ ciudades con coordenadas hardcodeadas en `CITY_COORDS`. Incluye: ciudades EEUU (NYC, LA, Chicago, Miami, Atlanta, Phoenix, etc.), Europa (London, Paris, Madrid, Berlin, Rome, etc.), Asia (Tokyo, Shanghai, Singapore, Seoul, Wuhan, Qingdao, etc.), Oceanía (Sydney, Melbourne, Wellington), Medio Oriente (Dubai, Tel-Aviv, Cairo), Sudamérica (São Paulo, Buenos Aires, Lima, Bogotá). Si aparece una ciudad no reconocida, se loguea como `weather_unknown_city` y se ignora.

### Edge y bet sizing

```
edge = forecast_prob - market_price
ev_per_share = forecast_prob - market_price - fee_per_share
fee_per_share = fee_rate × price × (1 - price)  # weather_fees = 0.05

# Kelly criterion: f* = (bp - q) / b
b = (1 / price) - 1    # odds
kelly = (b × prob - (1-prob)) / b
kelly = clamp(kelly, 0, 0.25)  # max 25% Kelly

bet_size = min(bankroll × kelly × kelly_multiplier, max_bet_per_trade)
```

### Filtros de calidad

Un trade se ejecuta solo si:
1. `edge >= min_edge` (8%)
2. `forecast_prob >= min_forecast_prob` (15%) — descarta predicciones ruidosas
3. `market_price <= max_price` (75¢) — asegura upside suficiente
4. `agreement >= min_agreement` (20%) — al menos 20% de miembros coinciden en un bucket
5. `ev_per_share > 0` — EV positivo tras fees
6. Max `max_bets_per_cycle` (3) trades por ciclo de scan

### Resolución de trades

`_resolution_loop()` corre cada `resolution_check_interval` (1h). Para cada trade pendiente:
1. Consulta Open-Meteo **daily** API (`temperature_2m_max`) para la ciudad y fecha del trade
2. `_determine_winner()`: Compara la temp real contra cada outcome bucket (misma lógica de parsing que `_build_distribution`)
3. Si el outcome comprado ganó → `status = "won"`, `pnl = (1.0 / price - 1) × cost`
4. Si otro outcome ganó → `status = "lost"`, `pnl = -cost`

### Dashboard

La cuenta weather se muestra en el dashboard principal con:
- **Badge**: "WEATHER" (amarillo, `badge-weather`)
- **Stats**: Markets (descubiertos), Forecasts (cacheados), Trades (ejecutados), Scans
- **Tabla de trades**: City, Outcome, Price, Edge, Cost, Result, P&L, Mode
- **Balance**: `simulated_balance + total_pnl` (paper mode)

### Parámetros (config.toml `[accounts.weather]`)
| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| scan_interval | 900 | Cada 15 min |
| forecast_cache_ttl | 3600 | Cache de forecasts: 1 hora |
| max_forecast_days | 2 | Solo mercados a ≤2 días (ensemble más fiable) |
| min_edge | 0.10 | Mínimo 10% edge para apostar |
| min_forecast_prob | 0.15 | Ignora outcomes con <15% prob (ruido) |
| min_agreement | 0.30 | Al menos 30% de modelos (15/50) deben coincidir |
| max_price | 0.65 | No comprar outcomes >65¢ (más upside) |
| max_bet_per_trade | 15.0 | $15 max por trade (Kelly sizing es el driver real) |
| bankroll | 300.0 | Capital total weather |
| kelly_multiplier | 0.30 | 30% Kelly (ligeramente más agresivo) |
| max_bets_per_cycle | 8 | Max 8 trades por ciclo (ciudades son independientes) |
| resolution_check_interval | 3600 | Verificar resoluciones cada hora |

Todos los parámetros son editables en caliente desde la pestaña Weather del panel web (`/panel/weather`).

---

## Panel de Control Web

Accesible en `http://host:8080`. Protegido por login con cookie HMAC-SHA256.

### Rutas
| Ruta | Descripción |
|------|-------------|
| `/login` | Login (user/password contra SQLite + bcrypt) |
| `/` | Dashboard (read-only, auto-refresh cada 5s) |
| `/panel/copy-trade` | Gestión wallets (add/remove/toggle/set_role) + parámetros |
| `/panel/directional` | Kill switch + market filter (crypto_only) + parámetros |
| `/panel/liquidity` | Scanner + provider + quotes activas + métricas P&L |
| `/panel/liquidity/cancel-all` | POST: emergency cancel de todas las órdenes de liquidez |
| `/panel/weather` | Config weather: bet sizing, edge thresholds, timing + forecasts activos |
| `/panel/weather/params` | POST: actualizar parámetros weather (hot-reload) |
| `/panel/settings` | Cambio password + execution mode (paper/live) + audit log |
| `/api/report` | JSON completo de estado del bot |
| `/api/report/{account}` | JSON por cuenta específica |
| `/api/rewards/markets` | JSON con mercados rankeados por reward/competencia |
| `/api/rewards/metrics` | JSON con métricas diarias, historial y resumen P&L |

### Hot-Reload
Los cambios desde el panel se aplican **inmediatamente** (mutación in-memory de dataclasses) y se persisten al archivo `config/config.toml` para sobrevivir reinicios.

Los roles y estado de wallets se almacenan en SQLite (`data/panel.db`), no en TOML.

### Cambio de execution mode desde el panel
En Settings se puede cambiar el modo de cada cuenta (paper/live). El cambio:
- Resetea todas las stats y apuestas del modo anterior
- En live: inicializa el CLOB client, refresca balance real, usa balance real como starting_balance
- En paper: vuelve al simulated_balance del config
- Si la inicialización del CLOB falla, revierte al modo anterior y loguea el error

### Dashboard multi-estrategia

El dashboard principal (`/`) muestra cada cuenta con su badge, stats y tabla de trades adaptados al tipo de estrategia:

| Estrategia | Badge (color) | Stats específicos | Tabla de trades |
|-----------|--------------|-------------------|----------------|
| Directional | DIRECTIONAL (azul) | Opportunities, Scans | Time, Market, Side, Price, Margin, Time Left, Depth, Bet, Profit, Duration, Result, P&L |
| Copy-Trade | COPY-TRADE (naranja) | Trades Copied, Polls | Time, Market, Side, Price, Bet, Profit, Duration, Result, P&L, Source |
| Weather | WEATHER (amarillo) | Markets, Forecasts, Trades, Scans | City, Outcome, Price, Edge, Cost, Result, P&L, Mode |
| Completeness | COMPLETENESS (cyan) | Opportunities, Pending Redeems, Scans | Market, Shares, Cost, Profit, Status, Mode |
| Liquidity | LIQUIDITY (púrpura) | Reward Markets, Active Quotes, Markets Quoting, Rewards $, Scans | Market, Mid, Bid, Ask, Fills Y/N, Skew, Rewards |

El mapping de stats se realiza en `_build_account_data()` (`routes_dashboard.py`), que detecta `strategy_type` y lee los campos correctos de cada estrategia (e.g. weather usa `trades_won`/`total_pnl`, completeness usa `trades_executed`/`total_profit`, liquidity lee de `provider` y `metrics_today`).

### Autenticación
- Cookie HMAC-SHA256 firmada (sin dependencias externas de crypto)
- Secreto de sesión desde env var `SESSION_SECRET` (o generado aleatoriamente)
- Password hasheado con bcrypt en SQLite
- Usuario admin creado automáticamente con `PANEL_PASSWORD` env var

---

## Base de Datos SQLite (`data/panel.db`)

```sql
-- Usuarios del panel
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,  -- bcrypt
    role TEXT DEFAULT 'admin',
    created_at REAL
);

-- Overrides de wallets (roles, alias, enable/disable)
CREATE TABLE wallet_overrides (
    address TEXT PRIMARY KEY,
    alias TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    role TEXT DEFAULT 'primary',         -- "primary" o "confirmation"
    confirms_wallet TEXT DEFAULT ''      -- dirección de la primary que confirma
);

-- Log de auditoría (quién cambió qué)
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    timestamp REAL
);
```

---

## Configuración (`config/config.toml`)

Archivo TOML con secciones: `[strategy]`, `[risk]`, `[data]`, `[websocket]`, `[logging]`, `[[accounts]]`.

Cada `[[accounts]]` es independiente con su propia estrategia, credenciales y riesgo:
- `strategy_type`: "directional", "copy_trade", "completeness", o "liquidity"
- `execution_mode`: "paper", "dry_run", o "live" (cambiable en caliente desde panel)
- `[accounts.credentials]`: env vars para private key, API keys y tipo de wallet
- `[accounts.copy_trade]`: config específica de copy trading
- `[accounts.risk]`: overrides de riesgo por cuenta

**Parámetros que NO se exponen en panel** (requieren reinicio):
- Credenciales API / private key
- `strategy_type`
- Configuración WebSocket

**Parámetros cambiables en caliente desde el panel:**
- `execution_mode` (paper/live) — resetea stats al cambiar
- Todos los parámetros de estrategia (tablas de arriba)
- Kill switch, wallets, roles

---

## Despliegue

```bash
# Build y deploy via Docker Compose / Portainer
docker compose build && docker compose up -d
```

### Variables de entorno (docker-compose.yml)

```yaml
environment:
  # Panel web
  - PANEL_PASSWORD=tu_password        # Password del admin (default: "admin")
  - SESSION_SECRET=                   # Secreto para cookies (generado si vacío)

  # Tipo de wallet — afecta cómo se firma contra la API CLOB
  # Valores: "magic_link" (default), "metamask", "2" (gnosis_safe)
  - WALLET_TYPE=2

  # Credenciales cuenta directional
  - PRIVATE_KEY=0x...                 # Private key (MetaMask o Magic Link)
  - POLYMARKET_PROXY_ADDRESS=0x...    # Proxy address (de Polymarket Settings/Perfil)
  - POLYMARKET_API_KEY=               # Opcional: relayer_api_key (se auto-deriva)
  - POLYMARKET_SECRET=                # Opcional: se auto-deriva de la private key
  - POLYMARKET_PASSPHRASE=            # Opcional: se auto-deriva de la private key

  # Builder credentials (requerido para MetaMask, de "Códigos del constructor")
  - BUILDER_API_KEY=
  - BUILDER_SECRET=
  - BUILDER_PASSPHRASE=

  # Credenciales cuenta copy-trade (puede ser el mismo wallet u otro)
  - COPY_PRIVATE_KEY=0x...            # Private key del wallet copy
  - COPY_API_KEY=                     # Opcional: se auto-deriva
  - COPY_SECRET=                      # Opcional: se auto-deriva
  - COPY_PASSPHRASE=                  # Opcional: se auto-deriva
  - COPY_WALLET_TYPE=                 # Opcional: hereda de WALLET_TYPE si vacío
  - COPY_PROXY_ADDRESS=               # Proxy address de la cuenta copy (si aplica)
```

**Mínimo requerido para operar en live:**
- `PRIVATE_KEY` y/o `COPY_PRIVATE_KEY` (según qué cuentas usen live)
- `WALLET_TYPE` configurado correctamente (ver tabla de tipos arriba)
- `POLYMARKET_PROXY_ADDRESS` si usas tipo 1 (Magic Link) o tipo 2 (Gnosis Safe)
- `BUILDER_API_KEY/SECRET/PASSPHRASE` si usas tipo 0 o 2 (MetaMask)

**Ejemplos:**

```yaml
# Ejemplo 1: MetaMask + Gnosis Safe (caso más común con MetaMask)
- WALLET_TYPE=2
- PRIVATE_KEY=0xaaaa...              # Exportada desde MetaMask
- POLYMARKET_PROXY_ADDRESS=0xbbbb... # Dirección del Perfil en Polymarket
- POLYMARKET_API_KEY=019d52b5-...    # relayer_api_key de Polymarket Settings
- BUILDER_API_KEY=...                # De "Códigos del constructor"
- BUILDER_SECRET=...                 # De "Códigos del constructor"
- BUILDER_PASSPHRASE=...             # De "Códigos del constructor"

# Ejemplo 2: Magic Link (email login)
- WALLET_TYPE=magic_link
- PRIVATE_KEY=0x1234abcd...          # Exportada desde Polymarket → Settings → Export Private Key
- POLYMARKET_PROXY_ADDRESS=0xcccc... # Dirección de Settings → Account

# Ejemplo 3: Directional con MetaMask (Gnosis Safe), Copy con Magic Link
- WALLET_TYPE=2
- PRIVATE_KEY=0xaaaa...              # MetaMask
- POLYMARKET_PROXY_ADDRESS=0xbbbb... # Proxy de Polymarket
- BUILDER_API_KEY=...
- BUILDER_SECRET=...
- BUILDER_PASSPHRASE=...
- COPY_PRIVATE_KEY=0xcccc...         # Magic Link (otra cuenta)
- COPY_WALLET_TYPE=magic_link
- COPY_PROXY_ADDRESS=0xdddd...       # Proxy de la cuenta copy

# Ejemplo 4: Solo paper trading (no requiere credenciales)
- WALLET_TYPE=magic_link
# No se necesitan PRIVATE_KEY ni COPY_PRIVATE_KEY en paper mode
```

### Volúmenes persistentes
- `bot-logs`: `/app/logs` (logs JSON)
- `bot-data`: `/app/data` (SQLite panel.db)

---

## Executor: ejecución de trades

El `Executor` maneja tres modos y es responsable de:
- **Paper**: Registra trades ficticios sin interactuar con Polymarket
- **Dry Run**: Inicializa cliente CLOB V2, valida órdenes, no las envía
- **Live**: Coloca órdenes reales contra Polymarket CLOB V2

### CLOB V2 (desde 28 abril 2026)

El bot usa `py-clob-client-v2` (paquete V2). Cambios clave respecto a V1:
- **SDK**: `from py_clob_client_v2 import ClobClient, OrderArgs, OrderType`
- **API keys**: `client.create_or_derive_api_key()` (antes `derive_api_key()`)
- **Cancelar orden**: `client.cancel_order(OrderPayload(orderID=id))` (antes `client.cancel(id)`)
- **Cancelar todo**: `client.cancel_all()` (sin cambios)
- **Collateral**: pUSD reemplaza USDC.e como colateral
- **Contratos V2**: CTF Exchange `0xE111...`, Neg Risk `0xe222...`
- **Orden struct V2**: elimina `nonce`/`feeRateBps`/`taker`, añade `timestamp`/`metadata`/`builder` (opcionales)
- `create_order(OrderArgs(...))` sigue funcionando sin `PartialCreateOrderOptions` (es opcional)
- `post_order(signed, OrderType.GTC, post_only=True)` — misma firma

### Flujo de una orden live
1. Risk checks: kill_switch, daily loss, max concurrent, balance suficiente
2. Resuelve `token_id` (YES/NO) del mercado
3. Crea y firma la orden con `ClobClient.create_order(OrderArgs(...))`
4. Envía con `ClobClient.post_order(signed)`
5. Monitorea estado (polling cada 5s) y cancela si no se llena en 30s
6. Resta coste del balance optimistamente al colocar

### Balance
- `_live_balance`: pUSD libre consultado via `get_balance_allowance(COLLATERAL)` (None en paper)
- Se refresca cada hora automáticamente y al cambiar a modo live
- En live, si `suggested_bet > _live_balance` → trade rechazado (`insufficient_balance`)
- El dashboard muestra el balance real en live, o el simulado en paper

---

## Optimizaciones implementadas

1. **O(1) detector**: `check(token_id)` busca un solo mercado por token_id en vez de escanear todos
2. **Filtro crypto**: `tag="crypto"` en Gamma API reduce mercados de ~200 a ~20
3. **Sesión HMAC ligera**: Reemplazó EncryptedCookieStorage (dependía de cryptography, muy lento en VPS)
4. **Wallets deshabilitadas no se pollean**: `_poll_all_wallets` filtra por `_wallet_enabled`
5. **Keyset pagination**: Gamma API usa `/markets/keyset` con cursor en vez de offset pagination (más eficiente para descubrimiento de mercados)

---

## Fees de Polymarket (V2 — Mayo 2026)

Fees por categoría. Makers nunca pagan fees. Takers pagan: `feeRate × shares × p × (1-p)`.

| Categoría | Taker feeRate | Maker Rebate |
|-----------|--------------|-------------|
| Crypto | 0.072 | 20% |
| Sports | 0.03 | 25% |
| Finance/Politics/Tech/Mentions | 0.04 | 25% |
| Economics/Culture/Weather/Other | 0.05 | 25% |
| Geopolitics | 0.0 (gratis) | — |

- **Fórmula taker**: `feeRate × shares × price × (1 - price)` — máximo a p=0.50, decrece hacia extremos
- **Maker rebate**: % del fee del taker devuelto al maker cuando es filled
- **Gas redeem**: ~$0.004 por redención
- **Módulo centralizado**: `src/fees.py` contiene `taker_fee()`, `taker_fee_per_share()`, `maker_rebate()`, `net_margin()`, `fee_rate_from_fee_type()`, `category_from_fee_type()`
- **Auto-detección desde Gamma API**: `FEE_TYPE_MAP` mapea `feeType` strings (e.g. `"crypto_fees_v2"` → `"crypto"`, `"sports_fees_v2"` → `"sports"`) a categorías internas. `fee_rate_from_fee_type(fee_type, fees_enabled)` devuelve el `feeRate` correcto; si `fees_enabled=False` devuelve 0.0

Ejemplos (crypto, 100 shares):
| Precio | Fee/share | Fee total | Maker rebate |
|--------|----------|-----------|-------------|
| 0.97 | $0.0021 | $0.21 | $0.04 |
| 0.50 | $0.0180 | $1.80 | $0.36 |
| 0.30 | $0.0151 | $1.51 | $0.30 |

---

## Cambios recientes y problemas resueltos (Abril-Mayo 2026)

### Migración a CLOB V2 (Mayo 2026)

Polymarket lanzó CLOB V2 el 28 de abril de 2026. Cambios aplicados:

| Aspecto | V1 (antes) | V2 (ahora) |
|---------|-----------|-----------|
| **SDK pip** | `py-clob-client>=0.15` | `py-clob-client-v2>=1.0.0` |
| **Import** | `from py_clob_client.client import ClobClient` | `from py_clob_client_v2 import ClobClient` |
| **Derivar keys** | `derive_api_key()` | `create_or_derive_api_key()` |
| **Cancel orden** | `cancel(order_id)` | `cancel_order(OrderPayload(orderID=id))` |
| **Collateral** | USDC.e `0x2791Bca1f...` | pUSD `0xC011a7E1...` |
| **CTF Exchange** | `0x4bFb41d5B...` | `0xE11118000...` |
| **Fees** | `0.003 × min(p, 1-p)` uniforme | `feeRate × p × (1-p)` por categoría |

**Sin cambios**: `ClobClient()` init params, `post_order()` con `OrderType.GTC` y `post_only`, `cancel_all()`, `get_balance_allowance()`, `OrderArgs`, WebSocket, Gamma API.

### Actualización de fees (Mayo 2026)

Nuevo sistema de fees por categoría + maker rebates. Implementado en `src/fees.py`:
- Crypto: feeRate=0.072, rebate=20%
- Finance/Politics: feeRate=0.04, rebate=25%
- Geopolitics: 0% fees
- `LiquidityMetrics` ahora trackea `maker_rebate` como ingreso adicional
- Impacto: directional a p=0.97 sigue rentable ($0.002/share vs margen $0.024); Up/Down a p=0.50 necesita WR>55%

### Problema: 0 órdenes colocadas después del despliegue
**Causa**: Los mercados top (Dota 2 esports) tenían `min_size=250`, pero con capital de $50/mercado solo podíamos hacer ~41 shares.
**Solución**: Auto-calc de `max_min_size = capital/2/0.70` filtra automáticamente a mercados accesibles.

### Problema: Fills masivos en mercados de 0 competencia
**Causa**: El scoring anterior premiaba 0 competencia (bonus 1.5×), seleccionando "WTI $95" (spread 26¢, comp $0) donde somos el book único.
**Solución**: Nueva fórmula de scoring penaliza 0 competencia (0.3×) y spreads anchos (5×), prefiriendo mercados con "escudos" (otros makers que absorben flow).

### Problema: Órdenes huérfanas bloqueaban capital tras redeploy
**Causa**: Las órdenes del run anterior quedaban en el CLOB consumiendo USDC, pero el bot nuevo no las conocía.
**Solución**: Llamar `client.cancel_all()` al startup en live mode para liberar todo el capital y comenzar fresh.

### Problema: Rewards internas no reflejaban realidad
**Causa**: Solo se simulaban rewards en paper mode; en live mode, el bot no sabía cuántos rewards cobraba realmente.
**Solución**: Consultar `GET /activity?user=<address>&type=REWARD` cada 5 min desde Data API, actualizar `metrics_today.rewards_earned` con datos reales.

### Problema: No ganábamos suficientes rewards estando a 4¢ del midpoint
**Causa**: Q-score es proporcional a 1-distancia/max_spread. A 4¢ (85%) nos ganamos poco.
**Solución**: Bajar a 3¢ (65%) para 3× más Q-score, pero monitorear cada 30s para huir rápido si el midpoint se acerca.

### Defensa: Ghost fill attack (Mayo 2026)
**Vulnerabilidad**: Polymarket tiene un gap entre matching off-chain y settlement on-chain. Un atacante puede provocar que el match falle, y Polymarket elimina silenciosamente las órdenes de los market makers del orderbook sin notificar. El bot seguiría creyendo que tiene órdenes activas cuando en realidad fueron eliminadas (0 rewards, capital idle).
**Solución**: `_check_order_status()` verifica en cada ciclo (30s) que cada orden activa realmente existe en el CLOB via `get_order()`. Si devuelve `None` o `CANCELLED/EXPIRED` sin que nosotros la cancelemos → log `ghost_order_detected` + incrementa `ghost_removals` + limpia referencia → `_refresh_quotes` recoloca inmediatamente. Tiempo máximo sin órdenes: ~30s (1 ciclo).

### Problema: Completeness scanner detectaba 0 oportunidades (Mayo 2026)
**Causa**: 3 bugs acumulados: (1) filtro `is_stale` descartaba mercados con `last_update=0` incluso cuando tenían precios válidos via `best_bid_ask`, (2) sin fallback de sizing cuando order book vacío pero `best_ask > 0`, (3) callback WebSocket no wired en cuentas standalone de completeness.
**Solución**: (1) Reemplazar `is_stale` por `last_update == 0`, (2) añadir fallback sizing `size = max_cost_per_trade / best_ask`, (3) wiring explícito en `account_runner.py`. Resultado: 200+ mercados evaluados correctamente. Los mercados están perfectamente arbitrados (best_gap ~-0.001), pero el scanner detectará gaps cuando aparezcan.

### Problema: spread_penalty usaba valor hardcodeado (Mayo 2026)
**Causa**: En `reward_scanner.py`, `our_distance` se calculaba con `0.85` hardcodeado en vez del `spread_pct_of_max` real del config (que ya estaba en 0.65 y ahora en 0.50). Esto causaba que la penalización por spread se calculara mal.
**Solución**: Pasar `spread_pct_of_max` como parámetro al RewardScanner y usarlo en `_rank_markets()`. Impacto: mejor selección de mercados donde realmente estamos protegidos.

### Problema: volume_factor demasiado agresivo (Mayo 2026)
**Causa**: Solo 3 tiers de volumen (>50k→0.05, >10k→0.15, >5k→0.4) excluían mercados de alto reward con volumen moderado como SPY $720 ($913/día, vol=23k → factor 0.15).
**Solución**: 5 tiers graduales (100k→0.05, 50k→0.15, 20k→0.4, 10k→0.6, 5k→0.8) permiten acceso a mercados high-reward con riesgo aceptable.

### Migración a keyset pagination en Gamma API (Mayo 2026)
**Causa**: La offset pagination (`/markets?offset=N`) era ineficiente para descubrimiento de mercados.
**Solución**: Migrar a `/markets/keyset` con cursor. Respuesta: `{"markets": [...], "next_cursor": "..."}`. Más eficiente y confiable.

### Problema: Completeness scanner usaba fees incorrectas para mercados no-crypto (Mayo 2026)
**Causa**: El endpoint keyset de Gamma API no devuelve `feeSchedule`, solo `feeType` (e.g. `"crypto_fees_v2"`, `"politics_fees"`) y `feesEnabled`. Sin inferencia, todos los mercados se evaluaban con fees de crypto (7.2%). Mercados de geopolítica (fees 0%) que solo necesitan $0.005 de gap se rechazaban al exigir $0.04.
**Solución**: `gamma_client.py` infiere `fee_rate` desde `feeType`+`feesEnabled`. Nuevas funciones en `src/fees.py`: `fee_rate_from_fee_type()`, `category_from_fee_type()`, `FEE_TYPE_MAP`. Mercados con `feesEnabled=false` obtienen correctamente 0% fees.

### Problema: Ghost trades bloqueaban _bet_placed permanentemente (Mayo 2026)
**Causa**: `restore_open_positions()` restauraba trades de la DB con `cost_usd=0, size=0` → `suggested_bet=0`. Estos trades fantasma bloqueaban keys en `_bet_placed` para siempre, impidiendo nuevas apuestas en esos mercados.
**Solución**: Skip de trades con `suggested_bet <= 0` durante la restauración.

### Problema: Bets en tokens Up/Down con precio near-zero (Mayo 2026)
**Causa**: No existía un precio mínimo para bets Up/Down. Tokens a $0.001 pasaban el filtro `max_price=0.60`.
**Solución**: Nuevo parámetro `min_price_updown=0.10` que rechaza tokens con precio demasiado bajo.

### Problema: Closing arb completamente bloqueado por min_margin_net (Mayo 2026)
**Causa**: Closing arb compartía `min_margin_net=0.05` con Up/Down. A precio $0.98, el margen bruto es solo $0.02 → closing arb nunca pasaba el filtro de margen.
**Solución**: Nuevo parámetro `min_margin_closing=0.005` separado para closing arb, que tiene márgenes inherentemente más estrechos pero mayor certeza.

### Problema: Bets pendientes forever en mercados expirados (Mayo 2026)
**Causa**: Si un mercado se eliminaba del tracker antes de la resolución, las bets quedaban en estado "pending" para siempre sin posibilidad de resolverse.
**Solución**: Nuevo background loop `sweep_stale_pending` en `main.py`, ejecuta cada 5 minutos. Resuelve bets stuck en "pending" para mercados expirados hace >1h consultando la CLOB API. Marca como "expired" bets irresolubles (>24h antiguas).

### Cambios de parámetros (antes → ahora)

| Aspecto | Antes | Ahora | Impacto |
|--------|-------|-------|--------|
| **Scoring** | Bonus 0 comp | Penalización 0.3× | Evita mercados sin competencia (fills seguros) |
| **Spread distance** | 4¢ (85%) | **~2.3¢ (50%)** | ~4× más Q-score que original |
| **Refresh rate** | Cada 120s | **Cada 15s** | 8× más rápido huyendo |
| **Reprice trigger** | 1¢ movimiento | 0.5¢ | Reacciona a cambios micro |
| **Max markets** | 5 | **15** | Más diversificación, capital como límite real |
| **Volume scoring** | 3 tiers agresivos | **5 tiers graduales** | Acceso a mercados high-reward con vol moderado |
| **spread_penalty calc** | Hardcoded 0.85 | **Usa config real** | Scoring correcto según distancia real |
| **Capital split** | $50 × 5 mdo | $34 × N mdo | Diversificación + acceso a más mercados |
| **Startup cleanup** | Ninguno | cancel_all() | Libera capital bloqueado |
| **Rewards tracking** | Simulado | Real (Data API) | Métricas confiables |

### Métrica clave: Fill rate

| Configuración | Fill rate | Pérdidas/día | Rewards/día | Neto |
|---|---|---|---|---|
| Antigua (WTI/Iran) | 18.2% | -$7.66 | $0.01 | **-$7.65** |
| Nueva (Starmer/Weinstein) | <1% | ~$0 | $10-30 | **+$10-30** |

### Weather: Estrategia implementada desde cero (Mayo 2026)

Nueva estrategia que predice temperatura máxima diaria usando ensemble ECMWF IFS de 50 miembros vs precios de mercados de temperatura en Polymarket.

**Archivos creados:**
- `src/weather_scanner.py` (~1300 líneas): scanner completo con descubrimiento, forecast, edge detection, ejecución y resolución
- `src/strategies/weather.py` (~130 líneas): wrapper como BaseStrategy con WeatherConfig
- `tests/test_weather_scanner.py` (48 tests): cobertura de parsing, distribución, evaluación, resolución y event parsing

**Integración con el sistema existente:**
- Configurado como `[[accounts]]` con `strategy_type = "weather"` en config.toml
- `account_runner.py`: `_init_weather()` crea la estrategia, `export_full_report()` y `export_opportunities()` manejan weather
- Dashboard: badge amarillo "WEATHER", stats propios (Markets, Forecasts, Trades, Scans), tabla de trades por ciudad

### Problema: Weather scanner reportaba `{"error": "no data"}` (Mayo 2026)
**Causa**: `account_runner.export_full_report()` no tenía handler para `strategy_type == "weather"`. Retornaba `None` → API devolvía 404.
**Solución**: Añadir `if "weather" in self.strategies:` handler en `export_full_report()`.

### Problema: Weather 0 mercados descubiertos (silencioso) (Mayo 2026)
**Causa**: Gamma API devuelve `clobTokenIds`, `outcomePrices`, `outcomes` como **JSON strings** (e.g. `"[\"token1\"]"`), no arrays nativos. El código hacía `clob_tokens[0]` sobre el string, obteniendo el carácter `[` en vez de un token ID.
**Solución**: Añadir `json.loads()` parsing con `isinstance` check y fallback. Ahora parsea ~90 mercados correctamente.

### Problema: Weather edges falsamente altos (86%, 82%) (Mayo 2026)
**Causa**: Bug crítico en `_build_distribution()`. Cuando la temperatura predicha estaba fuera de todos los buckets parseados (e.g. 50 miembros a 30°C pero buckets solo cubrían hasta 28°C), el fallback asignaba TODOS los miembros al último bucket regular → probabilidad falsa del 100%. Ocurría cuando el bucket edge "29°C or higher" no se parseaba por formato no reconocido (e.g. "or above" en vez de "or higher" en `groupItemTitle`).
**Solución**: (1) Solo asignar a edge buckets (bounds -999 o 999), no a buckets regulares. Miembros sin match quedan como `unmatched` con log warning. (2) Añadir "or above", "or under", "or lower" como variantes reconocidas en `_build_distribution()` y `_determine_winner()`.

### Problema: Dashboard mostraba 0 stats para cuentas no-directional (Mayo 2026)
**Causa**: `_build_account_data()` en `routes_dashboard.py` solo reconocía `copy_trade` y `detector` (directional). Cuentas de weather, completeness y liquidity caían al `else: s = {}` → todos los stats en 0, badge siempre "DIRECTIONAL".
**Solución**: Dashboard multi-estrategia con detección de `strategy_type` para cada cuenta. Cada tipo lee sus stats con los campos correctos (e.g. weather: `trades_won`/`total_pnl`, completeness: `trades_executed`/`total_profit`, liquidity: `provider`/`metrics_today`). Badges diferenciados: Weather amarillo, Completeness cyan, Liquidity púrpura. Tablas de trades adaptadas por tipo.

### Weather: Panel web + optimización de parámetros (Mayo 2026)
**Añadido**: Pestaña `/panel/weather` con hot-reload de todos los parámetros (bet sizing, edge thresholds, timing) + tabla de forecasts activos.
**Optimización**: Filtros más estrictos compensan mayor volumen — menos falsos positivos, más capital en oportunidades reales:

| Parámetro | Antes | Ahora | Razón |
|-----------|-------|-------|-------|
| `max_bets_per_cycle` | 3 | **8** | Ciudades independientes, dedup activo |
| `max_bet_per_trade` | $10 | **$15** | Kelly sizing es el driver real |
| `bankroll` | $200 | **$300** | Más capital → sizing ligeramente mayor en high-edge |
| `kelly_multiplier` | 0.25 | **0.30** | 30% Kelly, aún conservador |
| `min_edge` | 0.08 | **0.10** | Filtra más ruido |
| `min_agreement` | 0.20 | **0.30** | 15/50 modelos (más consenso) |
| `max_price` | 0.75 | **0.65** | Más upside por trade |
| `max_forecast_days` | 3 | **2** | Ensemble mucho más fiable a ≤48h |

### Weather: Robustez operativa (Mayo 2026)

**Bankroll efectivo**: Kelly sizing usa bankroll - capital comprometido en trades pendientes. Evita sobre-exposición con múltiples trades abiertos simultáneamente.

**Resolución independiente**: Cada `WeatherTrade` almacena `target_date`, `outcomes` y `unit` al ejecutarse. `check_resolutions()` no depende de `self._markets` (que se sobreescribe cada scan). Trades huérfanos se resuelven correctamente aunque el mercado ya no esté activo en Gamma API.

**Re-check precio en live**: Antes de colocar una orden real, consulta el precio actual vía CLOB `/book`. Si el edge cayó por debajo de `min_edge` (otro trader ya corrigió), cancela la ejecución. Protege contra datos stale (forecast cacheado 1h).

**Pruning de trades**: Trades resueltos con >7 días se eliminan de memoria cada ciclo. Hard cap de 500 trades como safety net para operación continua.

**Pre-filtro de mercados**: `_has_edge_potential()` descarta mercados donde todos los precios > `max_price` antes de consultar la API de forecast. Ahorra requests innecesarias.

**Rate limiting Open-Meteo**: Semáforo de 5 requests concurrentes máximo. Maneja 429 con retry delay de 2s.

**Persistencia** (`data/weather_trades.json`):
- Guarda trades pendientes/confirmados + stats acumulados (wins, losses, P&L, scans)
- Restaura al inicio → deduplicación, bankroll efectivo y dashboard sobreviven reinicios
- Compatible con formato anterior (lista plana de trades)
- Al cambiar de modo (paper→live): `reset_stats()` borra trades, contadores y cache. Live empieza de cero con datos 100% reales

---

## Idioma

El usuario prefiere toda la comunicación en español.
