# Análisis: Cómo el Backtesting Repo Puede Ayudar al Directional Bot

**Fecha:** 2026-04-12
**Conclusión:** El repo no resuelve directamente los problemas arquitectónicos actuales (dashboard, order blocking), pero proporciona herramientas para analizar adverse selection losses.

---

## 1. Problemas Actuales del Directional Bot

### 1.1 Dashboard Visibility ✓ RESUELTO

**Problema:** Bets no aparecían en el dashboard a pesar de estar en logs
**Root Cause:** Cleanup operation borraba oportunidades de `_opportunities_log` cuando superaba 500 entries
**Solución implementada:** Crear lista permanente `_dashboard_bets` que nunca se trimea
**Status:** Corrección ya aplicada en `src/detector.py`

### 1.2 Auto-Redeem ✓ FUNCIONANDO

**Problema:** Reportado como funcional
**Status:** No requiere cambios

### 1.3 Order Blocking / Adverse Selection ⚠️ INVESTIGAR

**Problema:** ¿Por qué se ejecutan las órdenes pero no se ven en dashboard? ¿Son pérdidas por adverse selection?

**Hipótesis:** Las órdenes se colocan a un precio, pero se ejecutan a otro peor:
- Colocamos a BID $0.45
- Se ejecuta a $0.42 (por impacto de orden en libro)
- Si el mercado resuelve YES a $1.00, ganamos $0.58 pero perdemos $0.03 por slippage

**Por qué el backtesting repo ayuda:**
El repo calcula exactamente esto: diferencia entre `order_price` (lo que pedimos) vs `filled_price` (lo que realmente ejecutó).

---

## 2. Estrategias del Repo vs Directional Bot

El backtesting repo incluye 4 estrategias de ejemplo. Comparación con nuestro bot:

| Estrategia | Repo | Nuestro Bot | Aplicabilidad |
|-----------|------|-----------|---------------|
| **EMA Crossover** | Cruces de medias móviles | N/A | Baja — requiere series de tiempo, nuestro bot no predice |
| **VWAP** | Volume-weighted price, detecta desviaciones | Indirecta en PriceChecker | Media — podría mejorar detección de mercados en movimiento |
| **RSI** | Momentum oscillator para overbought/oversold | N/A | Baja — requiere análisis técnico, nuestro bot apuesta a closing arbitrage |
| **Breakout** | Romper resistencia/soporte histórica | Indirecta en Up/Down | Media — detección de cambios de tendencia en Binance |

**Conclusión:** Las estrategias del repo son predictivas (apuestan a dirección). Nuestro bot directional ya apuesta a dirección. El repo **no ofrece mejora estratégica directa**.

---

## 3. Herramientas del Repo Aplicables al Directional Bot

### 3.1 PolymarketDataLoader

**Qué hace:**
```python
# Desde repo
trades = await data_loader.get_trades(
    market_id=market_id,
    limit=1000,
    start_timestamp=start
)
```

Paginación contra `GET /data-api.polymarket.com/trades` para obtener histórico.

**Aplicación a nuestro bot:**

1. **Análisis offline de adverse selection:**
   ```python
   # Script análisis (fuera del bot)
   def analyze_market_fills(market_id):
       """
       Obtener últimos 100 trades del mercado.
       Calcular: ¿cuál es la slippage típica?
       """
       trades = data_loader.get_trades(market_id, limit=100)

       # Agrupar por timestamp en ventanas de 10 segundos
       # Si hay 3+ órdenes en misma ventana → impact
       impact_per_trade = [...]
       avg_impact = mean(impact_per_trade)

       return {
           "market_id": market_id,
           "avg_slippage_pct": avg_impact,
           "recommendation": "avoid" if avg_impact > 0.05 else "ok"
       }
   ```

2. **Durante ejecución en tiempo real:**
   - Usar `/trades` después de que se ejecute nuestra orden
   - Verificar a qué precio realmente se ejecutó
   - Loguear diferencia entre `suggested_price` vs `actual_fill`
   - Alimentar a `adverse_selection_tracker` para decisiones futuras

### 3.2 Fee Formula Validation

**Repo usa:**
```python
fee = qty * 0.003 * p * (1 - p)
```

**Nuestro bot usa:**
```python
min_margin_net = (1 - price) - fees - gas_cost
# donde fees es exactamente lo mismo
```

**Validación:** ✓ Ambos usan la misma fórmula. No hay discrepancia.

### 3.3 Settlement P&L Calculation

**Del repo:**
```python
def calculate_pnl(
    side: str,           # "YES" o "NO"
    entry_price: float,
    exit_price: float,
    quantity: float
) -> float:
    if side == "YES":
        pnl = (1.0 - entry_price) * quantity - (entry_price - exit_price) * quantity
    else:
        pnl = exit_price * quantity - (exit_price - entry_price) * quantity
    return pnl
```

**Aplicación:** Usar este pattern exacto en `Executor.calculate_settled_pnl()` para asegurar que el cálculo de P&L sea idéntico al repo.

---

## 4. Por Qué el Repo NO Resuelve los Problemas Directos

### 4.1 Dashboard Visibility

**Repo ofrece:** Histórico de trades desde Data API
**Problema nuestro:** Bets no se muestran INMEDIATAMENTE en dashboard
**Por qué no ayuda:** El repo usa datos históricos (después del evento). Nuestro problema es de arquitectura real-time (caché de `_opportunities_log` vs `_dashboard_bets`)

**Solución:** Ya implementada — `_dashboard_bets` permanente

### 4.2 Order Blocking

**Repo ofrece:** Análisis post-trade de slippage
**Problema nuestro:** Órdenes se colocan pero se ejecutan a precio peor
**Por qué no ayuda completamente:** El repo no oferta, solo analiza. Pero sí puede DIAGNOSTICAR.

**Aplicación real:**
```python
# En PriceChecker o Executor
async def diagnose_fill_quality(order_id: str):
    """
    Después de que se ejecute una orden:
    1. Leer su precio de ejecución
    2. Buscar trades del mismo mercado en ±5 segundos
    3. Calcular: ¿fue nuestra orden victim de impact?
    """
    our_order = await clob.get_order(order_id)
    trades = await data_loader.get_trades(
        market_id=our_order.market_id,
        start_timestamp=our_order.filled_time - 5,
        end_timestamp=our_order.filled_time + 5
    )

    # Analizar orden book snapshot
    book_impact = calculate_impact(trades)
    return {
        "order_price": our_order.price,
        "actual_fill": our_order.average_fill_price,
        "impact_loss": order_price - actual_fill,
        "book_condition": "crowded" if book_impact > 2% else "normal"
    }
```

---

## 5. Recomendaciones de Integración

### 5.1 Corto Plazo (1-2 días)

**Objetivo:** Diagnosticar si adverse selection es el problema

```python
# Script: analyze_execution_quality.py
async def main():
    market_ids = [...]  # Últimos 20 mercados operados

    for market_id in market_ids:
        # 1. Obtener nuestras órdenes ejecutadas
        our_orders = executor._trades.filter_by(market_id)

        # 2. Para cada orden, obtener fills reales del repo
        for order in our_orders:
            trades = await data_loader.get_trades(
                market_id,
                limit=1000
            )

            # 3. Calcular slippage
            our_price = order.price
            actual_fill = find_our_trade_in(trades, order.id)
            slippage = our_price - actual_fill.price

            print(f"{market_id}: slippage ${slippage:.4f}")

    # Report: ¿el slippage promedio > 1%?
    # Si sí, adversa selection es problema real
```

**Output esperado:**
```
Market_A: slippage $0.0120 (2.6%)
Market_B: slippage $0.0045 (1.0%)
Market_C: slippage $0.0201 (4.2%)  ← Problema identificado

Conclusión: Slippage promedio 2.6%, pérdida de $X/mes
Recomendación: Usar order block hiding, reducir size por orden
```

### 5.2 Mediano Plazo (1-2 semanas)

**Objetivo:** Implementar monitoreo continuo de adverse selection

Añadir a `Executor`:
1. Después de cada `post_order()` exitoso, loguear `{order_id, price, timestamp}`
2. En background task cada 30s: consultar Data API por trades del mercado
3. Encontrar nuestro trade y comparar con precio esperado
4. Alimentar métrica `executor.adverse_selection_loss` al dashboard

```python
# En executor.py
async def _monitor_execution_quality_loop(self):
    """Background task que monitorea slippage real"""
    while True:
        for trade in self._recent_trades[-50:]:  # Últimos 50
            if not trade.quality_analyzed:
                # Consultar Data API
                trades = await data_loader.get_trades(
                    trade.market_id,
                    start=trade.filled_time - 30,
                    end=trade.filled_time + 30
                )
                # Buscar nuestro trade
                our_fill = find_matching_trade(trades, trade)
                if our_fill:
                    trade.actual_fill_price = our_fill.price
                    trade.slippage = abs(trade.price - our_fill.price)
                    trade.quality_analyzed = True

        await asyncio.sleep(30)
```

### 5.3 Largo Plazo (Fase Liquidity)

Reutilizar Data API para la estrategia de market making:

```python
# En liquidity_strategy.py (futuro)
async def select_markets_to_quote():
    """Usar Data API para elegir mercados rentables"""

    # 1. Obtener rewards de /rewards/markets/multi
    rewards = await gamma.get_rewards()

    # 2. Para cada mercado con rewards > $100/día:
    for market in rewards:
        # 3. Análisis histórico
        trades = await data_loader.get_trades(market.id, limit=1000)

        # 4. Calcular volatilidad, spread actual, volumen
        vol = calculate_volatility(trades)
        spread = calculate_spread(trades)
        volume = sum(t.quantity for t in trades)

        # 5. Score = reward / (volatility + adverse_loss_expected)
        adverse_expected = vol * 0.1  # Heurística
        score = market.daily_reward / (adverse_expected + spread * volume)

        if score > threshold:
            yield market
```

---

## 6. Conclusión

| Aspecto | Backtesting Repo | Valor para Nuestro Bot |
|---------|------------------|----------------------|
| **Estrategias de trading** | EMA, VWAP, RSI, Breakout | ❌ Bajo — no resuelve directional |
| **Fee formulas** | ✓ Idéntica a la nuestra | ✓ Validación |
| **Settlement P&L** | ✓ Patrón reutilizable | ✓ Medio — asegurar consistencia |
| **Data API integration** | ✓ PolymarketDataLoader | ✓ Alto — diagnosticar adverse selection |
| **Order book analysis** | Mínimo | ⚠️ Medio — mejorable |
| **Liquidity rewards** | No implementa | ✓ Alto — para futura fase liquidity |

**Recomendación final:**
1. ✓ Ya implementado: Dashboard fix (`_dashboard_bets`)
2. ⚠️ A investigar: Usar Data API para diagnosticar si adverse selection es problema
3. ✓ A futuro: Reutilizar patterns del repo para liquidity strategy

El repo NO es la solución inmediata a nuestros problemas (que ya resolvimos), pero SÍ es valioso para:
- **Validar** nuestras fórmulas de fees y P&L
- **Diagnosticar** adverse selection losses en ejecución real
- **Implementar** la próxima estrategia (liquidity provider)

---

**Status:** Análisis completo. Hallazgos listos para integración.
