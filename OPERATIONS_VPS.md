# Operaciones VPS — Liquidity Strategy (Fases 1-5)

Guía de operaciones diarias, monitoreo, troubleshooting, y escalada de paper → dry_run → live.

---

## Monitoreo Diario

### 1. Panel Web
Acceso: `http://tu-vps.com:8080`

**Credenciales:**
- Username: `admin`
- Password: (variable de entorno `PANEL_PASSWORD`, default `admin`)

**Secciones a revisar cada mañana:**

```
/panel/liquidity

├─ SCANNER STATUS
│  ├─ Markets Found: debe mostrar 5-50 mercados
│  ├─ Total Daily Rewards: suma de rewards $/día
│  ├─ Scans: contador incrementando cada 300s
│  └─ Last Scan: debe ser < 60s atrás
│
├─ LIQUIDITY PROVIDER
│  ├─ Status: RUNNING o STOPPED
│  ├─ Active Markets: número de posiciones abiertas
│  ├─ Orders Placed: total histórico
│  ├─ Fills: número de órdenes ejecutadas
│  ├─ Rewards: $ ganados en rewards
│  └─ Adverse: $ perdido por adverse selection
│
├─ HEARTBEAT & SCORING
│  ├─ Heartbeat: ON/OFF (con contador si ON)
│  └─ Scoring Rate: % de órdenes que ganan rewards
│
├─ TODAY'S P&L
│  ├─ Net P&L: ganancia neta
│  ├─ Rewards: $ de rewards
│  ├─ Adverse: $ de pérdida
│  ├─ Fill Rate: % de órdenes llenas
│  └─ Daily ROI: rentabilidad del día
│
└─ 7-DAY SUMMARY (después de 2-3 días)
   ├─ Cumulative P&L: ganancia acumulada
   ├─ Total Rewards: suma de 7 días
   ├─ APY (Est.): rentabilidad anualizada
   └─ Adv. Ratio: ratio de pérdida por adverse
```

### 2. Verificar Logs

**Logs en tiempo real:**
```bash
docker compose logs -f bot 2>&1 | grep -i liquidity

# O filtrar por evento específico:
docker compose logs -f bot 2>&1 | grep -E "scanning_reward|quote_placed|fill|error"
```

**Logs guardados (en VPS):**
```bash
# Acceder a contenedor
docker exec -it polymarket_bot /bin/sh

# Ver logs JSON
tail -100 /app/logs/bot.jsonl | jq '.event' | sort | uniq -c

# Buscar errores
grep -i error /app/logs/bot.jsonl | tail -20
```

### 3. Métricas Clave a Vigilar

| Métrica | Valor Esperado | Acción si Anómalo |
|---------|---|---|
| **Scanner Errors** | 0-1 por día | Si > 3: revisar logs, puede ser API rate limit |
| **Active Markets** | 1-5 (papel mode) | Si 0: check `min_daily_rate`, quizás markets cambiaron |
| **Fill Rate** | 10-30% (papel) | Si 0%: check liquidity book depths, spreads |
| **Scoring Rate** | 80%+ (si heartbeat=true) | Si < 50%: órdenes no ganan rewards, revisar precios |
| **Adverse Ratio** | < 30% | Si > 50%: mercados con mucha slippage, considerar reducir spreads |
| **Daily ROI** | +0.5% a +2% (bullish) | Si negativo: revisa adverse losses vs rewards |
| **Last Scan** | < 60s atrás | Si > 120s: bot puede estar stuck, revisar logs |

---

## Hot-Reload: Cambiar Parámetros sin Reiniciar

### 1. Cambiar en Panel Web

Secciones modificables en `/panel/liquidity`:

**SCANNER CONFIGURATION:**
```toml
Scan Interval (sec)         → scan_interval (default 300)
Min Daily Rate ($)          → min_daily_rate (default 1.0)
Min Reward/Dollar           → min_reward_per_dollar (default 0.001)
Capital Per Market ($)      → capital_per_market (default 50.0)
Max Markets                 → max_markets (default 5)
```

**Cómo cambiar:**
1. Ir a `/panel/liquidity` → SCANNER CONFIGURATION
2. Cambiar valores
3. Click "Save Config"
4. Los cambios se aplican **inmediatamente** en memoria
5. Se persisten a `config/config.toml` automáticamente

### 2. Cambios Comunes

#### Problema: Scanner encuentra 0 mercados
**Solución:**
```
Min Daily Rate → reducir de 1.0 a 0.5
Min Reward/Dollar → reducir de 0.001 a 0.0005
Click "Scan Now" button
```

#### Problema: Demasiados mercados (> 10)
**Solución:**
```
Max Markets → reducir de 5 a 3
Capital Per Market → reducir de 50 a 25 (menos capital por mercado)
```

#### Problema: Muy pocas órdenes ejecutadas
**Solución:**
```
Capital Per Market → aumentar de 50 a 100 (más capital para competir)
Spread %: aumentar desde config.toml o vía API
```

---

## Troubleshooting

### A. Scanner no encuentra mercados

**Síntomas:**
- "Markets Found: 0"
- Scans incrementan pero no hay datos

**Diagnóstico:**
```bash
# 1. Check API connectivity
curl -s https://clob.polymarket.com/rewards/markets/multi | head -c 200

# 2. Check logs
docker compose logs bot 2>&1 | grep -i "scanning\|reward\|error" | tail -20

# 3. Check panel
# Si hay error log reciente, leerlo completo
```

**Soluciones:**
1. **API down**: Esperar 5-10 min, luego "Scan Now" manualmente
2. **No hay mercados con rewards**: Cambiar thresholds (ver arriba)
3. **Rate limit**: Aumentar `scan_interval` de 300s a 600s
4. **Network issue en VPS**: Verificar conectividad: `ping clob.polymarket.com`

---

### B. Provider no coloca órdenes

**Síntomas:**
- Scanner encuentra mercados (✓)
- "Active Markets: 0"
- "Orders Placed: 0"

**Diagnóstico:**
```bash
# Check si provider está activo
docker compose logs bot 2>&1 | grep "provider_started\|quote_loop"

# Check por error en quote placement
docker compose logs bot 2>&1 | grep -i "quote\|place_order\|error" | tail -20
```

**Soluciones:**
1. **Paper mode no inicializa ClobClient**: Normal y esperado
2. **Dry-run/live pero falta PRIVATE_KEY**: Check env vars
3. **Capital insuficiente**: Aumentar `capital_per_market` pero menor que balance
4. **Mercados desaparecen rápido**: Aumentar `capital_per_market` para competir

---

### C. Fills = 0 (ninguna orden llena)

**Síntomas:**
- Órdenes colocadas (Orders Placed > 0)
- Pero Fills = 0
- Rewards = 0

**Diagnóstico:**
```bash
# Check if prices are too narrow/wide
# Compare bid/ask in panel vs real market book

# Check logs para ver qué precios se cotizaban
docker compose logs bot 2>&1 | grep "quote_placed\|bid\|ask" | tail -10
```

**Soluciones:**
1. **Spreads muy estrechos**: Aumentar `spread_pct_of_max` en config (default 0.20)
2. **Spreads muy amplios**: Revisar `max_spread` en mercados
3. **Competencia alta**: Reducir `max_concurrent_bets` para menos posiciones simultáneas
4. **Midpoints no se actualizan**: Check WebSocket connection, logs debe mostrar price updates

---

### D. Adverse Ratio alto (> 50%)

**Síntomas:**
- Fills ejecutados ✓
- Pero Adverse Loss es alto
- Net P&L negativo o bajo

**Diagnóstico:**
```bash
# Check qué precio se llenó vs midpoint
# Esto indica slippage o adversarial fills
```

**Soluciones:**
1. **Reducir spreads**: Cotizar más cerca del midpoint
2. **Post-only no activo**: Verificar que ordenes son `post_only=true`
3. **Mercado con low liquidity**: Reducir `capital_per_market` en ese mercado
4. **Abandonment threshold**: Mercado se elimina si ratio > 0.7, es OK

---

### E. Heartbeat errors (si use_heartbeat=true)

**Síntomas:**
- "Heartbeat: OFF" o contador estancado
- Logs muestran "heartbeat_failed"

**Diagnóstico:**
```bash
docker compose logs bot 2>&1 | grep -i heartbeat | tail -20
```

**Soluciones:**
1. **API credentials inválidas**: Verify `POLYMARKET_API_KEY`, `BUILDER_API_KEY`
2. **Auto-derivation failed**: Verificar `PRIVATE_KEY` es válida (64 chars hex)
3. **Too slow heartbeat**: Si interval > 5s, aumentar a 5s máximo
4. **Disable heartbeat**: En panel o config, cambiar `use_heartbeat = false` (es opcional)

---

### F. Panel web no accesible

**Síntomas:**
- "Connection refused" en http://vps:8080

**Diagnóstico:**
```bash
# Check si bot está corriendo
docker compose ps

# Check status del container
docker compose logs bot 2>&1 | grep -i "web\|listening\|started" | tail -10

# Check port mapping
docker port polymarket_bot
```

**Soluciones:**
1. **Bot crashed**: Ver logs completos
2. **Port 8080 en uso**: Cambiar puerto en docker-compose.yml
3. **Firewall VPS**: Abrir puerto 8080 (si es VPS remota)

---

## Escalada: Paper → Dry-Run → Live

### Fase 1: Paper Mode (Actual State)

**Status:** ✅ Ejecutando
```toml
execution_mode = "paper"
```

**Duración:** 2-3 días
- Validar que scanner funciona
- Validar que provider coloca órdenes (simuladas)
- Validar que metrics son correctas
- **No se necesitan credenciales**

**Checklist:**
- [ ] Markets found: 5-20 (varía según mercado)
- [ ] Fill rate: > 5% (algo se llena)
- [ ] Daily ROI: > -10% (al menos no es disaster)
- [ ] Metrics se actualizan correctamente
- [ ] 7-day summary empieza a acumular datos

---

### Fase 2: Dry-Run Mode (Validación)

**Requisito:** Credenciales válidas (wallet con fondos en Polymarket no necesarios)

**Setup:**
```bash
# 1. Set env vars (en .env o docker-compose.yml)
export PRIVATE_KEY=0x... (válida, pero sin fondos OK)
export WALLET_TYPE=2  # o 1 o 0 según tu wallet
export POLYMARKET_PROXY_ADDRESS=0x...  # (opcional si Magic Link)
export POLYMARKET_API_KEY=...
export BUILDER_API_KEY=...
export BUILDER_SECRET=...
export BUILDER_PASSPHRASE=...

# 2. Cambiar config
# En panel o en config.toml
execution_mode = "dry_run"

# 3. Reiniciar bot
docker compose restart bot
```

**Monitoreo:**
```bash
# Logs deben mostrar:
docker compose logs bot 2>&1 | grep -i "dry_run\|order_validation\|clobclient"

# Expected:
# "dry_run mode: order created and validated but NOT sent"
# "clobclient_initialized"
```

**Duración:** 1 día
- Validar que ClobClient se inicializa
- Validar que órdenes se crean y validan correctamente
- Validar que NO se envían al blockchain (monitorea Polymarket UI, debe estar vacío)

**Checklist:**
- [ ] ClobClient initialized (logs lo muestran)
- [ ] Orders created (logs muestran "order_id")
- [ ] Orders NOT sent (Polymarket UI muestra 0 órdenes del bot)
- [ ] Precios se calculan correctamente

---

### Fase 3: Live Mode (Real Trading)

⚠️ **WARNING**: Este paso ejecuta órdenes REALES y usa USDC real.

**Requisito:**
- Wallet con fondos en Polymarket (USDC disponible)
- Dry-run validado exitosamente

**Setup - Escalada GRADUAL:**

```bash
# Paso 1: Cambiar a live pero con CAPITAL MÍNIMO
# Editar config.toml:
execution_mode = "live"
capital_per_market = 10.0  # ← MUY BAJO para empezar

# Paso 2: Reiniciar bot
docker compose restart bot

# Paso 3: Monitorear CONSTANTEMENTE los primeros 10 minutos
docker compose logs -f bot 2>&1 | grep -E "order_placed|fill|error"

# Paso 4: Verificar en Polymarket UI que órdenes aparecen
# https://polymarket.com → tu wallet → órdenes abiertas
```

**Monitoreo Intensivo (Primeras 2-3 horas):**
```bash
# Terminal 1: Logs en vivo
docker compose logs -f bot 2>&1 | grep -i "liquidity\|order\|fill"

# Terminal 2: Check precios en panel
# http://vps:8080 → /panel/liquidity → ACTIVE QUOTES

# Terminal 3: Check balance (opcional)
# Polymarket UI → Profile → Balance should decrease as orders placed
```

**Red Flags (Detener inmediatamente):**
```
❌ Too many errors (> 5/min)
❌ Adverse ratio jumping to > 70%
❌ Orders not filling (creadas pero stuck)
❌ API errors increasing
❌ Balance draining (más de 20% en 1 hora)

→ Acción: Cambiar execution_mode = "paper" y reiniciar
```

**Escalada Gradual (días 1-5):**
```
Día 1: capital_per_market = 10
Día 2: capital_per_market = 25 (si OK)
Día 3: capital_per_market = 50 (si OK)
Día 4-5: capital_per_market = 100+ (si OK)
```

**Monitoreo Diario Post-Launch:**
- Cada mañana: revisar P&L del día anterior
- Cada medio día: verificar que orders se están ejecutando
- Cada noche: revisar logs por errores

---

## Cambio de Parámetros en Vivo

### Parámetros que se pueden cambiar sin reiniciar

✅ **Vía Panel Web** (hot-reload):
```
scan_interval
min_daily_rate
min_reward_per_dollar
capital_per_market
max_markets
```

✅ **Vía API** (POST endpoints):
```
/panel/liquidity/scan          → Force scan now
/panel/liquidity/cancel-all    → Emergency cancel todas las órdenes
```

### Parámetros que REQUIEREN reinicio

❌ **Requieren restart:**
```
execution_mode (paper ↔ live)
use_heartbeat (ON ↔ OFF)
credentials (PRIVATE_KEY, API_KEY, etc.)
strategy_type
```

**Cómo reiniciar:**
```bash
docker compose restart bot

# O más limpio:
docker compose down && docker compose up -d
```

**Verificar que reinició correctamente:**
```bash
docker compose logs bot | grep -i "started\|listening" | tail -5

# Debe mostrar:
# "bot_started"
# "web_server_listening_on_port_8080"
```

---

## Logs y Debugging

### Ubicación de Logs

**En VPS (dentro del container):**
```
/app/logs/bot.jsonl  (líneas JSON, una por evento)
```

**Ver últimas N líneas:**
```bash
docker compose exec bot tail -50 /app/logs/bot.jsonl | jq '.'
```

### Eventos Importantes a Buscar

```bash
# Scanner inicia
docker compose logs bot | grep "scanning_reward_markets"

# Mercado encontrado
docker compose logs bot | grep "reward_market_found"

# Orden colocada
docker compose logs bot | grep "quote_placed"

# Orden llena
docker compose logs bot | grep "fill_recorded"

# Rewards ganados
docker compose logs bot | grep "rewards_earned"

# Error
docker compose logs bot | grep "ERROR\|error_\|failed"

# Heartbeat
docker compose logs bot | grep "heartbeat"
```

### Formato JSON de Logs

Cada línea es un JSON con estructura:
```json
{
  "ts": 1713268800.123,
  "event": "scanning_reward_markets",
  "market_count": 15,
  "total_rewards": 450.50,
  "account": "liquidity_rewards",
  "level": "info"
}
```

**Extraer métrica específica:**
```bash
# Contar cuántos scans hubo hoy
docker compose exec bot bash -c 'grep "scanning_reward" /app/logs/bot.jsonl | wc -l'

# Ver rewards totales en último scan
docker compose exec bot bash -c 'grep "scanning_reward" /app/logs/bot.jsonl | tail -1 | jq .total_rewards'

# Ver todos los fills de hoy
docker compose exec bot bash -c 'grep "fill_recorded" /app/logs/bot.jsonl | jq "{price:.fill_price, size:.size, adverse:.adverse_amount}"'
```

---

## Mantenimiento y Backups

### Backups Automáticos

Logs se guardan en volumen Docker:
```bash
docker volumes ls | grep bot-logs
```

**Backup manual:**
```bash
# Copiar logs a máquina local
docker cp polymarket_bot:/app/logs/bot.jsonl ./backup_$(date +%Y%m%d).jsonl

# Copiar DB (si es necesario)
docker cp polymarket_bot:/app/data/panel.db ./backup_$(date +%Y%m%d).db
```

### Limpieza de Logs Antiguos

Si los logs crecen mucho (>1GB):
```bash
# Ver tamaño actual
du -sh data/logs/

# Archivar logs viejos
docker exec bot bash -c 'find /app/logs -name "*.jsonl" -mtime +30 -exec gzip {} \;'

# O simplemente: logs se rotan automáticamente a max_file_size_mb (100MB)
```

### Recuperación de Crash

Si el bot se cuelga:
```bash
# 1. Check status
docker compose ps

# 2. Ver logs del crash
docker compose logs bot 2>&1 | tail -100

# 3. Restart
docker compose restart bot

# 4. Verify
docker compose logs bot | grep "started"
```

**Si el crash es recurrente:**
```bash
# 1. Cambiar a paper mode
# editar config.toml: execution_mode = "paper"

# 2. Reduce risk parameters
max_bet_per_trade = 10.0  (reduce from 50)
capital_per_market = 5.0  (reduce from 50)

# 3. Restart
docker compose down && docker compose up -d
```

---

## Alertas Recomendadas

Si tienes acceso a monitoring (opcional), alertar sobre:

| Alerta | Condición | Acción |
|--------|-----------|--------|
| **Bot Down** | Container no está running | Reiniciar |
| **Scanner Stalled** | Último scan > 5 min atrás | Reiniciar bot |
| **High Error Rate** | > 10 errors/min en logs | Check API, reiniciar |
| **Low Fill Rate** | Fill rate < 1% en 1 hora | Aumentar capital/spread |
| **High Adverse** | Adverse ratio > 60% | Revisar spreads |
| **API Error 429** | Rate limit en CLOB API | Aumentar scan_interval |
| **No Heartbeat** | Heartbeat inactive > 60s | Revisar credenciales |

**Implementar alertas simples (bash):**
```bash
#!/bin/bash
# check_health.sh

LAST_SCAN=$(docker compose logs bot 2>&1 | grep -oP '"ts":\K[0-9.]+' | tail -1)
NOW=$(date +%s)
DIFF=$((NOW - LAST_SCAN))

if [ $DIFF -gt 600 ]; then
  echo "⚠️  ALERT: Last scan was $DIFF seconds ago"
  # Aquí enviar email/Slack si es necesario
fi
```

---

## Preguntas Frecuentes (FAQ)

### P: ¿Cuánto capital necesito para empezar en live?
**A:** Mínimo $50-100 USDC disponible en Polymarket. Comienza con `capital_per_market = 10` y escala gradualmente.

### P: ¿Cuánto tiempo tarda el bot en recuperarse si se cuelga?
**A:** Máximo 2-3 minutos (reinicio del container + carga del estado).

### P: ¿Pierdo dinero si cambio de execution_mode?
**A:** No, los stats se resetean al cambiar modo, pero el balance real (en Polymarket) no se toca. En paper mode el balance es simulado.

### P: ¿Puedo cambiar spreads en vivo?
**A:** Indirectamente: usa `capital_per_market` para ajustar competencia. Para spreads finos, necesitas cambiar código (no hot-reload).

### P: ¿Qué pasa si Polymarket baja?
**A:** El bot lo detecta (error en API) y hace retry automático. Si está más de 10+ min abajo, check logs y considera reiniciar.

### P: ¿Puedo correr múltiples instancias?
**A:** No recomendado (conflictos de órdenes). Usa una sola instancia en VPS.

### P: ¿Cómo reporto un bug?
**A:** 1. Captura logs (últimas 100 líneas con error). 2. Anota qué estabas haciendo. 3. Reporta en GitHub issues.

---

## Checklist Operaciones Diarias

```
⬜ 9am:  Revisar panel /panel/liquidity
         - Markets found: ✓
         - Provider status: ✓
         - Rewards acumuladas: ✓

⬜ 12pm: Check mid-day metrics
         - Fill rate: ✓
         - Adverse ratio: ✓
         - No errors en logs: ✓

⬜ 6pm:  End of day review
         - Daily P&L: ✓
         - 7-day summary trending up: ✓
         - Logs backup (si vives en prod): ✓

⬜ Semanal:
         - Compare ROI vs semana anterior: ✓
         - Ajustar spreads/capital si es necesario: ✓
         - Review competitive landscape (mercados cambian): ✓
```

---

## Soporte y Debugging Avanzado

### Debugging VPS Remota

```bash
# SSH al VPS
ssh user@vps.com

# Ir al directorio del bot
cd /path/to/polymarket

# Ver logs en vivo
docker compose logs -f

# O en background
nohup docker compose logs -f > monitoring.log 2>&1 &

# Después extraer info:
grep "liquidity" monitoring.log | jq '.event' | sort | uniq -c
```

### Monitoreo Remoto (opcional Prometheus/Grafana)

Si quieres métricas más avanzadas, los logs JSON son parseables.

Ejemplo: recolectar metrics cada 5 min
```bash
while true; do
  SUMMARY=$(curl -s http://vps:8080/api/rewards/metrics | jq '.summary')
  echo "$(date): $SUMMARY"
  sleep 300
done
```

---

## Conclusión

El bot está diseñado para:
✅ Operar con mínima intervención
✅ Permitir cambios de parámetros en vivo
✅ Recuperarse automáticamente de fallos
✅ Loguear todo para debugging

**Monitoreo recomendado:**
- 5 min: revisar panel web
- 1 hora: revisar logs por errores
- 1 día: revisar P&L y ajustar si es necesario

¡A operar!
