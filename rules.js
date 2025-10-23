// rules.js - Reglas de alerta mejoradas
const { getParam } = require('./redis');

async function checkEliteRules(token, CONFIG) {
  // Parámetros dinámicos desde Redis
  const minLiqUSD = await getParam('min_liq_usd', CONFIG.MIN_INITIAL_LIQUIDITY_USD);
  const maxDrawdown = await getParam('max_drawdown', 20); // 20% drawdown máximo
  const slopeMin = await getParam('slope_min', 0); // USD/min mínimo

  const alerts = [];
  const slope = (token.currentPrice - token.initialPrice) / Math.max(token.elapsedMinutes, 0.1);
  const ddOk = token.lossFromMaxPercent > -Math.abs(maxDrawdown);
  const liqOk = (token.initialMarketCap || 0) >= minLiqUSD;

  for (const rule of CONFIG.ALERT_RULES) {
    const fastEnough = token.gainPercent >= rule.percent && token.elapsedMinutes <= rule.timeWindowMin;
    if (fastEnough && ddOk && slope >= slopeMin && liqOk) {
      alerts.push({
        ruleName: rule.name,
        description: rule.description,
        gainPercent: token.gainPercent,
        timeElapsed: token.elapsedMinutes,
        priceAtAlert: token.currentPrice,
        marketCapAtAlert: token.currentPrice * (token.initialMarketCap / Math.max(token.initialPrice || 1e-9, 1e-9)),
        slope: slope
      });
    }
  }

  return alerts;
}

module.exports = { checkEliteRules };
