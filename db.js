// db.js
const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false,
});

// Inicializar tabla si no existe
async function initDB() {
  try {
    await pool.query(`
      CREATE TABLE IF NOT EXISTS dryrun_trades (
        id SERIAL PRIMARY KEY,
        type VARCHAR(10) NOT NULL,
        mint VARCHAR(44) NOT NULL,
        symbol VARCHAR(32),
        entry_price NUMERIC(20,10),
        exit_price NUMERIC(20,10),
        amount_sol NUMERIC(20,10),
        tokens NUMERIC(30,10),
        slippage_bps INTEGER,
        slip_factor NUMERIC(10,5),
        partial_fill NUMERIC(10,5),
        pnl_percent NUMERIC(10,4),
        hold_time_min NUMERIC(10,2),
        rules_matched TEXT,
        drawdown_at_entry NUMERIC(10,4),
        slope_at_entry NUMERIC(20,10),
        liq_usd_at_entry NUMERIC(20,2),
        created_at TIMESTAMP DEFAULT NOW()
      );
    `);
    console.log('✅ PostgreSQL initialized');
  } catch (error) {
    console.error('❌ PostgreSQL init failed:', error.message);
  }
}

// Guardar trade simulado
async function saveDryRunTrade(tradeData) {
  try {
    const query = `
      INSERT INTO dryrun_trades (
        type, mint, symbol, entry_price, exit_price, amount_sol, tokens,
        slippage_bps, slip_factor, partial_fill, pnl_percent, hold_time_min,
        rules_matched, drawdown_at_entry, slope_at_entry, liq_usd_at_entry
      ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
    `;
    
    const values = [
      tradeData.type,
      tradeData.mint,
      tradeData.symbol,
      tradeData.entryPrice,
      tradeData.exitPrice,
      tradeData.amountSol,
      tradeData.tokens,
      tradeData.slippageBps,
      tradeData.slipFactor,
      tradeData.partialFill,
      tradeData.pnlPercent,
      tradeData.holdTimeMin,
      Array.isArray(tradeData.rulesMatched) ? tradeData.rulesMatched.join('|') : tradeData.rulesMatched,
      tradeData.drawdownAtEntry,
      tradeData.slopeAtEntry,
      tradeData.liqUsdAtEntry
    ];

    await pool.query(query, values);
    return true;
  } catch (error) {
    console.error('❌ Save trade failed:', error.message);
    return false;
  }
}

// Obtener trades para export
async function getDryRunTrades() {
  try {
    const result = await pool.query('SELECT * FROM dryrun_trades ORDER BY created_at');
    return result.rows;
  } catch (error) {
    console.error('❌ Get trades failed:', error.message);
    return [];
  }
}

module.exports = {
  pool,
  initDB,
  saveDryRunTrade,
  getDryRunTrades,
};
