# Blueprint de Arquitectura: Bot de Arbitraje Polymarket CLOB

**Versión**: 1.0
**Fecha**: 2026-03-20
**Estado**: Especificación técnica — no implementado

---

## Tabla de Contenidos

1. [Resumen Ejecutivo](#1-resumen-ejecutivo)
2. [Infraestructura y Co-ubicación](#2-infraestructura-y-co-ubicación)
3. [Conectividad y Consumo de Datos](#3-conectividad-y-consumo-de-datos)
4. [Gestión de Rate-Limits y Ejecución](#4-gestión-de-rate-limits-y-ejecución)
5. [Motor de Estrategia y Riesgo](#5-motor-de-estrategia-y-riesgo)
6. [Stack de Software](#6-stack-de-software)
7. [Fases de Implementación](#7-fases-de-implementación)
8. [Anexos](#8-anexos)

---

## 1. Resumen Ejecutivo

### Estrategias objetivo

| Estrategia | Condición | Ganancia esperada |
|---|---|---|
| **Bonding Arbitrage** | Precio(YES) + Precio(NO) < $1.00 | Diferencia menos costes |
| **Closing Arbitrage** | Token ganador converge a $1.00 al resolver el mercado | Spread de convergencia |

### Hallazgo crítico: Ubicación del Matching Engine

> **El motor de matching de Polymarket opera en AWS `eu-west-2` (Londres), NO en `us-east-1`.**

Mediciones de latencia conocidas:

| Origen | Destino | Latencia |
|---|---|---|
| QuantVPS Dublin | `clob.polymarket.com` | **0.83ms** |
| QuantVPS Dublin | `ws-live-data.polymarket.com` | **0.76ms** |
| QuantVPS Dublin | `gamma-api.polymarket.com` | **0.91ms** |
| US East (Virginia) | `clob.polymarket.com` | ~50-100ms |

Esto significa que desplegar en us-east-1 introduce **50-100x más latencia** que un servidor en Irlanda/Londres. Para un bot de arbitraje competitivo, esto es inaceptable.

---

## 2. Infraestructura y Co-ubicación

### 2.1 Región óptima: AWS eu-west-1 (Irlanda) o eu-west-2 (Londres)

**Justificación:**

- El matching engine de Polymarket está en `eu-west-2`. La latencia de red intra-AZ en AWS es <1ms, e inter-región eu-west-1↔eu-west-2 es ~2-5ms.
- Los principales proveedores de nodos RPC (Alchemy, QuickNode, Infura) tienen endpoints dedicados en Europa con latencias de 5-15ms para la red Polygon.
- Los validadores de Polygon (PoS) están distribuidos globalmente, pero la mayoría de los nodos RPC con baja latencia operan desde Europa y US-East.

**Descarte de us-east-1:** Aunque us-east-1 es popular para infraestructura cripto genérica, la penalización de ~70ms transatlántica al matching engine elimina cualquier ventaja de arbitraje en mercados competidos. Cada milisegundo cuenta cuando múltiples bots detectan la misma oportunidad.

### 2.2 Configuración de instancia recomendada

| Componente | Instancia | Justificación |
|---|---|---|
| **Motor de ejecución** | `c7gn.medium` (1 vCPU, 2GB) | Familia C7gn: ARM Graviton3 + Enhanced Networking ENA a 25 Gbps. Optimizada para throughput de red y computación. |
| **Escalado (Fase 3)** | `c7gn.xlarge` (4 vCPU, 8GB) | Si se monitorean >50 mercados simultáneos o se requiere procesamiento paralelo intensivo. |
| **Alternativa costo-eficiente** | `c7g.medium` + Placement Group | C7g estándar en cluster placement group para latencia intra-AZ mínima. |

**Configuraciones de red críticas:**

```
- Enhanced Networking (ENA): Habilitado por defecto en C7gn
- Placement Group: Tipo "cluster" en la misma AZ que el endpoint más cercano
- Jumbo Frames: MTU 9001 habilitado para tráfico intra-VPC
- TCP Tuning:
    net.ipv4.tcp_nodelay = 1          (deshabilitar Nagle)
    net.ipv4.tcp_low_latency = 1
    net.core.somaxconn = 65535
    net.ipv4.tcp_fastopen = 3         (TFO cliente+servidor)
    net.ipv4.tcp_tw_reuse = 1
```

### 2.3 Estimación de costes mensuales (Fase 1-2)

| Recurso | Coste estimado/mes |
|---|---|
| c7gn.medium (on-demand) | ~$45 |
| c7gn.medium (reserved 1yr) | ~$28 |
| EBS gp3 20GB | ~$2 |
| Transferencia de datos (~50GB) | ~$4 |
| **Total Fase 1-2** | **~$35-50/mes** |

---

## 3. Conectividad y Consumo de Datos

### 3.1 Arquitectura de APIs: Gamma vs CLOB vs Data

```
┌─────────────────────────────────────────────────────────────┐
│                    FLUJO DE DATOS                            │
│                                                             │
│  ┌──────────┐     ┌──────────┐     ┌──────────┐            │
│  │ Gamma API│     │ CLOB API │     │ Data API │            │
│  │ (REST)   │     │ (REST+WS)│     │ (REST)   │            │
│  └────┬─────┘     └────┬─────┘     └────┬─────┘            │
│       │                │                │                   │
│  Descubrimiento   Trading +        Posiciones +             │
│  de mercados      Order Book       Historial                │
│       │                │                │                   │
│  - Events/slugs   - GET /book      - Trades propios        │
│  - Token IDs      - POST /order    - Profit/loss           │
│  - Condition IDs  - WebSocket      - Balance               │
│  - Metadata       - Cancelaciones                           │
│                                                             │
│  Frecuencia:      Frecuencia:      Frecuencia:             │
│  1x al arranque   Continua (WS)    Bajo demanda            │
│  + polling c/5min  + REST backup   + reconciliación         │
└─────────────────────────────────────────────────────────────┘
```

| API | Uso en el bot | Frecuencia | Auth |
|---|---|---|---|
| **Gamma** (`gamma-api.polymarket.com`) | Descubrir mercados activos, obtener `tokenId`, `conditionId`, verificar `enableOrderBook` | Arranque + polling cada 5 min | No |
| **CLOB REST** (`clob.polymarket.com`) | Backup de precios, colocación/cancelación de órdenes | Bajo demanda (ejecución) | L2 HMAC |
| **CLOB WebSocket** (`ws-subscriptions-clob.polymarket.com`) | Stream en tiempo real de order book y trades | Conexión persistente | No (market) / API Key (user) |
| **Data** (`data-api.polymarket.com`) | Reconciliación de posiciones, P&L | Cada 30s o post-trade | L2 HMAC |

### 3.2 Sistema de ingesta WebSocket

#### Canales a suscribir

**Canal Market** (sin auth) — `wss://ws-subscriptions-clob.polymarket.com/ws/market`

```json
{
  "assets_ids": ["<YES_token_id>", "<NO_token_id>"],
  "type": "market",
  "custom_feature_enabled": true
}
```

Eventos relevantes para arbitraje:

| Evento | Uso | Latencia vs REST |
|---|---|---|
| `book` | Snapshot completo del order book al suscribirse y post-trade | Inmediato |
| `price_change` | Nuevas órdenes/cancelaciones, actualización incremental | ~0ms vs ~50-100ms polling |
| `best_bid_ask` | Spread actual (requiere `custom_feature_enabled: true`) | ~0ms |
| `last_trade_price` | Precio de última transacción ejecutada | ~0ms |
| `market_resolved` | Señal para Closing Arbitrage | Crítico |

**Canal User** (con auth) — `wss://ws-subscriptions-clob.polymarket.com/ws/user`

```json
{
  "auth": {"apiKey": "...", "secret": "...", "passphrase": "..."},
  "markets": ["<condition_id>"],
  "type": "user"
}
```

Eventos relevantes:

| Evento | Uso |
|---|---|
| `trade` (status: MATCHED→MINED→CONFIRMED) | Confirmar ejecución, detectar fills parciales |
| `order` (type: PLACEMENT/UPDATE/CANCELLATION) | Estado de nuestras órdenes en el book |

#### Mantenimiento del Order Book local

```
Estrategia de reconstrucción incremental:

1. Al conectar → Recibir snapshot "book" → Poblar estructura local
2. Evento "price_change" → Actualizar nivel de precio:
   - size > 0: Insertar/actualizar nivel
   - size = "0": Eliminar nivel
3. Validar integridad con hash del book (campo "hash" en snapshots)
4. Si hash diverge → Forzar re-suscripción para nuevo snapshot
```

### 3.3 Estrategia de Keep-Alive y Reconexión

```
POLÍTICA DE HEARTBEAT:
├── Market/User Channel:
│   ├── Enviar "PING" cada 10 segundos
│   ├── Esperar "PONG" dentro de 5 segundos
│   └── Si no PONG → Marcar conexión como degradada
│
├── RECONEXIÓN (Exponential Backoff):
│   ├── Intento 1: Inmediato (0ms)
│   ├── Intento 2: 100ms
│   ├── Intento 3: 500ms
│   ├── Intento 4: 1000ms
│   ├── Intento 5+: 2000ms (cap)
│   └── Jitter: ±20% aleatorio para evitar thundering herd
│
├── FALLBACK A REST:
│   ├── Si WS desconectado >2s → Activar polling REST
│   ├── GET /book cada 500ms (consume ~20 RPS del límite de 150/s)
│   ├── Suspender trading si datos >5s stale
│   └── Restaurar WS cuando reconecte → Desactivar polling
│
└── CLOUDFLARE CONSIDERATIONS:
    ├── Cloudflare puede terminar conexiones idle >100s
    ├── El PING cada 10s mantiene la conexión activa
    ├── User-Agent: Usar uno descriptivo, no genérico
    ├── No abrir múltiples WS desde la misma IP innecesariamente
    └── Si Cloudflare bloquea (HTTP 403/1020): Esperar 60s + rotar headers
```

---

## 4. Gestión de Rate-Limits y Ejecución

### 4.1 Rate Limits de Polymarket (por IP)

Los límites son **por ventana de 10 segundos** (burst) y **por ventana de 10 minutos** (sustained):

| Endpoint | Burst (10s) | Sustained (10min) | RPS seguro |
|---|---|---|---|
| `POST /order` | 3,500 | 36,000 | **~300** |
| `DELETE /order` | 3,000 | 30,000 | **~250** |
| `POST /orders` (batch) | 1,000 | 15,000 | **~80** |
| `GET /book` | 1,500 | — | **~120** |
| `GET /price` | 1,500 | — | **~120** |
| General CLOB | 9,000 | — | **~750** |

**Sistema de Rate-Limiting interno:**

```
Token Bucket por categoría de endpoint:

TRADING_BUCKET:
  capacity: 300 tokens (burst)
  refill_rate: 300 tokens/s
  sustained_limit: 60 tokens/s (para respetar 36,000/10min)

READING_BUCKET:
  capacity: 120 tokens
  refill_rate: 120 tokens/s

BATCH_BUCKET:
  capacity: 80 tokens
  refill_rate: 80 tokens/s

Reglas:
- Cada request consume 1 token del bucket correspondiente
- Si bucket vacío → Enqueue con prioridad (órdenes > cancelaciones > lecturas)
- Cola de prioridad: Órdenes de arbitraje activo > Mantenimiento > Lecturas
- Monitor: Si bucket <20% → Log warning, reducir actividad no-esencial
- Margen de seguridad: Operar al 70% del límite teórico
```

### 4.2 Firma EIP-712 y Meta-Transacciones

#### Flujo de autenticación de dos niveles

```
NIVEL 1 (L1) — Derivación de credenciales (una vez):
┌──────────┐    EIP-712 Sign    ┌──────────────┐
│ Private  │───────────────────>│ CLOB Server  │
│ Key      │  ClobAuthDomain    │              │
│          │<───────────────────│ Returns:     │
│          │  {apiKey, secret,  │ - apiKey     │
└──────────┘   passphrase}     │ - secret     │
                                │ - passphrase │
                                └──────────────┘

NIVEL 2 (L2) — Cada request de trading:
┌──────────┐    HMAC-SHA256     ┌──────────────┐
│ API      │───────────────────>│ CLOB Server  │
│ Creds    │  Headers:          │              │
│          │  POLY_ADDRESS      │ Valida HMAC  │
│          │  POLY_SIGNATURE    │ Ejecuta      │
│          │  POLY_TIMESTAMP    │              │
│          │  POLY_API_KEY      │              │
│          │  POLY_PASSPHRASE   │              │
└──────────┘                    └──────────────┘
```

#### Firma de órdenes EIP-712 (on-chain settlement)

Cada orden requiere una firma EIP-712 del struct `Order` con estos campos:

```
Order {
  salt:          uint256  (random, garantiza unicidad)
  maker:         address  (nuestra dirección)
  signer:        address  (dirección firmante — puede diferir si usamos proxy)
  taker:         address  (0x0 para órdenes públicas)
  tokenId:       uint256  (ID del token CTF ERC1155)
  makerAmount:   uint256  (tokens a vender)
  takerAmount:   uint256  (tokens a recibir)
  expiration:    uint256  (unix timestamp)
  nonce:         uint256  (para cancelaciones on-chain)
  feeRateBps:    uint256  (fee en basis points)
  side:          uint8    (0=BUY, 1=SELL)
  signatureType: uint8    (0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE)
}
```

**Impacto en latencia de la firma:**

| Operación | Tiempo estimado |
|---|---|
| Firma EIP-712 (secp256k1) | ~0.1-0.5ms (Rust/Go nativo) |
| Firma EIP-712 (Python ethers) | ~2-5ms |
| HMAC-SHA256 para headers L2 | ~0.01ms |
| **Total overhead de auth** | **~0.1-0.5ms (Rust) / ~2-5ms (Python)** |

**Optimización:** Pre-computar el domain separator y type hash al arranque. Solo el hash del mensaje cambia por orden.

### 4.3 Latencia del Relayer y Mitigación

El Relayer es el componente que toma las órdenes matcheadas off-chain y las somete como transacciones on-chain a Polygon.

```
Flujo de ejecución temporal:

t=0ms     Bot detecta oportunidad
t=0.5ms   Firma orden EIP-712
t=1ms     POST /order al CLOB
t=2-5ms   Matching engine procesa y matchea
t=5ms     WebSocket "trade" status=MATCHED
          ← A partir de aquí, el Relayer toma el control →
t=50-200ms  Relayer construye TX, firma, y envía a Polygon
t=2-4s      TX incluida en bloque Polygon (~2s block time)
t=2-4s      WebSocket "trade" status=MINED
t=~60s      Finalidad (128 bloques) → status=CONFIRMED
```

**Riesgos del Relayer:**

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Relayer congestionado | TX demorada, posición expuesta | Monitorear `status.polymarket.com`; no entrar en trades si relayer lento |
| Relayer falla (RETRYING) | Posible fill parcial | Lógica de timeout: si status no llega a MINED en 30s, activar hedging |
| TX reverted | Fondos devueltos pero oportunidad perdida | Verificar allowances y balances pre-trade |

**Rate limit del Relayer:** 25 requests/minuto en `/submit`. Esto NO afecta a las órdenes vía CLOB (el Relayer actúa internamente), pero sí a operaciones directas on-chain.

### 4.4 Dynamic Gas Bidding para Polygon

> **Nota importante:** Para operaciones estándar vía CLOB API (POST /order), el gas lo paga el Relayer de Polymarket, NO el usuario. El gas bidding solo es relevante para:
> - Operaciones directas contra los contratos (split/merge/redeem)
> - Aprobaciones de tokens (approve)
> - Cancelaciones on-chain (nonce-based)

Para esas operaciones directas:

```
Estrategia de Gas Bidding en Polygon:

1. ESTIMACIÓN BASE:
   - Consultar gasPrice via eth_gasPrice del nodo RPC
   - Polygon usa EIP-1559: baseFee + priorityFee (maxPriorityFeePerGas)
   - Base fee típico en Polygon: 30-100 gwei
   - Priority fee mínimo efectivo: 30 gwei

2. BIDDING DINÁMICO:
   - Para operaciones rutinarias (approve): baseFee × 1.1 + 30 gwei priority
   - Para operaciones urgentes (merge post-arbitrage): baseFee × 1.5 + 50 gwei priority
   - Para operaciones críticas (redeem pre-cierre): baseFee × 2.0 + 100 gwei priority
   - Cap máximo: 500 gwei total (protección contra spikes)

3. MONITOREO DE INCLUSIÓN:
   - Enviar TX → Esperar receipt con timeout de 5s
   - Si no incluida en 5s → Reenviar con +20% gas (speed-up TX, mismo nonce)
   - Si no incluida en 15s → Reenviar con +50% gas
   - Máximo 3 reintentos → Abortar y loguear

4. COSTE ESTIMADO POR OPERACIÓN:
   - Approve: ~50,000 gas × 100 gwei = 0.005 MATIC (~$0.002)
   - Split/Merge: ~150,000 gas × 100 gwei = 0.015 MATIC (~$0.006)
   - Redeem: ~100,000 gas × 100 gwei = 0.01 MATIC (~$0.004)
```

---

## 5. Motor de Estrategia y Riesgo

### 5.1 Bonding Arbitrage: Cálculo de Margen Neto

#### Condición de entrada

```
Margen_Bruto = 1.00 - (Best_Ask_YES + Best_Ask_NO)

Costes:
  Slippage_YES  = f(order_size, depth_YES)    // Estimado del impacto en precio
  Slippage_NO   = f(order_size, depth_NO)
  Fee_Taker     = 0.0030 × min(price, 1-price) × size  // Por cada pierna
  Gas_Merge     = ~$0.006                      // Si merge on-chain necesario
  Gas_Redeem    = ~$0.004                      // Si redeem necesario

Margen_Neto = Margen_Bruto - Slippage_YES - Slippage_NO - Fee_YES - Fee_NO - Gas

REGLA: Solo ejecutar si Margen_Neto > Umbral_Mínimo
  - Fase 2 (paper): Umbral = $0.00 (registrar todo)
  - Fase 3 (mainnet): Umbral = $0.005 por share (0.5%)
```

#### Ejemplo numérico

```
Mercado: "¿Ganará el candidato X?"
  YES best ask: $0.47 (depth: 500 shares a este precio)
  NO best ask:  $0.51 (depth: 300 shares a este precio)

Margen_Bruto = 1.00 - (0.47 + 0.51) = $0.02/share

Para 100 shares:
  Slippage_YES: ~$0.001 (100 << 500 depth, impacto mínimo)
  Slippage_NO:  ~$0.002 (100 < 300 depth, impacto bajo)
  Fee_YES: 0.003 × min(0.47, 0.53) × 100 = $0.141
  Fee_NO:  0.003 × min(0.51, 0.49) × 100 = $0.147
  Gas: $0.006

  Margen_Neto = (100 × $0.02) - $0.001 - $0.002 - $0.141 - $0.147 - $0.006
             = $2.00 - $0.297
             = $1.703 por trade de 100 shares

  ROI = $1.703 / ($47 + $51) = 1.74%
```

### 5.2 Closing Arbitrage: Lógica de Convergencia

```
Condición: Mercado resuelto o resolución inminente

Si resultado = YES:
  - YES token converge a $1.00
  - NO token converge a $0.00
  Oportunidad: Comprar YES < $1.00 antes de que converja

Señales de entrada:
  1. Evento "market_resolved" en WebSocket → winning_asset_id conocido
  2. UMA Oracle ha resuelto pero el precio aún no refleja $1.00
  3. Precio del ganador < $0.98 post-resolución

Margen_Neto = (1.00 - Precio_Compra_Ganador) - Fee - Gas_Redeem

RIESGO: El mercado puede estar en disputa (UMA dispute period)
  → Verificar el estado del oráculo antes de entrar
```

### 5.3 Slippage Control

```
MODELO DE ESTIMACIÓN DE SLIPPAGE:

Para un tamaño de orden S en un libro con profundidad D:

1. CONSTRUIR CURVA DE IMPACTO:
   accumulated_cost = 0
   accumulated_size = 0
   for level in order_book_asks (sorted by price):
     available = min(level.size, S - accumulated_size)
     accumulated_cost += available × level.price
     accumulated_size += available
     if accumulated_size >= S: break

   VWAP = accumulated_cost / accumulated_size
   Slippage = VWAP - best_ask

2. REGLAS DE ABORTO:
   - Si depth total en book < 2× order_size → NO EJECUTAR
   - Si slippage estimado > 50% del margen bruto → NO EJECUTAR
   - Si spread (best_ask - best_bid) > 5% → REDUCIR tamaño o NO EJECUTAR
   - Si book tiene "gaps" (saltos >2 ticks entre niveles) → REDUCIR tamaño

3. SIZING DINÁMICO:
   max_size = min(
     target_size,
     depth_at_acceptable_slippage,    // Máximo que podemos comprar sin exceder slippage
     balance / price,                  // Máximo que podemos pagar
     daily_risk_limit - exposure       // Límite de riesgo diario
   )
```

### 5.4 Partial Fills: Estrategia de Gestión de Pierna Única

Este es el riesgo más crítico del Bonding Arbitrage: comprar una pierna pero no la otra.

```
ESCENARIO: Compramos YES a $0.47 pero NO sube a $0.54 antes de poder comprar
  → YES + NO = $0.47 + $0.54 = $1.01 → El arbitraje se invirtió

ESTRATEGIAS DE MITIGACIÓN:

1. EJECUCIÓN ATÓMICA (Preferida):
   - Usar POST /orders (batch) para enviar ambas piernas simultáneamente
   - Limitación: El batch no garantiza atomicidad (ambas se procesan independientemente)
   - Pero minimiza la ventana temporal entre piernas

2. PIERNA RÁPIDA PRIMERO:
   - Identificar cuál pierna tiene MENOS liquidez/mayor volatilidad
   - Ejecutar esa pierna primero (es la más probable de fallar/moverse)
   - Si éxito → Ejecutar segunda pierna inmediatamente
   - Razonamiento: Si la pierna difícil se llena, la fácil probablemente también

3. TIMEOUT Y SALIDA:
   - Tras ejecutar pierna 1, iniciar timer de 500ms para pierna 2
   - Si pierna 2 no ejecutada en 500ms → Evaluar:
     a. ¿El margen sigue siendo positivo? → Reintentar con precio ajustado
     b. ¿El margen es negativo pero <1%? → Mantener posición, esperar reversión
     c. ¿El margen es muy negativo (>2%)? → Salir de pierna 1 con market sell

4. HEDGING PASIVO:
   - Si tenemos solo YES comprado a $0.47:
     - Colocar limit sell de YES a $0.48 (tomar profit mínimo)
     - Colocar limit buy de NO con GTD corto (1 min) al precio objetivo
   - Si ninguno se llena en 2 min → Market sell YES y cortar pérdida

5. CIRCUIT BREAKER:
   - Si 3 partial fills consecutivos en el mismo mercado → Blacklist 5 min
   - Si pérdida acumulada por partial fills > 1% del capital → Pausar bot 15 min

MATRIZ DE DECISIÓN POST-FILL-PARCIAL:

| Precio NO actual | Margen residual | Acción |
|---|---|---|
| < precio_objetivo + 1% | Positivo | Ejecutar pierna 2 |
| objetivo + 1-3% | Ligeramente negativo | Esperar 30s, limit order |
| > objetivo + 3% | Muy negativo | Market sell pierna 1 |
```

### 5.5 Parámetros de Riesgo por Estrategia

Los límites se definen **individualmente por estrategia** porque sus perfiles de riesgo son muy distintos:

- **Bonding Arbitrage**: Riesgo de mercado nulo (ambas piernas cubiertas), pero riesgo de ejecución alto (partial fills). Se permite más capital por trade.
- **Closing Arbitrage**: Riesgo de mercado real (el resultado puede cambiar), pero ejecución simple (una sola orden). Se limita más el capital.

```
LÍMITES DE RIESGO — BONDING ARBITRAGE (YES + NO < $1.00):

  max_bet_per_trade:         $500      // Máximo invertido por arbitraje (suma de ambas piernas)
  max_position_per_market:   $1,000    // Exposición máxima acumulada por mercado
  max_total_exposure:        $3,000    // Exposición total abierta en todos los mercados
  max_daily_loss:            $50       // Stop-loss diario (solo por partial fills fallidos)
  min_margin_net:            $0.005    // Margen mínimo por share tras fees
  max_concurrent_arbs:       3         // Arbitrajes simultáneos abiertos
  min_book_depth_ratio:      2.0       // Depth mínima = 2× order size en AMBAS piernas
  max_partial_fill_loss:     $15       // Pérdida máxima aceptable si una pierna falla

LÍMITES DE RIESGO — CLOSING ARBITRAGE (Convergencia a $1.00):

  max_bet_per_trade:         $200      // Máximo invertido por trade (más conservador: hay riesgo de mercado)
  max_position_per_market:   $400      // Exposición máxima acumulada por mercado
  max_total_exposure:        $1,000    // Exposición total abierta en todos los mercados
  max_daily_loss:            $100      // Stop-loss diario (pérdidas reales posibles)
  min_margin_net:            $0.008    // Margen mínimo por share (más alto para compensar riesgo)
  max_concurrent_positions:  5         // Posiciones abiertas simultáneas
  min_implied_probability:   0.95      // Solo comprar tokens con implied prob ≥ 95%
  max_time_to_resolution:    24h       // Solo mercados que resuelven en <24h

LÍMITES GLOBALES (aplican a ambas estrategias combinadas):

  max_total_capital_deployed: $4,000   // Capital máximo total en juego
  stale_data_threshold:       5s       // Pausar AMBAS estrategias si datos >5s antiguos
  max_daily_loss_global:      $150     // Si pérdida combinada >$150, parar todo el día
  kill_switch:                true     // Endpoint manual para detener todo inmediatamente
```

Cada parámetro será configurable en tiempo de ejecución (archivo de configuración o variable de entorno) sin necesidad de reiniciar el bot.

---

## 6. Stack de Software

### 6.1 Evaluación de Lenguajes

| Criterio | Rust | Go | Python | Node.js |
|---|---|---|---|---|
| Latencia de ejecución | ★★★★★ (~μs) | ★★★★ (~μs-ms) | ★★ (~ms) | ★★★ (~ms) |
| Firma EIP-712 | ★★★★★ (alloy/ethers-rs) | ★★★★ (go-ethereum) | ★★★ (eth-account) | ★★★★ (ethers.js) |
| WebSocket handling | ★★★★ (tokio-tungstenite) | ★★★★★ (gorilla/ws, nhooyr) | ★★★ (websockets) | ★★★★ (ws) |
| Concurrencia | ★★★★★ (async/tokio) | ★★★★★ (goroutines) | ★★ (asyncio) | ★★★ (event loop) |
| SDK disponible | ★★★ (polymarket-sdk, joven) | ★★ (no oficial) | ★★★★★ (py-clob-client oficial) | ★★★★ (@polymarket/clob-client oficial) |
| Velocidad de desarrollo | ★★ | ★★★★ | ★★★★★ | ★★★★ |
| Debugging/profiling | ★★★ | ★★★★ | ★★★★★ | ★★★★ |

### 6.2 Recomendación: Arquitectura Híbrida

```
┌─────────────────────────────────────────────────────────────┐
│                   ARQUITECTURA DEL BOT                       │
│                                                             │
│  ┌─────────────────────────────────────────────────┐        │
│  │           CAPA DE ORQUESTACIÓN (Python)          │        │
│  │  - Configuración y parámetros de riesgo          │        │
│  │  - Dashboard / Logging / Alertas                 │        │
│  │  - Descubrimiento de mercados (Gamma API)        │        │
│  │  - Reconciliación de posiciones (Data API)       │        │
│  │  - Backtesting y simulación                      │        │
│  └──────────┬──────────────────────────┬────────────┘        │
│             │ IPC (Unix Socket/gRPC)   │                    │
│  ┌──────────▼──────────┐  ┌────────────▼────────────┐       │
│  │ MOTOR DE EJECUCIÓN  │  │ INGESTA DE DATOS        │       │
│  │ (Rust)              │  │ (Rust)                  │       │
│  │                     │  │                         │       │
│  │ - Firma EIP-712     │  │ - WebSocket client      │       │
│  │ - Cálculo de margen │  │ - Order book local      │       │
│  │ - Rate limiter      │  │ - Detección de          │       │
│  │ - HTTP client       │  │   oportunidades         │       │
│  │ - Order management  │  │ - Reconexión auto       │       │
│  └─────────────────────┘  └─────────────────────────┘       │
│                                                             │
│  ┌─────────────────────────────────────────────────┐        │
│  │           CAPA ON-CHAIN (Rust/ethers-rs)         │        │
│  │  - Split/Merge/Redeem via contratos              │        │
│  │  - Monitoreo de eventos (OrderFilled, etc.)      │        │
│  │  - Gas estimation y TX management                │        │
│  └─────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────┘
```

**Justificación de la arquitectura híbrida:**

- **Rust para el hot path**: La firma criptográfica, el mantenimiento del order book, y la ejecución de órdenes se benefician enormemente de la velocidad de Rust. La diferencia entre 0.5ms y 5ms en la firma puede ser decisiva.
- **Python para el cold path**: Configuración, backtesting, logging, y dashboard no son sensibles a latencia. Python tiene el mejor ecosistema para data analysis y el SDK oficial más maduro.
- **Alternativa simplificada (Fases 1-2)**: Usar Python puro con `py-clob-client`. Migrar componentes críticos a Rust solo en Fase 3 si la latencia es un cuello de botella demostrado.

### 6.3 Dependencias clave

**Rust (Motor de ejecución):**
```toml
[dependencies]
tokio = { version = "1", features = ["full"] }
tokio-tungstenite = "0.21"          # WebSocket client
alloy = "0.1"                       # EIP-712 signing, ABI
reqwest = { version = "0.12", features = ["json"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
rust_decimal = "1.34"               # Precisión decimal para precios
crossbeam = "0.8"                   # Lock-free data structures
tracing = "0.1"                     # Structured logging
```

**Python (Orquestación):**
```
py-clob-client>=0.15
py-order-utils>=0.3
web3>=6.0
pandas                              # Análisis y backtesting
prometheus-client                   # Métricas
structlog                           # Logging estructurado
```

---

## 7. Fases de Implementación

### Fase 1: Observador / Logging (Semanas 1-3)

**Objetivo:** Recopilar datos reales del mercado sin ejecutar trades.

```
ENTREGABLES:
├── 1.1 Conexión WebSocket al canal Market
│   ├── Suscripción a 5-10 mercados activos de alta liquidez
│   ├── Recepción y parseo de eventos: book, price_change, best_bid_ask
│   └── Heartbeat (PING/PONG cada 10s) con reconexión automática
│
├── 1.2 Order Book local
│   ├── Estructura de datos para mantener bids/asks por mercado
│   ├── Actualización incremental via price_change
│   └── Validación de integridad via hash
│
├── 1.3 Detector de oportunidades (sin ejecución)
│   ├── Calcular spread: 1.00 - (best_ask_YES + best_ask_NO)
│   ├── Estimar margen neto (fees + slippage estimado)
│   ├── Loguear cada oportunidad con timestamp, mercado, margen, depth
│   └── Generar estadísticas: frecuencia, duración, margen promedio
│
├── 1.4 Integración Gamma API
│   ├── Descubrimiento automático de mercados activos
│   ├── Filtrado: enableOrderBook=true, liquidez mínima
│   └── Polling cada 5 min para nuevos mercados
│
├── 1.5 Logging e infraestructura
│   ├── Logging estructurado (JSON) a archivo + stdout
│   ├── Métricas: latencia WS, oportunidades/hora, profundidad promedio
│   └── Alertas básicas (WS desconexión, error rates)
│
└── CRITERIO DE ÉXITO:
    ├── WS estable >24h sin desconexión manual
    ├── Order book local sincronizado (hash match >99%)
    ├── Dataset de >1000 oportunidades detectadas con métricas
    └── Análisis: ¿Las oportunidades son reales y ejecutables?
```

**Stack Fase 1:** Python puro con `py-clob-client` + `websockets` + `structlog`

### Fase 2: Paper Trading / Simulación (Semanas 4-6)

**Objetivo:** Simular la ejecución de trades y validar la estrategia sin capital real.

```
ENTREGABLES:
├── 2.1 Simulador de ejecución
│   ├── Simular POST /order con slippage realista
│   ├── Modelar latencia de ejecución (agregar delay aleatorio 2-10ms)
│   ├── Simular partial fills basados en depth real del book
│   └── Simular fees reales (taker 30bps)
│
├── 2.2 Motor de estrategia completo
│   ├── Bonding Arbitrage: detección + decisión + ejecución simulada
│   ├── Closing Arbitrage: detección de market_resolved + ejecución
│   ├── Cálculo de margen neto con todos los costes
│   └── Lógica de partial fill handling
│
├── 2.3 Gestión de riesgo
│   ├── Position tracking (simulado)
│   ├── P&L tracking en tiempo real
│   ├── Enforcement de límites de riesgo (max exposure, max loss, etc.)
│   └── Circuit breakers
│
├── 2.4 Autenticación CLOB
│   ├── Derivación de API credentials (L1 EIP-712)
│   ├── Headers L2 (HMAC-SHA256) para requests autenticados
│   ├── Lectura de posiciones reales via Data API
│   └── Canal User WebSocket para confirmar capacidad de auth
│
├── 2.5 Rate limiter interno
│   ├── Token bucket por categoría de endpoint
│   ├── Queue con prioridades
│   └── Logging de utilización de rate limits
│
├── 2.6 Dashboard
│   ├── Terminal UI o web simple mostrando:
│   │   - Mercados monitoreados y sus spreads
│   │   - Oportunidades activas
│   │   - P&L simulado acumulado
│   │   - Estado de conexiones (WS, REST)
│   │   - Rate limit utilization
│   └── Export de datos para análisis posterior
│
└── CRITERIO DE ÉXITO:
    ├── P&L simulado positivo durante >7 días consecutivos
    ├── Tasa de partial fills <10% de los trades
    ├── Rate limiter nunca excede 70% de capacidad
    ├── Latencia end-to-end (detección → decisión) <10ms (Python)
    └── Documentación de edge cases encontrados
```

**Stack Fase 2:** Python puro, mismas dependencias + simulador custom

### Fase 3: Mainnet (Semanas 7-10+)

**Objetivo:** Trading real con capital limitado, escalando gradualmente.

```
ENTREGABLES:
├── 3.1 Migración a producción
│   ├── Despliegue en AWS eu-west-1/eu-west-2 (c7gn.medium)
│   ├── Migrar componentes hot-path a Rust (si latencia Python insuficiente)
│   │   ├── WebSocket client + order book
│   │   ├── Firma EIP-712
│   │   └── HTTP client para POST /order
│   └── IPC entre Python (orquestación) y Rust (ejecución)
│
├── 3.2 Ejecución real
│   ├── POST /order y POST /orders contra CLOB real
│   ├── Monitoreo de trades via User WebSocket channel
│   ├── Tracking de estado: MATCHED → MINED → CONFIRMED
│   └── Manejo de RETRYING y FAILED
│
├── 3.3 Operaciones on-chain
│   ├── Approve USDC.e para CTF Exchange
│   ├── Split/Merge cuando sea más eficiente que trading
│   ├── Redeem tokens ganadores post-resolución
│   └── Dynamic gas bidding para TXs on-chain
│
├── 3.4 Escalado gradual
│   ├── Semana 7: $100 capital, 1 mercado, solo Bonding Arbitrage
│   ├── Semana 8: $250 capital, 3 mercados, ambas estrategias
│   ├── Semana 9: $500 capital, 5 mercados, parámetros ajustados
│   └── Semana 10+: Escalar según resultados
│
├── 3.5 Monitoreo y alertas
│   ├── Prometheus + Grafana para métricas
│   ├── Alertas: pérdida diaria, desconexión, error rate, balance bajo
│   ├── Logging completo para auditoría post-mortem
│   └── Kill switch manual (endpoint HTTP o señal)
│
├── 3.6 Seguridad
│   ├── Private key en AWS Secrets Manager o similar (nunca en disco)
│   ├── API credentials rotados periódicamente
│   ├── Instancia sin acceso SSH desde internet (bastion o SSM)
│   ├── Balance mínimo en wallet (solo lo necesario para operar)
│   └── Alertas si balance cae por debajo de umbral
│
└── CRITERIO DE ÉXITO:
    ├── Profit real positivo durante >14 días
    ├── Drawdown máximo <5% del capital
    ├── Uptime >99.5%
    ├── Zero incidents de seguridad
    └── Decisión informada sobre escalar o ajustar estrategia
```

---

## 8. Anexos

### 8.1 Contratos relevantes (Polygon Mainnet)

| Contrato | Dirección |
|---|---|
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Neg Risk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| Neg Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| Conditional Tokens (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC.e (Collateral) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| USDC (native) | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` |

### 8.2 URLs de referencia

| Servicio | URL |
|---|---|
| CLOB API | `https://clob.polymarket.com` |
| Gamma API | `https://gamma-api.polymarket.com` |
| Data API | `https://data-api.polymarket.com` |
| WebSocket Market | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| WebSocket User | `wss://ws-subscriptions-clob.polymarket.com/ws/user` |
| Status | `https://status.polymarket.com` |

### 8.3 Tick sizes

| Rango de precio | Tick size |
|---|---|
| $0.04 - $0.96 | `0.01` (la mayoría de mercados) |
| < $0.04 o > $0.96 | `0.001` o `0.0001` |

El tick size puede cambiar dinámicamente (evento `tick_size_change` en WebSocket).

### 8.4 Diagrama de secuencia: Bonding Arbitrage completo

```
Bot                    CLOB WS              CLOB REST            Polygon
 │                        │                     │                    │
 │◄──price_change(YES)────│                     │                    │
 │◄──price_change(NO)─────│                     │                    │
 │                        │                     │                    │
 │ [Detecta: YES+NO<1.00] │                     │                    │
 │ [Calcula margen neto]  │                     │                    │
 │ [Verifica depth]       │                     │                    │
 │ [Verifica riesgo]      │                     │                    │
 │                        │                     │                    │
 │ ──────POST /orders (YES buy + NO buy)──────> │                    │
 │ ◄─────Response {orderIDs}────────────────────│                    │
 │                        │                     │                    │
 │◄──trade(YES,MATCHED)───│                     │                    │
 │◄──trade(NO,MATCHED)────│                     │                    │
 │                        │                     │  Relayer submits   │
 │                        │                     │─────TX──────────>  │
 │                        │                     │                    │
 │◄──trade(YES,MINED)─────│                     │  Block included    │
 │◄──trade(NO,MINED)──────│                     │                    │
 │                        │                     │                    │
 │ [Posición: 100 YES + 100 NO]                 │                    │
 │ [Opción A: Mantener hasta resolución]        │                    │
 │ [Opción B: Merge on-chain → 100 USDC.e]     │                    │
 │                        │                     │                    │
 │ ─────────────────merge(100 YES + 100 NO)──────────────────────>  │
 │ ◄─────────────────100 USDC.e─────────────────────────────────────│
 │                        │                     │                    │
 │ [Profit = 100×$0.02 - fees - gas]            │                    │
```
