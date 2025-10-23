// trading-engine.js - Estrategia probada de TreeCityWes mejorada
const PumpFunTrading = require('./pump-api');
const { getTokenDataFromBondingCurve } = require('./pump-sdk');

class TradingEngine {
  constructor(config) {
    this.config = config;
    this.trading = new PumpFunTrading(config);
    this.activeTrades = new Map(); // mint -> tradeData
  }

  async executeBuy(token, alert) {
    console.log(`🎯 Evaluando compra: ${token.symbol} +${alert.gainPercent.toFixed(1)}%`);

    // Verificar bonding curve
    const pumpData = await getTokenDataFromBondingCurve(token.mint);
    if (!pumpData) {
      console.log(`🚫 No se pudo obtener datos de bonding curve para ${token.symbol}`);
      return { success: false, reason: 'no_bonding_data' };
    }

    if (pumpData.bondingCurve >= this.config.MAX_BONDING_CURVE_PROGRESS) {
      console.log(`🚫 Bonding curve muy alta: ${pumpData.bondingCurve}% (max: ${this.config.MAX_BONDING_CURVE_PROGRESS}%)`);
      return { success: false, reason: 'bonding_curve_high' };
    }

    // Verificar liquidez
    if (pumpData.liquidity < this.config.MIN_INITIAL_LIQUIDITY_USD) {
      console.log(`🚫 Liquidez insuficiente: $${pumpData.liquidity} (min: $${this.config.MIN_INITIAL_LIQUIDITY_USD})`);
      return { success: false, reason: 'liquidity_low' };
    }

    // MODO DRY_RUN - Simulación
    if (this.config.DRY_RUN) {
      console.log(`🧪 DRY_RUN BUY: ${token.symbol} @ $${token.currentPrice.toFixed(8)}`);
      
      this.activeTrades.set(token.mint, {
        symbol: token.symbol,
        entryPrice: token.currentPrice,
        entryTime: Date.now(),
        amountSol: this.config.MINIMUM_BUY_AMOUNT,
        tokensBought: this.config.MINIMUM_BUY_AMOUNT / token.currentPrice,
        simulated: true
      });

      return { 
        success: true, 
        simulated: true,
        message: `DRY_RUN BUY ejecutado para ${token.symbol}`
      };
    }

    // MODO LIVE - Trading real
    console.log(`🚀 LIVE TRADE: Comprando ${token.symbol}...`);
    
    const txHash = await this.trading.buy(token.mint, this.config.MINIMUM_BUY_AMOUNT);
    
    if (txHash) {
      this.activeTrades.set(token.mint, {
        symbol: token.symbol,
        entryPrice: token.currentPrice,
        entryTime: Date.now(),
        amountSol: this.config.MINIMUM_BUY_AMOUNT,
        tokensBought: this.config.MINIMUM_BUY_AMOUNT / token.currentPrice,
        txHash: txHash,
        simulated: false
      });

      console.log(`✅ COMPRA EXITOSA: ${token.symbol} - TX: ${txHash}`);
      return { success: true, txHash };

    } else {
      console.log(`❌ COMPRA FALLADA: ${token.symbol}`);
      return { success: false, reason: 'tx_failed' };
    }
  }

  async monitorAndSell(mint) {
    const trade = this.activeTrades.get(mint);
    if (!trade) {
      console.log(`🚫 No hay trade activo para ${mint}`);
      return;
    }

    console.log(`📊 Iniciando monitoreo para venta: ${trade.symbol}`);
    
    const endTime = Date.now() + (2 * 60 * 1000); // 2 minutos timeout
    let lastMarketValue = trade.entryPrice * trade.tokensBought;
    let sold = false;

    while (Date.now() < endTime && !sold) {
      const tokenData = await getTokenDataFromBondingCurve(mint);
      
      if (!tokenData) {
        await this.sleep(5000);
        continue;
      }

      const currentValue = tokenData.price * trade.tokensBought;
      const gainPercent = ((currentValue - trade.entryPrice) / trade.entryPrice) * 100;
      const holdTimeMin = (Date.now() - trade.entryTime) / 60000;

      console.log(`📈 ${trade.symbol}: ${gainPercent >= 0 ? '+' : ''}${gainPercent.toFixed(1)}% | ${holdTimeMin.toFixed(1)}min | BC: ${tokenData.bondingCurve}%`);

      // ESTRATEGIA TREE CITY WES - Take Profit 1 (25%)
      if (gainPercent >= 25 && !trade.tookProfit1) {
        console.log(`🎯 Take Profit 1 (+25%) - Vendiendo 50%`);
        await this.executePartialSell(mint, 0.50, 'take_profit_1');
        trade.tookProfit1 = true;
        lastMarketValue = currentValue;
      }
      
      // Stop Loss (-10%)
      else if (gainPercent <= -10) {
        console.log(`🛑 Stop Loss (-10%) - Vendiendo 100%`);
        await this.executePartialSell(mint, 1.00, 'stop_loss');
        sold = true;
        break;
      }
      
      // Bonding Curve alcanzó límite
      else if (tokenData.bondingCurve >= this.config.SELL_BONDING_CURVE_PROGRESS) {
        console.log(`📊 Bonding Curve ${tokenData.bondingCurve}% - Vendiendo 75%`);
        await this.executePartialSell(mint, 0.75, 'bonding_curve_limit');
        sold = true;
        break;
      }
      
      // Take Profit 2 (50% total desde entrada)
      else if (currentValue >= lastMarketValue * this.config.PROFIT_TARGET_2 && trade.tookProfit1) {
        console.log(`🎯 Take Profit 2 (+50% total) - Vendiendo 75% del restante`);
        await this.executePartialSell(mint, 0.75, 'take_profit_2');
        lastMarketValue = currentValue;
      }

      await this.sleep(5000); // Check cada 5 segundos
    }

    // Timeout - Estrategia conservadora
    if (!sold && Date.now() >= endTime) {
      console.log(`⏰ Timeout (2min) - Vendiendo 75%, manteniendo 25% moon bag`);
      await this.executePartialSell(mint, 0.75, 'timeout');
    }

    console.log(`🏁 Monitoreo finalizado para ${trade.symbol}`);
  }

  async executePartialSell(mint, percentage, reason) {
    const trade = this.activeTrades.get(mint);
    if (!trade) return;

    console.log(`💰 Ejecutando venta: ${percentage * 100}% de ${trade.symbol} - Razón: ${reason}`);

    // DRY_RUN - Simulación
    if (trade.simulated || this.config.DRY_RUN) {
      console.log(`🧪 DRY_RUN SELL: ${percentage * 100}% de ${trade.symbol}`);
      
      // Actualizar posición simulada
      trade.tokensBought = trade.tokensBought * (1 - percentage);
      if (trade.tokensBought <= 0) {
        this.activeTrades.delete(mint);
      }
      
      return { simulated: true, success: true };
    }

    // LIVE - Venta real
    const tokensToSell = trade.tokensBought * percentage;
    const txHash = await this.trading.sell(mint, tokensToSell);

    if (txHash) {
      // Actualizar posición real
      trade.tokensBought = trade.tokensBought * (1 - percentage);
      if (trade.tokensBought <= 0) {
        this.activeTrades.delete(mint);
      }
      
      console.log(`✅ VENTA EXITOSA: ${percentage * 100}% de ${trade.symbol} - TX: ${txHash}`);
      return { success: true, txHash };
      
    } else {
      console.log(`❌ VENTA FALLADA: ${trade.symbol}`);
      return { success: false };
    }
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  getActiveTrades() {
    return Array.from(this.activeTrades.entries()).map(([mint, trade]) => ({
      mint,
      ...trade
    }));
  }
}

module.exports = TradingEngine;
