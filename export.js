// export.js - Exportación de datos para Machine Learning
const fs = require('fs');
const { getDryRunTrades } = require('./db');

async function exportDryRunCSV(path = 'dryrun_export.csv') {
  const trades = await getDryRunTrades();
  
  if (trades.length === 0) {
    throw new Error('No hay datos para exportar');
  }

  const headers = [
    'id',
    'type',
    'mint',
    'symbol',
    'entry_price',
    'exit_price',
    'amount_sol',
    'tokens',
    'slippage_bps',
    'slip_factor',
    'partial_fill',
    'pnl_percent',
    'hold_time_min',
    'rules_matched',
    'drawdown_at_entry',
    'slope_at_entry',
    'liq_usd_at_entry',
    'created_at'
  ];

  const csvContent = [
    headers.join(','),
    ...trades.map(trade => [
      trade.id,
      trade.type,
      trade.mint,
      trade.symbol,
      trade.entry_price,
      trade.exit_price,
      trade.amount_sol,
      trade.tokens,
      trade.slippage_bps,
      trade.slip_factor,
      trade.partial_fill,
      trade.pnl_percent,
      trade.hold_time_min,
      trade.rules_matched,
      trade.drawdown_at_entry,
      trade.slope_at_entry,
      trade.liq_usd_at_entry,
      trade.created_at
    ].join(','))
  ].join('\n');

  fs.writeFileSync(path, csvContent, 'utf8');
  console.log(`✅ CSV exportado: ${path} (${trades.length} registros)`);
  return path;
}

module.exports = { exportDryRunCSV };
