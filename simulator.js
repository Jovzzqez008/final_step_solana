// simulator.js - Simulación de trades con realismo
const { incrStat } = require('./redis');
const { saveDryRunTrade } = require('./db');

function simulateBuy(priceUsd, amountSol, slippageBps = 100) {
  const slipFactor = 1 + (Math.random() * slippageBps) / 10000;
  const fail = Math.random() < 0.05; // 5% de fallos de ejecución
  const partial = 0.8 + Math.random() * 0.2; // 80-100% fill

  if (fail) {
    return { 
      failed: true,
      reason: 'execution_failed'
    };
  }

  const execPrice = priceUsd * slipFactor;
  const tokensBought = (amountSol / execPrice) * partial;

  return {
    failed: false,
    execPrice,
    tokensBought,
    slipFactor,
    partialFill: partial,
    slippageBps
  };
}

function simulateSell(entryPrice, currentPrice, tokens, slippageBps = 50) {
  const fail = Math.random() < 0.05;
  const partial = 0.8 + Math.random() * 0.2;
  const slipFactor = 1 - (Math.random() * slippageBps) / 10000; // Precio de venta peor

  if (fail) {
    return { 
      failed: true,
      reason: 'execution_failed'
    };
  }

  const execPrice = currentPrice * slipFactor;
  const pnlPercent = ((execPrice - entryPrice) / entryPrice) * 100;
  const tokensSold = tokens * partial;

  return {
    failed: false,
    execPrice,
    tokensSold,
    pnlPercent,
    partialFill: partial,
    slipFactor,
    slippageBps
  };
}

async function recordDryRunTrade(tradeData) {
  try {
    const saved = await saveDryRunTrade(tradeData);
    if (saved) {
      await incrStat('dryrun_trades');
      if (tradeData.type === 'sell') {
        if (tradeData.pnlPercent > 0) {
          await incrStat('dryrun_wins');
        } else {
          await incrStat('dryrun_losses');
        }
      }
      return true;
    }
    return false;
  } catch (error) {
    console.error('❌ Error recording dry run trade:', error.message);
    return false;
  }
}

module.exports = {
  simulateBuy,
  simulateSell,
  recordDryRunTrade
};
