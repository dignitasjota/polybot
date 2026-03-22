# Polymarket Multi-Strategy Trading Bot

## Resumen

Bot de trading automatizado para Polymarket que ejecuta dos estrategias independientes en paralelo:
- **Directional**: detecta oportunidades de arbitraje en mercados crypto de 5 minutos
- **Copy Trade**: copia trades de wallets rentables con sistema de roles (primary/confirmation)

Corre en Docker (VPS Alemania), desplegado via Portainer. Actualmente en **paper trading** (simulado).

---

## Arquitectura

```
src/
  main.py              # Bot: orquestador principal, arranca cuentas + web
  config.py            # Dataclasses de configuración (cargadas desde TOML)
  account_runner.py    # Runner independiente por cuenta (directional o copy_trade)
  detector.py          # ClosingArbitrageDetector: detecta oportunidades directional
  copy_trader.py       # CopyTrader: monitorea wallets y genera oportunidades copy
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

## Estrategia 1: Directional (Closing Arbitrage + Up/Down)

### Closing Arbitrage
Compra tokens que cotizan a $0.97+ cuando el mercado está cerca de resolverse. Si el token gana, paga $1.00. Margen = $1.00 - precio - fees.

**Flujo:**
1. `GammaClient` descubre mercados activos (tag="crypto", poll cada 30s)
2. `WebSocketClient` se suscribe a precios en tiempo real
3. En cada update de precio, `detector.check(token_id)` evalúa O(1) ese mercado
4. Si precio >= probabilidad mínima según tier temporal → oportunidad
5. `Executor` registra el paper trade

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
5. Registra el paper trade y monitorea resolución

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

## Panel de Control Web

Accesible en `http://host:8080`. Protegido por login con cookie HMAC-SHA256.

### Rutas
| Ruta | Descripción |
|------|-------------|
| `/login` | Login (user/password contra SQLite + bcrypt) |
| `/` | Dashboard (read-only, auto-refresh cada 5s) |
| `/panel/copy-trade` | Gestión wallets (add/remove/toggle/set_role) + parámetros |
| `/panel/directional` | Kill switch + market filter (crypto_only) + parámetros |
| `/panel/settings` | Cambio password + audit log |
| `/api/report` | JSON completo de estado del bot |
| `/api/report/{account}` | JSON por cuenta específica |

### Hot-Reload
Los cambios desde el panel se aplican **inmediatamente** (mutación in-memory de dataclasses) y se persisten al archivo `config/config.toml` para sobrevivir reinicios.

Los roles y estado de wallets se almacenan en SQLite (`data/panel.db`), no en TOML.

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
- `execution_mode`: "paper" (actual), "dry_run", o "live"
- `[accounts.credentials]`: env vars para private key y API keys
- `[accounts.copy_trade]`: config específica de copy trading
- `[accounts.risk]`: overrides de riesgo por cuenta

**Parámetros que NO se exponen en panel** (requieren reinicio):
- `execution_mode` (paper/live)
- Credenciales API
- `strategy_type`
- Configuración WebSocket

---

## Despliegue

```bash
# Build y deploy via Docker Compose / Portainer
docker compose build && docker compose up -d

# Env vars necesarias en docker-compose.yml:
# PANEL_PASSWORD: password del admin (default: "admin")
# SESSION_SECRET: secreto para firmar cookies (generado si vacío)
# PRIVATE_KEY, POLYMARKET_API_KEY, etc.: credenciales (solo para modo live)
```

Volúmenes persistentes:
- `bot-logs`: `/app/logs` (logs JSON)
- `bot-data`: `/app/data` (SQLite panel.db)

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
