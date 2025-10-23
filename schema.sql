-- schema.sql - Estructura de base de datos para DRY_RUN y ML

-- Tabla principal: trades simulados
CREATE TABLE IF NOT EXISTS dryrun_trades (
  id SERIAL PRIMARY KEY,
  
  -- Tipo de operación
  type VARCHAR(10) NOT NULL, -- 'buy' o 'sell'
  
  -- Identificación del token
  mint VARCHAR(44) NOT NULL,
  symbol VARCHAR(32),
  name VARCHAR(128),
  
  -- Precios
  entry_price NUMERIC(20,10),
  exit_price NUMERIC(20,10),
  initial_price NUMERIC(20,10),
  max_price NUMERIC(20,10),
  
  -- Cantidades
  amount_sol NUMERIC(20,10),
  tokens NUMERIC(30,10),
  
  -- Ejecución
  slippage_bps INT,
  slip_factor NUMERIC(10,5),
  partial_fill NUMERIC(10,5),
  failed BOOLEAN DEFAULT FALSE,
  
  -- Resultado
  pnl_percent NUMERIC(10,4),
  hold_time_min NUMERIC(10,2),
  
  -- Contexto del token al momento del trade
  liq_usd_at_entry NUMERIC(20,2),
  market_cap_at_entry NUMERIC(20,2),
  slope_at_entry NUMERIC(20,10),
  drawdown_at_entry NUMERIC(10,4),
  gain_percent_at_entry NUMERIC(10,4),
  elapsed_min_at_entry NUMERIC(10,2),
  
  -- Reglas que dispararon la alerta
  rules_matched TEXT, -- JSON array: ["fast_2x", "momentum_150"]
  
  -- Flags
  passed_filters BOOLEAN DEFAULT TRUE,
  
  -- Timestamps
  created_at TIMESTAMP DEFAULT NOW(),
  
  -- Índices para búsquedas rápidas
  INDEX idx_mint (mint),
  INDEX idx_created_at (created_at),
  INDEX idx_pnl (pnl_percent),
  INDEX idx_type (type)
);

-- Tabla de tokens monitoreados (histórico)
CREATE TABLE IF NOT EXISTS monitored_tokens (
  id SERIAL PRIMARY KEY,
  mint VARCHAR(44) NOT NULL UNIQUE,
  symbol VARCHAR(32),
  name VARCHAR(128),
  
  -- Bonding curve
  bonding_curve VARCHAR(44),
  
  -- Precios
  initial_price NUMERIC(20,10),
  max_price NUMERIC(20,10),
  final_price NUMERIC(20,10),
  
  -- Métricas
  max_gain_percent NUMERIC(10,4),
  final_gain_percent NUMERIC(10,4),
  max_drawdown_percent NUMERIC(10,4),
  
  -- Liquidez
  initial_market_cap NUMERIC(20,2),
  initial_liquidity_usd NUMERIC(20,2),
  
  -- Timing
  started_at TIMESTAMP,
  ended_at TIMESTAMP,
  monitor_duration_min NUMERIC(10,2),
  
  -- Resultado
  exit_reason VARCHAR(32), -- 'alert_sent', 'timeout', 'dumped', 'error'
  alert_triggered BOOLEAN DEFAULT FALSE,
  
  created_at TIMESTAMP DEFAULT NOW()
);

-- Vista para análisis ML
CREATE OR REPLACE VIEW ml_features AS
SELECT 
  t.mint,
  t.symbol,
  t.pnl_percent,
  t.hold_time_min,
  t.liq_usd_at_entry,
  t.market_cap_at_entry,
  t.slope_at_entry,
  t.drawdown_at_entry,
  t.gain_percent_at_entry,
  t.elapsed_min_at_entry,
  t.slip_factor,
  t.partial_fill,
  t.failed,
  t.rules_matched,
  CASE WHEN t.pnl_percent > 0 THEN 1 ELSE 0 END as label_profitable,
  CASE WHEN t.pnl_percent > 20 THEN 1 ELSE 0 END as label_good,
  CASE WHEN t.pnl_percent > 50 THEN 1 ELSE 0 END as label_excellent,
  t.created_at
FROM dryrun_trades t
WHERE t.type = 'sell' AND t.exit_price IS NOT NULL;

-- Función para calcular estadísticas
CREATE OR REPLACE FUNCTION get_trading_stats()
RETURNS TABLE (
  total_trades BIGINT,
  wins BIGINT,
  losses BIGINT,
  winrate NUMERIC,
  avg_pnl NUMERIC,
  total_pnl NUMERIC,
  max_win NUMERIC,
  max_loss NUMERIC
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    COUNT(*)::BIGINT as total_trades,
    COUNT(*) FILTER (WHERE pnl_percent > 0)::BIGINT as wins,
    COUNT(*) FILTER (WHERE pnl_percent <= 0)::BIGINT as losses,
    ROUND(
      (COUNT(*) FILTER (WHERE pnl_percent > 0)::NUMERIC / NULLIF(COUNT(*), 0)) * 100, 
      2
    ) as winrate,
    ROUND(AVG(pnl_percent), 2) as avg_pnl,
    ROUND(SUM(pnl_percent), 2) as total_pnl,
    ROUND(MAX(pnl_percent), 2) as max_win,
    ROUND(MIN(pnl_percent), 2) as max_loss
  FROM dryrun_trades
  WHERE type = 'sell' AND exit_price IS NOT NULL;
END;
$$ LANGUAGE plpgsql;
