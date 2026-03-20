# Blueprint: Bot de Arbitraje Polymarket — Desarrollo en Dos Etapas

**Versión**: 2.0
**Fecha**: 2026-03-20
**Estado**: Especificación técnica — no implementado

---

## Índice General

- **[ETAPA 1: Closing Arbitrage](#etapa-1-closing-arbitrage)** — VPS Alemania / Docker
- **[ETAPA 2: Bonding Arbitrage](#etapa-2-bonding-arbitrage)** — AWS eu-west-2 / Baja latencia

Ambas etapas son **independientes**. La Etapa 1 es un producto completo por sí mismo. La Etapa 2 solo se aborda si la Etapa 1 demuestra viabilidad.

---

# ═══════════════════════════════════════════════════════════
# ETAPA 1: CLOSING ARBITRAGE
# ═══════════════════════════════════════════════════════════

## E1.1 Resumen

| | |
|---|---|
| **Estrategia** | Comprar el token ganador cuando cotiza < $1.00 cerca de la resolución |
| **Edge** | Evaluación de probabilidad + velocidad de reacción a resolución |
| **Infraestructura** | VPS existente en Alemania con Docker |
| **Stack** | Python puro (py-clob-client) |
| **Latencia requerida** | No crítica (~10-20ms a Londres es aceptable) |
| **Riesgo principal** | Riesgo de mercado (el resultado puede cambiar) |

### ¿Por qué la velocidad no es crítica aquí?

En Closing Arbitrage la oportunidad dura **minutos u horas** (un token a $0.96 que converge lentamente a $1.00). No compites por milisegundos contra otros bots — compites por **evaluar correctamente** si el resultado es seguro. La latencia de ~15ms desde Alemania a Londres es irrelevante en este contexto.

---

## E1.2 Infraestructura

### VPS Alemania + Docker

```
┌──────────────────────────────────────┐
│           VPS ALEMANIA               │
│                                      │
│  ┌────────────────────────────────┐  │
│  │     Docker Container           │  │
│  │                                │  │
│  │  ┌──────────────────────────┐  │  │
│  │  │   Bot Closing Arbitrage  │  │  │
│  │  │   (Python)               │  │  │
│  │  └──────────────────────────┘  │  │
│  │                                │  │
│  │  ┌──────────┐ ┌─────────────┐  │  │
│  │  │ config/  │ │ logs/       │  │  │
│  │  │ .toml    │ │ .jsonl      │  │  │
│  │  └──────────┘ └─────────────┘  │  │
│  └────────────────────────────────┘  │
│                                      │
│  Volúmenes montados:                 │
│  - ./config:/app/config              │
│  - ./logs:/app/logs                  │
│  - ./data:/app/data                  │
└──────────────────────────────────────┘
```

**Dockerfile base:**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
CMD ["python", "-m", "src.main"]
```

**docker-compose.yml:**

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    volumes:
      - ./config:/app/config:ro
      - ./logs:/app/logs
      - ./data:/app/data
    env_file:
      - stack.env
```

### Despliegue via Portainer

El VPS ya dispone de **Portainer** y **Nginx Proxy Manager**. El flujo de despliegue es:

```
1. Push a repositorio Git
2. Portainer → Stack → Git Repository → Pull & Build
3. Variables de entorno (.env) se configuran en Portainer (stack.env)
   - NUNCA se suben al repositorio Git
   - Portainer las inyecta al contenedor en cada deploy
4. Logs accesibles desde Portainer UI
5. Restart/stop/rebuild desde el panel

Secretos a configurar en Portainer (stack.env):
  PRIVATE_KEY=0x...
  PROXY_ADDRESS=0x...
  POLYMARKET_API_KEY=...
  POLYMARKET_SECRET=...
  POLYMARKET_PASSPHRASE=...
```

> **Nota:** Nginx Proxy Manager está disponible en el VPS pero no es necesario
> para el bot (no expone puertos web). Si en el futuro se añade un dashboard,
> se rutearía por NPM.

**Latencia estimada desde Alemania:**

| Destino | Latencia |
|---|---|
| `clob.polymarket.com` (Londres) | ~10-20ms |
| `ws-subscriptions-clob.polymarket.com` | ~10-20ms |
| Nodo RPC Polygon (Alchemy EU) | ~5-15ms |

Perfectamente suficiente para Closing Arbitrage.

---

## E1.3 Archivo de Configuración

Toda la configuración de riesgo y estrategia se lee de `config/config.toml`. El bot lo carga al arranque y puede recargarlo en caliente (SIGHUP o file watcher).

```toml
# config/config.toml — Closing Arbitrage

[strategy]
enabled = true
name = "closing_arbitrage"

# Criterios de entrada
min_implied_probability = 0.95    # Solo tokens con precio ≥ $0.95 (implica ≥95% prob)
max_time_to_resolution = "24h"    # Solo mercados que resuelven en <24h
min_margin_net = 0.008            # Margen mínimo por share tras fees ($0.008)

[risk]
max_bet_per_trade = 200.0         # Máximo invertido por trade ($)
max_position_per_market = 400.0   # Exposición máxima por mercado ($)
max_total_exposure = 1000.0       # Exposición total abierta ($)
max_daily_loss = 100.0            # Stop-loss diario ($)
max_concurrent_positions = 5      # Posiciones abiertas simultáneas
kill_switch = false               # true = detener todo inmediatamente

[data]
stale_data_threshold_seconds = 5  # Pausar si datos >5s antiguos
gamma_poll_interval_seconds = 300 # Polling Gamma API cada 5 min
max_markets_monitored = 20        # Máximo mercados a monitorear simultáneamente

[websocket]
ping_interval_seconds = 10
pong_timeout_seconds = 5
reconnect_max_delay_ms = 2000
reconnect_jitter_pct = 20
fallback_rest_interval_ms = 500   # Polling REST si WS caído

[logging]
level = "INFO"                    # DEBUG | INFO | WARNING | ERROR
format = "json"                   # json | text
file = "/app/logs/bot.jsonl"
max_file_size_mb = 100
rotate_count = 5
```

**Secretos (configurados en Portainer como stack.env, NUNCA en config.toml ni en Git):**

```env
PRIVATE_KEY=0x...
PROXY_ADDRESS=0x...
POLYMARKET_API_KEY=...
POLYMARKET_SECRET=...
POLYMARKET_PASSPHRASE=...
```

---

## E1.4 Conectividad y Consumo de Datos

### APIs utilizadas

| API | Uso | Frecuencia | Auth |
|---|---|---|---|
| **Gamma** (`gamma-api.polymarket.com`) | Descubrir mercados, obtener tokenId/conditionId, metadata de resolución | Arranque + cada 5 min | No |
| **CLOB REST** (`clob.polymarket.com`) | Precios, ejecución de órdenes | Bajo demanda | L2 HMAC |
| **CLOB WebSocket** (`ws-subscriptions-clob.polymarket.com`) | Precios en tiempo real, evento `market_resolved` | Conexión persistente | No (market) / API Key (user) |
| **Data** (`data-api.polymarket.com`) | Posiciones, P&L, reconciliación | Post-trade + cada 30s | L2 HMAC |

### WebSocket — Canal Market

```json
{
  "assets_ids": ["<YES_token_id>", "<NO_token_id>"],
  "type": "market",
  "custom_feature_enabled": true
}
```

**Eventos críticos para Closing Arbitrage:**

| Evento | Importancia | Uso |
|---|---|---|
| `market_resolved` | **MÁXIMA** | Señal de que el mercado ha resuelto → `winning_asset_id` conocido |
| `best_bid_ask` | Alta | Precio actual del token ganador |
| `last_trade_price` | Media | Confirmar a qué precio se está ejecutando realmente |
| `price_change` | Media | Cambios en el order book |

### WebSocket — Canal User

```json
{
  "auth": {"apiKey": "...", "secret": "...", "passphrase": "..."},
  "markets": ["<condition_id>"],
  "type": "user"
}
```

| Evento | Uso |
|---|---|
| `trade` (MATCHED→MINED→CONFIRMED) | Confirmar que nuestra orden se ejecutó |
| `order` (PLACEMENT/CANCELLATION) | Estado de nuestras órdenes |

### Heartbeat y Reconexión

```
PING cada 10s → Esperar PONG en 5s
Si no PONG → Reconexión con exponential backoff:
  0ms → 100ms → 500ms → 1000ms → 2000ms (cap)
  Jitter ±20%

Si WS caído >2s → Fallback a REST polling (GET /price cada 500ms)
Si datos >5s stale → PAUSAR ejecución

Cloudflare:
  - User-Agent descriptivo
  - No múltiples WS desde misma IP
  - Si 403/1020 → Esperar 60s
```

---

## E1.5 Autenticación

### Nivel 1 (L1) — Derivación de credenciales (una sola vez)

Firma EIP-712 con la private key sobre el dominio `ClobAuthDomain` (chainId: 137).
Devuelve: `apiKey`, `secret`, `passphrase`.

### Nivel 2 (L2) — Cada request de trading

Headers HMAC-SHA256:
```
POLY_ADDRESS: <wallet_address>
POLY_SIGNATURE: HMAC-SHA256(secret, timestamp + method + path + body)
POLY_TIMESTAMP: <unix_timestamp>
POLY_API_KEY: <api_key>
POLY_PASSPHRASE: <passphrase>
```

### Firma de órdenes EIP-712

```
Order {
  salt, maker, signer, taker (0x0),
  tokenId, makerAmount, takerAmount,
  expiration, nonce, feeRateBps,
  side (0=BUY, 1=SELL), signatureType
}
```

En Python con `py-order-utils` la firma tarda ~2-5ms. Aceptable para Closing Arbitrage.

---

## E1.6 Rate Limits

| Endpoint | Burst (10s) | RPS seguro (70%) |
|---|---|---|
| `POST /order` | 3,500 | ~300 |
| `GET /price` | 1,500 | ~120 |
| `GET /book` | 1,500 | ~120 |
| General CLOB | 9,000 | ~750 |

Para Closing Arbitrage el consumo será **muy bajo** (pocas órdenes por hora). No necesitamos un rate limiter sofisticado — basta un simple token bucket básico como protección.

---

## E1.7 Motor de Estrategia: Closing Arbitrage

### Lógica de detección y entrada

```
FLUJO DE DECISIÓN:

1. DESCUBRIMIENTO (Gamma API, cada 5 min):
   - Buscar mercados con resolución en <24h
   - Obtener tokenIds (YES/NO) y conditionId
   - Suscribirse al WebSocket Market channel

2. MONITOREO CONTINUO (WebSocket):
   - Recibir precios en tiempo real
   - Para cada mercado monitoreado:
     precio_token_candidato = max(precio_YES, precio_NO)
     implied_probability = precio_token_candidato

3. SEÑALES DE ENTRADA:
   a. PRE-RESOLUCIÓN:
      - implied_probability ≥ 0.95 (configurable)
      - Tiempo hasta resolución < 24h (configurable)
      - Margen neto estimado > min_margin_net
      - No excede límites de riesgo

   b. POST-RESOLUCIÓN (más seguro, menos margen):
      - Evento "market_resolved" recibido
      - winning_asset_id identificado
      - Precio del ganador aún < $1.00
      - VERIFICAR: ¿Hay disputa UMA activa? Si sí → NO ENTRAR

4. EJECUCIÓN:
   - POST /order: Limit BUY del token ganador
   - Tipo: GTC o GTD (con expiración corta, ej. 5 min)
   - Tamaño: min(max_bet_per_trade / precio, depth disponible)

5. POST-EJECUCIÓN:
   - Monitorear via User WS channel: MATCHED → MINED → CONFIRMED
   - Esperar resolución final del mercado
   - Redeem tokens ganadores → $1.00 cada uno
```

### Cálculo de margen neto

```
Margen_Neto = (1.00 - Precio_Compra) - Fee - Gas_Redeem

Donde:
  Fee = 0.003 × min(precio, 1-precio) × cantidad
  Gas_Redeem ≈ $0.004

Ejemplo:
  Compra 500 shares a $0.96
  Inversión: $480
  Retorno al resolver: $500
  Fee: 0.003 × min(0.96, 0.04) × 500 = $0.06
  Gas: $0.004
  Margen Neto: $20.00 - $0.06 - $0.004 = $19.94 (4.15%)

Ejemplo conservador:
  Compra 200 shares a $0.99
  Inversión: $198
  Retorno: $200
  Fee: 0.003 × min(0.99, 0.01) × 200 = $0.006
  Margen Neto: $2.00 - $0.006 - $0.004 = $1.99 (1.0%)
```

### Gestión de riesgo específica

```
RIESGOS Y MITIGACIONES:

1. RESULTADO INCORRECTO (el "seguro" pierde):
   Impacto: Pérdida total de la posición
   Mitigación:
   - min_implied_probability ≥ 0.95 (solo eventos muy probables)
   - max_bet_per_trade limita la pérdida máxima por evento
   - max_daily_loss corta el bot si acumula pérdidas
   - Diversificar: max_concurrent_positions en diferentes mercados

2. DISPUTA UMA (mercado resuelto pero disputado):
   Impacto: Resolución se revierte, precio cae
   Mitigación:
   - Verificar estado del oráculo UMA antes de entrar post-resolución
   - Si se detecta disputa → NO ENTRAR o SALIR inmediatamente

3. MERCADO ILÍQUIDO:
   Impacto: No poder comprar al precio deseado, slippage
   Mitigación:
   - Verificar profundidad del book antes de ejecutar
   - Si depth < 2× order_size → Reducir tamaño o no ejecutar

4. DESCONEXIÓN EN MOMENTO CRÍTICO:
   Impacto: Perder señal de resolución o cambio de precio
   Mitigación:
   - Fallback a REST polling automático
   - Posiciones abiertas siguen siendo válidas (el token ya es nuestro)
   - Redeem se puede hacer en cualquier momento post-resolución
```

---

## E1.8 Operaciones On-Chain

Para Closing Arbitrage, las únicas operaciones on-chain necesarias son:

| Operación | Cuándo | Gas estimado |
|---|---|---|
| `approve` USDC.e → CTF Exchange | Una vez (setup) | ~$0.002 |
| `redeem` tokens ganadores | Post-resolución | ~$0.004 |

**Contratos relevantes:**

| Contrato | Dirección |
|---|---|
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Neg Risk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| Conditional Tokens (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC.e (Collateral) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |

No se necesita Dynamic Gas Bidding complejo. Gas fijo conservador (baseFee × 1.2 + 30 gwei priority) es suficiente para el redeem.

---

## E1.9 Stack Técnico

```
Python 3.12
├── py-clob-client          # SDK oficial Polymarket CLOB
├── py-order-utils          # Firma EIP-712
├── web3                    # Operaciones on-chain (redeem)
├── websockets              # WebSocket client
├── structlog               # Logging estructurado (JSON)
├── tomli                   # Lectura de config.toml
├── aiohttp                 # HTTP async para Gamma/Data API
└── schedule                # Tareas periódicas (polling Gamma)
```

---

## E1.10 Fases de Implementación — Etapa 1

### Fase 1.1: Observador (Semanas 1-2)

**Objetivo:** Recopilar datos reales sin ejecutar trades.

```
ENTREGABLES:
├── Conexión WebSocket Market channel
│   ├── Suscripción a mercados con resolución próxima
│   ├── Recepción de eventos: price_change, best_bid_ask, market_resolved
│   └── Heartbeat + reconexión automática
│
├── Integración Gamma API
│   ├── Descubrir mercados con resolución en <24h
│   ├── Filtrar por enableOrderBook=true y liquidez mínima
│   └── Polling cada 5 min
│
├── Detector de oportunidades (sin ejecución)
│   ├── Identificar tokens con precio ≥ $0.95
│   ├── Calcular margen neto estimado
│   ├── Loguear: timestamp, mercado, precio, margen, depth, tiempo_a_resolución
│   └── Loguear eventos market_resolved y precio post-resolución
│
├── Docker + config.toml
│   ├── Dockerfile + docker-compose.yml
│   ├── Archivo de configuración con todos los parámetros
│   └── Logging estructurado a archivo .jsonl
│
└── CRITERIO DE ÉXITO:
    ├── WS estable >24h
    ├── Dataset de oportunidades con métricas reales
    ├── Respuesta a: ¿Cuántas oportunidades hay por día?
    ├── Respuesta a: ¿Cuánto margen tienen en promedio?
    └── Respuesta a: ¿Cuánto tiempo duran?
```

### Fase 1.2: Paper Trading (Semanas 3-4)

**Objetivo:** Simular trades y validar la estrategia.

```
ENTREGABLES:
├── Simulador de ejecución
│   ├── Simular POST /order con delay realista (~20ms)
│   ├── Simular fees reales (taker 30bps)
│   └── Simular slippage basado en depth real
│
├── Motor de estrategia completo
│   ├── Detección → Decisión → Ejecución simulada
│   ├── Cálculo de margen neto con todos los costes
│   └── Gestión de riesgo (límites del config.toml)
│
├── Autenticación CLOB (preparar para Fase 1.3)
│   ├── Derivar API credentials (L1)
│   ├── Verificar lectura de posiciones via Data API
│   └── Conectar User WS channel
│
├── Tracking de P&L simulado
│   ├── Portfolio virtual con balance inicial
│   ├── Registro de cada trade simulado
│   └── Métricas: win rate, P&L acumulado, drawdown
│
└── CRITERIO DE ÉXITO:
    ├── P&L simulado positivo durante >7 días
    ├── Win rate >90% (dado min_implied_probability=0.95)
    ├── Drawdown máximo aceptable
    └── Decisión GO/NO-GO para capital real
```

### Fase 1.3: Mainnet (Semanas 5-8)

**Objetivo:** Trading real con capital limitado, escalando gradualmente.

```
ENTREGABLES:
├── Ejecución real
│   ├── POST /order contra CLOB real
│   ├── Monitoreo via User WS: MATCHED → MINED → CONFIRMED
│   └── Redeem on-chain de tokens ganadores
│
├── Escalado gradual
│   ├── Semana 5: $50 capital, 1-2 mercados, max_bet=$25
│   ├── Semana 6: $100 capital, 3 mercados, max_bet=$50
│   ├── Semana 7: $200 capital, 5 mercados, max_bet=$100
│   └── Semana 8+: Según resultados, hasta los límites del config
│
├── Monitoreo
│   ├── Logging completo de cada trade
│   ├── Alertas: pérdida diaria, desconexión, balance bajo
│   └── Kill switch en config.toml (kill_switch = true)
│
├── Seguridad
│   ├── Private key en Portainer stack.env (nunca en config.toml ni en Git)
│   ├── Balance mínimo en wallet
│   └── Docker con restart=unless-stopped (gestionado via Portainer)
│
└── CRITERIO DE ÉXITO:
    ├── Profit real positivo durante >14 días
    ├── Drawdown <5% del capital
    └── Decisión: ¿Escalar? ¿Añadir Bonding Arbitrage (Etapa 2)?
```

### Diagrama de secuencia: Closing Arbitrage

```
Bot                    Gamma API      CLOB WS              CLOB REST         Polygon
 │                        │              │                     │                 │
 │ ──GET /events──────────>              │                     │                 │
 │ <──mercados próximos───│              │                     │                 │
 │                        │              │                     │                 │
 │ ──subscribe(token_ids)──────────────> │                     │                 │
 │                        │              │                     │                 │
 │ ◄──best_bid_ask────────────────────── │                     │                 │
 │ [YES=$0.96, prob alta, resolución 2h] │                     │                 │
 │ [Margen: $0.04 - fees = $0.039]      │                     │                 │
 │ [Verifica riesgo: OK]  │              │                     │                 │
 │                        │              │                     │                 │
 │ ─────────────POST /order (BUY YES $0.96 × 200)───────────> │                 │
 │ ◄────────────{orderID}──────────────────────────────────────│                 │
 │                        │              │                     │                 │
 │ ◄──trade(MATCHED)──────────────────── │                     │                 │
 │ ◄──trade(MINED)────────────────────── │                     │                 │
 │                        │              │                     │                 │
 │ [Posición: 200 YES @ $0.96]          │                     │                 │
 │ [Espera resolución...]  │              │                     │                 │
 │                        │              │                     │                 │
 │ ◄──market_resolved(winner=YES)─────── │                     │                 │
 │                        │              │                     │                 │
 │ ────────────────────redeem(200 YES)───────────────────────────────────────> │
 │ ◄───────────────────200 USDC.e────────────────────────────────────────────  │
 │                        │              │                     │                 │
 │ [Profit: $200 - $192 - $0.024 - $0.004 = $7.97]           │                 │
```

---

# ═══════════════════════════════════════════════════════════
# ETAPA 2: BONDING ARBITRAGE
# ═══════════════════════════════════════════════════════════

> **PRERREQUISITO:** Solo abordar si la Etapa 1 demuestra que el sistema es estable,
> el bot opera correctamente, y se ha validado la infraestructura base.

## E2.1 Resumen

| | |
|---|---|
| **Estrategia** | Comprar YES + NO cuando su suma < $1.00 (arbitraje puro) |
| **Edge** | Velocidad de detección y ejecución |
| **Infraestructura** | **AWS eu-west-2 (Londres)** — co-ubicación con matching engine |
| **Stack** | Rust (hot path) + Python (orquestación) |
| **Latencia requerida** | **CRÍTICA** (~1-5ms objetivo) |
| **Riesgo principal** | Riesgo de ejecución (partial fills) |

### ¿Por qué aquí SÍ importa la velocidad?

Las oportunidades de Bonding Arbitrage (YES + NO < $1.00) duran **milisegundos**. Múltiples bots compiten por la misma oportunidad. El más rápido en detectar y ejecutar ambas piernas gana. Desde Alemania (~15ms) llegas tarde; desde Londres (<1ms) compites.

---

## E2.2 Infraestructura: Migración a AWS eu-west-2

### Hallazgo crítico: Ubicación del Matching Engine

> **El motor de matching de Polymarket opera en AWS `eu-west-2` (Londres).**

| Origen | Latencia a `clob.polymarket.com` |
|---|---|
| QuantVPS Dublin | **0.83ms** |
| AWS eu-west-2 (Londres) | **<1ms** (estimado, misma región) |
| AWS eu-west-1 (Irlanda) | ~2-5ms |
| VPS Alemania | ~10-20ms |
| US East (Virginia) | ~50-100ms |

### Instancia recomendada

| Componente | Instancia | Justificación |
|---|---|---|
| Motor de ejecución | `c7gn.medium` (1 vCPU, 2GB) | ARM Graviton3 + Enhanced Networking ENA 25 Gbps |
| Escalado | `c7gn.xlarge` (4 vCPU, 8GB) | Si >50 mercados simultáneos |

### Configuraciones de red críticas

```
Enhanced Networking (ENA): Habilitado por defecto en C7gn
Placement Group: Tipo "cluster" en la misma AZ
Jumbo Frames: MTU 9001 para tráfico intra-VPC
TCP Tuning:
  net.ipv4.tcp_nodelay = 1
  net.ipv4.tcp_low_latency = 1
  net.core.somaxconn = 65535
  net.ipv4.tcp_fastopen = 3
  net.ipv4.tcp_tw_reuse = 1
```

### Coste mensual estimado

| Recurso | Coste/mes |
|---|---|
| c7gn.medium on-demand | ~$45 |
| c7gn.medium reserved 1yr | ~$28 |
| EBS gp3 20GB | ~$2 |
| Transferencia de datos | ~$4 |
| **Total** | **~$35-50/mes** |

---

## E2.3 Configuración adicional para Bonding Arbitrage

Se añade al `config.toml` una sección independiente:

```toml
# Añadir a config/config.toml — Bonding Arbitrage (Etapa 2)

[bonding_arbitrage]
enabled = true

[bonding_arbitrage.strategy]
min_margin_net = 0.005            # Margen mínimo por share tras fees ($0.005)
min_book_depth_ratio = 2.0        # Depth mínima = 2× order_size en AMBAS piernas
execute_illiquid_leg_first = true  # Ejecutar pierna menos líquida primero

[bonding_arbitrage.risk]
max_bet_per_trade = 500.0         # Máximo invertido por arbitraje (suma ambas piernas) ($)
max_position_per_market = 1000.0  # Exposición máxima por mercado ($)
max_total_exposure = 3000.0       # Exposición total ($)
max_daily_loss = 50.0             # Stop-loss diario ($) — solo por partial fills
max_concurrent_arbs = 3           # Arbitrajes simultáneos
max_partial_fill_loss = 15.0      # Pérdida máxima si una pierna falla ($)

[bonding_arbitrage.partial_fill]
leg2_timeout_ms = 500             # Timeout para ejecutar segunda pierna
retry_with_adjusted_price = true  # Reintentar pierna 2 con precio ajustado
max_negative_margin_hold_pct = 1.0  # Mantener si margen negativo <1%
emergency_exit_threshold_pct = 2.0  # Market sell pierna 1 si margen negativo >2%
consecutive_fails_blacklist = 3   # Blacklist mercado tras N partial fills seguidos
blacklist_duration_minutes = 5
```

---

## E2.4 Motor de Estrategia: Bonding Arbitrage

### Lógica de detección y entrada

```
FLUJO DE DECISIÓN:

1. MONITOREO CONTINUO (WebSocket, book completo):
   Para cada mercado:
     spread = 1.00 - (best_ask_YES + best_ask_NO)
     Si spread > 0:
       → Calcular margen neto

2. CÁLCULO DE MARGEN NETO:
   Margen_Bruto = 1.00 - (best_ask_YES + best_ask_NO)
   Slippage_YES = f(order_size, depth_YES)
   Slippage_NO  = f(order_size, depth_NO)
   Fee_YES = 0.003 × min(price_YES, 1-price_YES) × size
   Fee_NO  = 0.003 × min(price_NO, 1-price_NO) × size
   Gas_Merge = ~$0.006
   Margen_Neto = Margen_Bruto × size - Slippage - Fees - Gas

3. EJECUCIÓN (si Margen_Neto > min_margin_net × size):
   - Identificar pierna menos líquida
   - POST /orders (batch) con ambas piernas
   - O ejecutar pierna ilíquida primero → pierna líquida inmediatamente después

4. POST-EJECUCIÓN:
   - Monitorear ambos fills via User WS
   - Si ambas MATCHED → Merge on-chain (YES + NO → USDC.e)
   - Si partial fill → Activar lógica de mitigación
```

### Modelo de Slippage

```
Para un tamaño S en un book con profundidad D:

accumulated_cost = 0
accumulated_size = 0
for level in asks (sorted by price):
  available = min(level.size, S - accumulated_size)
  accumulated_cost += available × level.price
  accumulated_size += available
  if accumulated_size >= S: break

VWAP = accumulated_cost / accumulated_size
Slippage = VWAP - best_ask

ABORTAR SI:
- depth total < 2× order_size (en cualquier pierna)
- slippage > 50% del margen bruto
- spread bid-ask > 5%
```

### Partial Fills — Gestión de pierna única

```
Si solo pierna 1 ejecutada:

MATRIZ DE DECISIÓN:
┌──────────────────────┬────────────────┬───────────────────────┐
│ Precio pierna 2      │ Margen residual│ Acción                │
├──────────────────────┼────────────────┼───────────────────────┤
│ < objetivo + 1%      │ Positivo       │ Ejecutar pierna 2     │
│ objetivo + 1-2%      │ Ligero negativo│ Limit order, esperar  │
│ > objetivo + 2%      │ Muy negativo   │ Market sell pierna 1  │
└──────────────────────┴────────────────┴───────────────────────┘

CIRCUIT BREAKER:
- 3 partial fills consecutivos en mismo mercado → Blacklist 5 min
- Pérdida acumulada por partial fills > max_partial_fill_loss → Pausar 15 min
```

### Operaciones On-Chain adicionales

| Operación | Cuándo | Gas estimado |
|---|---|---|
| `approve` USDC.e | Una vez (setup) | ~$0.002 |
| `merge` YES + NO → USDC.e | Tras arbitraje completado | ~$0.006 |
| `split` USDC.e → YES + NO | Si es más eficiente que comprar | ~$0.006 |

### Dynamic Gas Bidding (solo para merge/split/redeem)

```
Operaciones rutinarias: baseFee × 1.1 + 30 gwei priority
Operaciones urgentes:   baseFee × 1.5 + 50 gwei priority
Cap máximo: 500 gwei

Si TX no incluida en 5s  → Reenviar +20% gas (mismo nonce)
Si TX no incluida en 15s → Reenviar +50% gas
Máximo 3 reintentos → Abortar
```

---

## E2.5 Stack Técnico — Etapa 2

### Arquitectura Híbrida

```
┌─────────────────────────────────────────────────────┐
│              AWS eu-west-2 (Londres)                 │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │        ORQUESTACIÓN (Python)                   │  │
│  │  - Config, logging, alertas                    │  │
│  │  - Gamma API (descubrimiento)                  │  │
│  │  - Data API (reconciliación)                   │  │
│  │  - Dashboard / métricas                        │  │
│  └──────────┬────────────────────────┬────────────┘  │
│             │  Unix Socket / gRPC    │              │
│  ┌──────────▼──────────┐  ┌──────────▼───────────┐  │
│  │ EJECUCIÓN (Rust)    │  │ INGESTA (Rust)       │  │
│  │ - Firma EIP-712     │  │ - WebSocket client   │  │
│  │ - Cálculo margen    │  │ - Order book local   │  │
│  │ - Rate limiter      │  │ - Detección spreads  │  │
│  │ - HTTP client       │  │ - Reconexión auto    │  │
│  └─────────────────────┘  └──────────────────────┘  │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │        ON-CHAIN (Rust / alloy)                 │  │
│  │  - Merge/Split/Redeem                          │  │
│  │  - Gas bidding                                 │  │
│  │  - Monitoreo eventos                           │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**¿Por qué Rust para Bonding y no para Closing?**
- Closing: Oportunidades duran minutos → Python (~5ms firma) es más que suficiente
- Bonding: Oportunidades duran milisegundos → Rust (~0.5ms firma) marca la diferencia

### Dependencias Rust

```toml
[dependencies]
tokio = { version = "1", features = ["full"] }
tokio-tungstenite = "0.21"
alloy = "0.1"                    # EIP-712, ABI
reqwest = { version = "0.12", features = ["json"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
rust_decimal = "1.34"
crossbeam = "0.8"
tracing = "0.1"
```

---

## E2.6 Fases de Implementación — Etapa 2

### Fase 2.1: Observador de Bonding desde VPS Alemania (Semana 9-10)

> Antes de migrar a AWS, medir las oportunidades reales desde la infra existente.

```
ENTREGABLES:
├── Añadir detector de Bonding Arbitrage al bot existente
│   ├── Monitorear spread: 1.00 - (best_ask_YES + best_ask_NO)
│   ├── Calcular margen neto incluyendo fees y slippage estimado
│   └── Loguear cada oportunidad: duración, margen, depth
│
└── CRITERIO DE ÉXITO (GO/NO-GO para migración AWS):
    ├── ¿Cuántas oportunidades con margen neto >0 hay por día?
    ├── ¿Cuánto duran? (si <50ms, necesitamos AWS)
    ├── ¿Qué profundidad tienen? (¿merece la pena por el tamaño?)
    └── ¿La frecuencia × margen justifica $35-50/mes de AWS?
```

### Fase 2.2: Migración a AWS + Paper Trading (Semanas 11-13)

```
ENTREGABLES:
├── Desplegar en AWS eu-west-2
│   ├── c7gn.medium con tuning de red
│   ├── Docker (misma imagen, nueva ubicación)
│   └── Medir latencia real al CLOB (<1ms esperado)
│
├── Implementar motor Rust (si datos justifican la velocidad)
│   ├── WebSocket client + order book en Rust
│   ├── Firma EIP-712 en Rust
│   └── IPC con Python
│
├── Paper trading de Bonding Arbitrage
│   ├── Simular ejecución de ambas piernas
│   ├── Simular partial fills
│   └── P&L simulado
│
└── CRITERIO DE ÉXITO:
    ├── Latencia <5ms detección-a-decisión
    ├── P&L simulado positivo >7 días
    └── Partial fills simulados <10%
```

### Fase 2.3: Mainnet Bonding (Semanas 14+)

```
ENTREGABLES:
├── Ejecución real de Bonding Arbitrage
├── Escalado gradual ($100 → $250 → $500)
├── Monitoreo: Prometheus + Grafana
├── Seguridad: AWS Secrets Manager para keys
└── Ambas estrategias corriendo simultáneamente
```

---

## Anexos

### A.1 URLs de referencia

| Servicio | URL |
|---|---|
| CLOB API | `https://clob.polymarket.com` |
| Gamma API | `https://gamma-api.polymarket.com` |
| Data API | `https://data-api.polymarket.com` |
| WebSocket Market | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| WebSocket User | `wss://ws-subscriptions-clob.polymarket.com/ws/user` |
| Status | `https://status.polymarket.com` |

### A.2 Contratos Polygon Mainnet (Chain ID 137)

| Contrato | Dirección |
|---|---|
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Neg Risk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| Neg Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| Conditional Tokens (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC.e (Collateral) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| USDC (native) | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` |

### A.3 Tick Sizes

| Rango de precio | Tick size |
|---|---|
| $0.04 - $0.96 | `0.01` |
| < $0.04 o > $0.96 | `0.001` o `0.0001` |

### A.4 Fees

```
fee = 0.003 × min(price, 1-price) × size    (taker, 30bps)
rebate = 0.002 × min(price, 1-price) × size (maker, 20bps)
```

### A.5 EIP-712 Order Struct

```
Order {
  salt:          uint256
  maker:         address
  signer:        address
  taker:         address  (0x0 for public)
  tokenId:       uint256
  makerAmount:   uint256
  takerAmount:   uint256
  expiration:    uint256
  nonce:         uint256
  feeRateBps:    uint256
  side:          uint8    (0=BUY, 1=SELL)
  signatureType: uint8    (0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE)
}

Domain: { name: "ClobAuthDomain", version: "1", chainId: 137 }
```
