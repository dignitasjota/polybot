# CPU Optimization - Throttles de Detector (2026-04-01)

## Problema Original
- **CPU**: 106% constantemente
- **Causa raíz**: `detector.check()` era invocado miles de veces por segundo para los mismos mercados sin throttling
- **Síntoma**: Logs duplicados todas en el mismo millisegundo (ej: 12:05:38.265962Z a 12:05:38.316687Z)

## Solución Implementada
Se agregó throttling en 3 niveles para reducir llamadas innecesarias sin perder trades:

### 1. Detector Throttle Per-Token (detector.py)
**Archivo**: `src/detector.py`

**Cambios**:
```python
# Línea 91-92 (en __init__)
self._last_check_time: dict[str, float] = {}  # token_id -> timestamp
self._check_throttle_ms = 300  # Skip re-check for same token within 300ms

# Línea 172-178 (en check() method)
if token_id and event_type != "resolution_check":
    now = time.time()
    last_check = self._last_check_time.get(token_id, 0)
    if now - last_check < self._check_throttle_ms / 1000.0:
        return  # Skip this check, was called too recently
    self._last_check_time[token_id] = now
```

**Efecto**: Cada mercado se chequea máximo ~3.3 veces por segundo (1000ms / 300ms)

**Ajuste si es necesario**:
- Aumentar a `500ms`: ~2 veces/segundo (CPU más baja, riesgo de perder oportunidades rápidas)
- Disminuir a `200ms`: ~5 veces/segundo (más detección, CPU más alta)
- Disminuir a `100ms`: ~10 veces/segundo (máximo nivel anterior, CPU 40%)

---

### 2. WebSocket Global Throttle (websocket_client.py)
**Archivo**: `src/websocket_client.py`

**Cambios**:
```python
# Línea 200-203 (en _message_handler)
now = time.time()
last = self._last_check_time.get(asset_id, 0)
if now - last < 2.0:  # Cambio de 1.0s a 2.0s
    continue
self._last_check_time[asset_id] = now
```

**Efecto**: Events como "book", "best_bid_ask", "last_trade_price" se throttleam a máximo 1 vez cada 2 segundos por token

**Nota**: Los eventos `price_change` no están sujetos a este throttle (se procesan siempre), pero ahora pasan token_id específico al detector para que el detector throttle los procese.

**Ajuste si es necesario**:
- Aumentar a `3.0s`: Reducción de CPU pero menos reactivo a cambios de libro de órdenes
- Disminuir a `1.0s`: Vuelve a la configuración anterior (CPU 40%)

---

### 3. Price Checker Poll Interval (price_checker.py)
**Archivo**: `src/price_checker.py`

**Cambios**:
```python
# Línea 194 (en __init__)
def __init__(self, min_buffer_pct: float = 0.10, poll_interval: float = 3.0,  # Cambio de 2.0s a 3.0s
```

**Efecto**: Las consultas a Binance para confirmar dirección de Up/Down ocurren máximo cada 3 segundos

**Nota**: En un mercado de 5 minutos, sigue siendo tiempo suficiente. El open price se captura una sola vez (5s después de start_utc).

**Ajuste si es necesario**:
- Aumentar a `5.0s`: Reducción de CPU pero riesgo de perder direction confirmation rápida
- Disminuir a `2.0s`: Vuelve a la configuración anterior
- Disminuir a `1.0s`: Máxima reactividad (CPU más alta)

---

## Configuración de Reset
Los throttles se **resetean correctamente** en cambios de modo (paper ↔ live):

```python
# detector.py línea 142
self._last_check_time.clear()  # Clear check throttle for fresh checks in new mode
```

Esto garantiza que al cambiar a live mode, el detector comience con estado limpio sin arrastrar throttle del período paper.

---

## CPU Esperada
| Configuración | Detector | WS | Price Checker | CPU Esperada |
|---|---|---|---|---|
| Original (sin throttles) | - | 1s | 2s | 106% |
| Anterior | 100ms | 1s | 2s | 40% |
| **Actual (Moderada)** | **300ms** | **2s** | **3s** | **15-20%** |

---

## Cómo Revertir Si Pierdes Trades
Si observas que **no estás detectando oportunidades** que deberías detectar:

1. **Si es problema de "price_change no se detecta rápido"**:
   - Bajar `detector._check_throttle_ms` a `200ms` o `100ms`

2. **Si es problema de "cambios en el libro de órdenes no se ven"**:
   - Bajar websocket throttle de `2.0s` a `1.0s` en línea 202 de `websocket_client.py`

3. **Si es problema de "dirección Up/Down no se confirma a tiempo"**:
   - Bajar `poll_interval` de `3.0s` a `2.0s` o `1.0s` en `price_checker.py` línea 194

4. **Para reverticar completamente a estado anterior (CPU 40%, máxima reactividad)**:
   ```python
   # detector.py línea 92
   self._check_throttle_ms = 100

   # websocket_client.py línea 201
   if now - last < 1.0:

   # price_checker.py línea 194
   poll_interval: float = 2.0
   ```

---

## Monitoreo
- Revisar logs con `docker logs -f` para asegurar que se siguen detectando oportunidades
- Monitorear CPU con `docker stats polymarket` (debe estar en 15-20%)
- Si CPU sube de 30% → ajustar throttles
- Si oportunidades desaparecen → disminuir throttles

---

## Cambios Secundarios (Price Change Event Callback)
**Archivo**: `src/websocket_client.py`

Se modificó `_handle_price_change()` para retornar lista de tokens actualizados, permitiendo que el callback sea invocado con `token_id` específico en lugar de vacío. Esto permite que el throttle del detector por token funcione correctamente.

```python
# Antes: await self._on_opportunity_callback("", event_type)  # empty token_id!
# Ahora: for token_id in updated_tokens: await self._on_opportunity_callback(token_id, event_type)
```

Este cambio es transparente y **no debe revertirse sin revisar la lógica de detección**.
