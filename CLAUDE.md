# Polymarket Multi-Strategy Trading Bot

## Resumen

Bot de trading automatizado para Polymarket que ejecuta tres estrategias independientes en paralelo:
- **Directional**: detecta oportunidades de arbitraje en mercados crypto de 5 minutos
- **Copy Trade**: copia trades de wallets rentables con sistema de roles (primary/confirmation)
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
| `live` | Trading real contra Polymarket CLOB. Usa balance USDC real. |

### Cambio de modo (paper ↔ live)

Se cambia desde el panel web (Settings). Al cambiar de modo:

1. **Paper → Live**: Se inicializa el cliente CLOB con credenciales, se refresca el balance USDC real, se **resetean todas las stats y apuestas del período paper** (bets, wins, losses, P&L), y se establece el balance real como nuevo `starting_balance`. Esto garantiza una vista limpia de la operativa live.
2. **Live → Paper**: Se resetean stats y se vuelve al `simulated_balance` del config.
3. **Métodos de reset**: `CopyTrader.reset_stats()`, `ClosingArbitrageDetector.reset_stats()`, `Executor.reset_trades()` — limpian todo el historial y reinician contadores. Mantienen `polls`/`total_scans` para diagnóstico.

### Balance en modo live

- El executor consulta `get_balance_allowance(COLLATERAL)` de la API CLOB para obtener USDC libre.
- **USDC libre ≠ portfolio total**: solo devuelve USDC disponible para apostar, no el valor de posiciones abiertas.
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

> **IMPORTANTE**: Con MetaMask, los fondos están en el proxy de Polymarket, NO en tu wallet de MetaMask. Si en MetaMask ves $0 USDC pero en Polymarket ves saldo, es correcto — el USDC está en el proxy.

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
3. Si no están definidas → **auto-deriva** las API keys desde la private key usando `ClobClient.derive_api_key()`
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
1. Detector ve un mercado Up/Down con precio <= `max_price` (default 0.60)
2. Consulta precio actual de Binance via `PriceChecker`
3. Si el cambio de precio en Binance confirma la dirección con buffer >= `min_buffer_pct` (default 3%) → oportunidad
4. Límite de `max_concurrent_bets` (default 3) para evitar drawdowns correlacionados

### Parámetros configurables (hot-reload via panel)
| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| kill_switch | false | Detiene todo el trading inmediatamente |
| min_margin_net | 0.008 | Margen mínimo por share después de fees |
| max_price | 0.60 | Precio máximo para bets Up/Down |
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

## Estrategia 3: Liquidity Rewards (Fases 1-5)

Market making incentivado: ganar rewards de Polymarket por proveer liquidez.

### Fase 1: RewardScanner (read-only)
- `RewardScanner` consulta `GET /rewards/markets/multi` (CLOB API, sin auth)
- Rankea mercados por `score = (daily_rate / competitiveness) × spread_factor × volume_factor / risk_factor`
- Panel web muestra top mercados con rewards, competencia, spread y ROI estimado

### Fase 2: LiquidityProvider (market making)
- `LiquidityProvider` coloca órdenes GTC bidireccionales en los top mercados del scanner
- **Two-sided quoting**: BUY YES a `bid_price` (bid) + BUY NO a `1-ask_price` (ask, equivalente a SELL YES)
- Todas las órdenes con `post_only=True` (maker, 0% fees)
- ClobClient propio (no reutiliza Executor — incompatible por ORDER_TIMEOUT=30s y solo BUY)
- Paper mode: simula órdenes sin ClobClient
- Quote refresh cada 30s: cancel+replace si precio difiere >0.5¢ del calculado

### Fase 3: Risk & Inventory
- **Inventory skew**: `(fills_yes - fills_no) / (fills_yes + fills_no)`, rango [-1, 1]
- **Rebalanceo automático** (3 niveles según |skew| vs `max_inventory_skew`=0.6):
  - Mild (0.6-0.7): spread ×1.5 lado largo, ×0.8 lado corto
  - Moderate (0.7-0.8): size ×0.5 largo / ×1.5 corto + ajuste spread
  - Severe (>0.8): solo cotiza lado rebalanceador
- **Adverse selection**: estimada como `|fill_price - midpoint| × size`; mercado abandonado si ratio > 0.7
- **Emergency cancel**: si midpoint mueve >5% en <30s → cancela todo en ese mercado

### Fase 3.5: Heartbeat, Scoring & Block Hiding
- **Heartbeat**: POST `/heartbeat` cada 5s; si se pierde >10s, Polymarket cancela todas las órdenes
- **Order scoring**: GET `/order-scoring?order_id=X` verifica que las órdenes earning rewards
- **Order block hiding**: detecta bloques grandes en el book, coloca órdenes 1 tick detrás para protección

### Fase 4-5: Metrics & KPIs
- `LiquidityMetrics`: snapshots diarios con rollover a medianoche UTC, retención 90 días
- Tracking: fill rate, scoring rate, rewards, adverse loss, net P&L, ROI, APY estimado
- Panel web: P&L del día + resumen 7 días + quotes activas + botón emergency cancel

### Parámetros configurables (hot-reload via panel)
| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| scan_interval | 300 | Segundos entre scans de mercados con rewards |
| min_daily_rate | 1.0 | Mínimo $/día para considerar un mercado |
| min_reward_per_dollar | 0.001 | Ratio mínimo reward/competencia |
| capital_per_market | 50.0 | USDC a asignar por mercado |
| max_markets | 5 | Máximo mercados cotizando simultáneamente |
| quote_refresh_s | 30 | Segundos entre refresh de quotes |
| use_heartbeat | true | Activar heartbeat loop |
| heartbeat_interval | 5 | Segundos entre heartbeats |
| scoring_check_interval | 60 | Segundos entre checks de scoring |

### Spec completo
Ver `LIQUIDITY_STRATEGY_SPEC.md` para arquitectura detallada, fórmulas, y roadmap.

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
- `strategy_type`: "directional" o "copy_trade"
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
- **Dry Run**: Inicializa cliente CLOB, valida órdenes, no las envía
- **Live**: Coloca órdenes reales contra Polymarket CLOB

### Flujo de una orden live
1. Risk checks: kill_switch, daily loss, max concurrent, balance suficiente
2. Resuelve `token_id` (YES/NO) del mercado
3. Crea y firma la orden con `ClobClient.create_order()`
4. Envía con `ClobClient.post_order()`
5. Monitorea estado (polling cada 5s) y cancela si no se llena en 30s
6. Resta coste del balance optimistamente al colocar

### Balance
- `_live_balance`: USDC libre consultado via API (None en paper)
- Se refresca cada hora automáticamente y al cambiar a modo live
- En live, si `suggested_bet > _live_balance` → trade rechazado (`insufficient_balance`)
- El dashboard muestra el balance real en live, o el simulado en paper

---

## Optimizaciones implementadas

1. **O(1) detector**: `check(token_id)` busca un solo mercado por token_id en vez de escanear todos
2. **Filtro crypto**: `tag="crypto"` en Gamma API reduce mercados de ~200 a ~20
3. **Sesión HMAC ligera**: Reemplazó EncryptedCookieStorage (dependía de cryptography, muy lento en VPS)
4. **Wallets deshabilitadas no se pollean**: `_poll_all_wallets` filtra por `_wallet_enabled`

---

## Fees de Polymarket

- **Taker fee**: 0.3% * min(price, 1-price) * size
- **Gas redeem**: ~$0.0005 por redención
- **Margen neto**: (1.0 - precio) - fee_per_share - gas_redeem

---

## Idioma

El usuario prefiere toda la comunicación en español.
