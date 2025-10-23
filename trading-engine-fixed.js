// trading-engine.js - VERSIÓN MEJORADA CON CIRCUIT BREAKER Y MÉTRICAS
const PumpFunTrading = require('./pump-api');
const PumpDataFetcher = require('./pump-data-fetcher');

// Circuit Breaker para protección contra pérdidas consecutivas
class CircuitBreaker {
  constructor(maxLosses = 3, resetTimeMs = 300000) {
    this.failures = 0;
    this.maxLosses = maxLosses;
    this.resetTimeMs = resetTimeMs;
    this.isOpen = false;
    this.lastFailureTime = null;
  }
  
  recordFailure() {
    this.failures++;
    this.lastFailureTime = Date.now();
    
    if (this.failures >= this.maxLosses) {
      this.isOpen = true;
      console.log(`🚨 CIRCUIT BREAKER ACTIVADO: ${this.failures} fallos consecutivos`);
      console.log(`⏰ Se reactivará en ${this.resetTimeMs / 60000} minutos`);
      
      setTimeout(() => this.reset(), this.resetTimeMs);
    }
  }
  
  recordSuccess() {
    if (this.failures > 0) {
      console.log(`✅ Trade exitoso - Reseteando contador de fallos (${this.failures} → 0)`);
    }
    this.failures = 0;
  }
  
  reset() {
    console.log('🔄 Circuit Breaker reseteado - Trading reactivado');
    this.failures = 0;
    this.isOpen = false;
    this.lastFailureTime = null;
  }
  
  canTrade() {
    return !this.isOpen;
  }
  
  getStatus() {
    return {
      isOpen: this.isOpen,
      failures: this.failures,
      maxLosses: this.maxLosses,
      lastFailure: this.lastFailureTime,
      resetInMs: this.isOpen ? Math.max(0, this.resetTimeMs - (Date.now() - this.lastFailureTime)) : 0
    };
  }
}

// Métricas de Performance
class PerformanceMetrics {
  constructor() {
    this.trades = [];
    this.maxHistory = 100; // Últimos 100 trades
  }
  
  recordTrade(tradeData) {
    this.trades.push({
      timestamp: Date.now(),
      success: tradeData.success,
      executionTimeMs: tradeData.executionTimeMs,
      slippage: tradeData.slippage || 0,
      pnl: tradeData.pnl || 0,
      retries: tradeData.retries || 0
    });
    
    // Mantener solo las últimas N trades
    if (this.trades.length > this.maxHistory) {
      this.trades.shift();
    }
  }
  
  getMetrics() {
    if (this.trades.length === 0) {
      return {
        totalTrades: 0,
        successRate: 0,
        avgExecutionTime: 0,
        avgSlippage: 0,
        avgPnL: 0,
        winRate: 0
      };
    }
    
    const successful = this.trades.filter(t => t.success);
    const withPnL = this.trades.filter(t => t.pnl !== 0);
    const wins = withPnL.filter(t => t.pnl > 0);
    
    return {
      totalTrades: this.trades.length,
      successRate: (successful.length / this.trades.length * 100).toFixed(2),
      avgExecutionTime: (successful.reduce((sum, t) => sum + t.executionTimeMs, 0) / successful.length).toFixed(0),
      avgSlippage: (successful.reduce((sum, t) => sum + Math.abs(t.slippage), 0) / successful.length).toFixed(3),
      avgPnL: withPnL.length > 0 ? (withPnL.reduce((sum, t) => sum + t.pnl, 0) / withPnL.length).toFixed(2) : 0,
      winRate: withPnL.length > 0 ? (wins.length / withPnL.length * 100).toFixed(2) : 0,
      avgRetries: (this.trades.reduce((sum, t) => sum + t.retries, 0) / this.trades.length).toFixed(2)
    };
  }
  
  getRecentPerformance(lastN = 10) {
    const recent = this.trades.slice(-lastN);
    const wins = recent.filter(t => t.pnl > 0);
    const losses = recent.filter(t => t.pnl < 0);
    
    return {
      trades: recent.length,
      wins: wins.length,
      losses: losses.length,
      totalPnL: recent.reduce((sum, t) => sum + t.pnl, 0).toFixed(2),
      streak: this.calculateStreak(recent)
    };
  }
  
  calculateStreak(trades) {
    if (trades.length === 0) return { type: 'none', count: 0 };
    
    let streak = 0;
    const lastResult = trades[trades.length - 1].pnl > 0 ? 'win' : 'loss';
    
    for (let i = trades.length - 1; i >= 0; i--) {
      const isWin = trades[i].pnl > 0;
      if ((lastResult === 'win' && isWin) || (lastResult === 'loss' && !isWin)) {
        streak++;
      } else {
        break;
      }
    }
    
    return { type: lastResult, count: streak };
  }
}

class TradingEngine {
  constructor(config) {
    this.config = config;
    this.trading = new PumpFunTrading(config);
    this.dataFetcher = new PumpDataFetcher(config.RPC_URL);
    this.activeTrades = new Map();
    this.failedAttempts = new Map();
    
    // Nuevas características
    this.circuitBreaker = new CircuitBreaker(3, 300000); // 3 fallos, 5min reset
    this.metrics = new PerformanceMetrics();
    this.lastRPCCheck = 0;
    this.rpcHealthy = true;
  }

  async checkRPCHealth() {
    // Verificar salud del RPC cada 30 segundos
    if (Date.now() - this.lastRPCCheck < 30000) {
      return this.rpcHealthy;
    }
    
    try {
      const start = Date.now();
      const connection = this.dataFetcher.connection;
      await connection.getSlot();
      const latency = Date.now() - start;
      
      this.rpcHealthy = latency < 2000; // 2 segundos max
      this.lastRPCCheck = Date.now();
      
      if (!this.rpcHealthy) {
        console.warn(`⚠️ RPC lento: ${latency}ms (max: 2000ms)`);
      }
      
      return this.rpcHealthy;
    } catch (error) {
      console.error(`❌ RPC Health Check falló: ${error.message}`);
      this.rpcHealthy = false;
      this.lastRPCCheck = Date.now();
      return false;
    }
  }

  async executeBuy(token, alert) {
    const startTime = Date.now();
    
    // Verificar Circuit Breaker
    if (!this.circuitBreaker.canTrade()) {
      const status = this.circuitBreaker.getStatus();
      console.log(`🚫 Circuit Breaker ABIERTO - Trading pausado`);
      console.log(`⏰ Se reactivará en ${(status.resetInMs / 60000).toFixed(1)} minutos`);
      
      return { 
        success: false, 
        reason: 'circuit_breaker_open',
        resetIn: status.resetInMs
      };
    }
    
    // Verificar RPC Health
    const rpcHealthy = await this.checkRPCHealth();
    if (!rpcHealthy) {
      console.log(`⚠️ RPC no saludable - skippeando trade`);
      return { success: false, reason: 'rpc_unhealthy' };
    }

    console.log(`🎯 Evaluando compra: ${token.symbol} +${alert.gainPercent.toFixed(1)}%`);

    // Verificar intentos previos fallidos
    const failCount = this.failedAttempts.get(token.mint) || 0;
    if (failCount >= 3) {
      console.log(`🚫 Token con ${failCount} fallos previos - skippeando`);
      return { success: false, reason: 'max_retries_exceeded' };
    }

    // Obtener datos actualizados
    const pumpData = await this.dataFetcher.getTokenData(token.mint);
    
    if (!pumpData || pumpData.price === 0) {
      console.log(`🚫 No se pudo obtener datos para ${token.symbol}`);
      this.incrementFailCount(token.mint);
      this.recordTradeMetrics(startTime, false, 0, 0);
      return { success: false, reason: 'no_bonding_data' };
    }

    // Validaciones de seguridad
    if (pumpData.bondingCurve >= this.config.MAX_BONDING_CURVE_PROGRESS) {
      console.log(`🚫 Bonding curve muy alta: ${pumpData.bondingCurve}%`);
      return { success: false, reason: 'bonding_curve_high' };
    }

    if (pumpData.liquidity < this.config.MIN_INITIAL_LIQUIDITY_USD) {
      console.log(`🚫 Liquidez insuficiente: $${pumpData.liquidity}`);
      return { success: false, reason: 'liquidity_low' };
    }

    // Verificar divergencia de precio (protección anti-frontrun)
    const priceDivergence = Math.abs((pumpData.price - token.currentPrice) / token.currentPrice) * 100;
    if (priceDivergence > 5) {
      console.log(`⚠️ Divergencia de precio: ${priceDivergence.toFixed(2)}% - posible frontrun`);
      return { success: false, reason: 'price_divergence' };
    }

    // MODO DRY_RUN
    if (this.config.DRY_RUN) {
      const simulatedSlippage = Math.random() * 0.01; // 0-1% slippage
      const slippageFactor = 1 + simulatedSlippage;
      const execPrice = pumpData.price * slippageFactor;
      const executionTime = Date.now() - startTime;
      
      console.log(`🧪 DRY_RUN BUY: ${token.symbol} @ $${execPrice.toFixed(8)} (slippage: ${(simulatedSlippage * 100).toFixed(3)}%)`);
      
      this.activeTrades.set(token.mint, {
        symbol: token.symbol,
        entryPrice: execPrice,
        referencePrice: pumpData.price,
        entryTime: Date.now(),
        amountSol: this.config.TRADE_AMOUNT_SOL || this.config.MINIMUM_BUY_AMOUNT,
        tokensBought: (this.config.TRADE_AMOUNT_SOL || this.config.MINIMUM_BUY_AMOUNT) / execPrice,
        simulated: true,
        bondingCurveAtEntry: pumpData.bondingCurve,
        liquidityAtEntry: pumpData.liquidity
      });

      this.circuitBreaker.recordSuccess();
      this.recordTradeMetrics(startTime, true, simulatedSlippage * 100, 0);

      return { 
        success: true, 
        simulated: true,
        price: execPrice,
        slippage: simulatedSlippage * 100,
        executionTime
      };
    }

    // MODO LIVE - Trading real con retry
    console.log(`🚀 LIVE TRADE: Comprando ${token.symbol}...`);
    
    let txHash = null;
    let retries = 0;
    const maxRetries = 2;

    while (!txHash && retries <= maxRetries) {
      try {
        const priorityFeeMultiplier = 1 + (retries * 0.5);
        const adjustedConfig = {
          ...this.config,
          PRIORITY_FEE_BASE: this.config.PRIORITY_FEE_BASE * priorityFeeMultiplier
        };
        
        const tradingWithAdjustedFee = new PumpFunTrading(adjustedConfig);
        txHash = await tradingWithAdjustedFee.buy(
          token.mint, 
          this.config.TRADE_AMOUNT_SOL || this.config.MINIMUM_BUY_AMOUNT
        );

        if (txHash) break;
        
        retries++;
        if (retries <= maxRetries) {
          console.log(`⚠️ Reintento ${retries}/${maxRetries} con priority fee ${(priorityFeeMultiplier * 100).toFixed(0)}%...`);
          await this.sleep(1000);
        }
      } catch (error) {
        console.error(`❌ Error en intento ${retries}: ${error.message}`);
        retries++;
        if (retries <= maxRetries) {
          await this.sleep(1000);
        }
      }
    }
    
    const executionTime = Date.now() - startTime;
    
    if (txHash) {
      this.failedAttempts.delete(token.mint);
      this.circuitBreaker.recordSuccess();
      
      this.activeTrades.set(token.mint, {
        symbol: token.symbol,
        entryPrice: pumpData.price,
        entryTime: Date.now(),
        amountSol: this.config.TRADE_AMOUNT_SOL || this.config.MINIMUM_BUY_AMOUNT,
        tokensBought: (this.config.TRADE_AMOUNT_SOL || this.config.MINIMUM_BUY_AMOUNT) / pumpData.price,
        txHash: txHash,
        simulated: false,
        bondingCurveAtEntry: pumpData.bondingCurve,
        liquidityAtEntry: pumpData.liquidity
      });

      this.recordTradeMetrics(startTime, true, 0, retries);
      console.log(`✅ COMPRA EXITOSA: ${token.symbol} - TX: ${txHash} (${executionTime}ms, ${retries} retries)`);
      
      return { success: true, txHash, retries, executionTime };

    } else {
      this.incrementFailCount(token.mint);
      this.circuitBreaker.recordFailure();
      this.recordTradeMetrics(startTime, false, 0, retries);
      
      console.log(`❌ COMPRA FALLADA después de ${maxRetries} intentos: ${token.symbol}`);
      return { success: false, reason: 'tx_failed_all_retries', retries, executionTime };
    }
  }

  async monitorAndSell(mint) {
    const trade = this.activeTrades.get(mint);
    if (!trade) {
      console.log(`🚫 No hay trade activo para ${mint}`);
      return;
    }

    console.log(`📊 Iniciando monitoreo para venta: ${trade.symbol}`);
    
    const maxMonitorTime = 5 * 60 * 1000; // 5 minutos
    const endTime = Date.now() + maxMonitorTime;
    let highestValue = trade.entryPrice * trade.tokensBought;
    let sold = false;
    let checkCount = 0;

    while (Date.now() < endTime && !sold) {
      checkCount++;
      
      const tokenData = await this.dataFetcher.getTokenData(mint);
      
      if (!tokenData || tokenData.price === 0) {
        console.log(`⚠️ No se pudieron obtener datos, reintentando...`);
        await this.sleep(3000);
        continue;
      }

      const currentValue = tokenData.price * trade.tokensBought;
      const gainPercent = ((tokenData.price - trade.entryPrice) / trade.entryPrice) * 100;
      const holdTimeMin = (Date.now() - trade.entryTime) / 60000;
      const drawdownFromMax = ((currentValue - highestValue) / highestValue) * 100;

      if (currentValue > highestValue) {
        highestValue = currentValue;
      }

      if (checkCount % 5 === 0) {
        console.log(`📈 ${trade.symbol}: ${gainPercent >= 0 ? '+' : ''}${gainPercent.toFixed(1)}% | Hold: ${holdTimeMin.toFixed(1)}min | BC: ${tokenData.bondingCurve}% | DD: ${drawdownFromMax.toFixed(1)}%`);
      }

      // ESTRATEGIA TREE CITY WES
      
      if (gainPercent >= 25 && !trade.tookProfit1) {
        console.log(`🎯 TP1 (+25%) - Vendiendo 50%`);
        const result = await this.executePartialSell(mint, 0.50, 'take_profit_1', tokenData.price);
        trade.tookProfit1 = true;
        trade.tokensBought = result.remainingTokens;
        
        if (result.success) {
          highestValue = tokenData.price * trade.tokensBought;
        }
      }
      
      else if (gainPercent <= -10) {
        console.log(`🛑 Stop Loss (-10%) - Vendiendo 100%`);
        await this.executePartialSell(mint, 1.00, 'stop_loss', tokenData.price);
        sold = true;
        break;
      }
      
      else if (trade.tookProfit1 && drawdownFromMax <= -15) {
        console.log(`📉 Trailing Stop (-15% desde max) - Vendiendo 50%`);
        await this.executePartialSell(mint, 0.50, 'trailing_stop', tokenData.price);
        sold = true;
        break;
      }
      
      else if (tokenData.bondingCurve >= this.config.SELL_BONDING_CURVE_PROGRESS) {
        console.log(`📊 BC ${tokenData.bondingCurve}% - Vendiendo 75%`);
        await this.executePartialSell(mint, 0.75, 'bonding_curve_limit', tokenData.price);
        sold = true;
        break;
      }
      
      else if (gainPercent >= 50 && trade.tookProfit1) {
        console.log(`🎯 TP2 (+50%) - Vendiendo 50% del restante`);
        const result = await this.executePartialSell(mint, 0.50, 'take_profit_2', tokenData.price);
        trade.tokensBought = result.remainingTokens;
        
        console.log(`🌙 Moon bag: ${trade.tokensBought.toFixed(2)} tokens`);
        sold = true;
        break;
      }

      await this.sleep(5000);
    }

    if (!sold && Date.now() >= endTime) {
      console.log(`⏰ Timeout (${maxMonitorTime/60000}min) - Cerrando posición`);
      const tokenData = await this.dataFetcher.getTokenData(mint);
      const currentGain = tokenData ? ((tokenData.price - trade.entryPrice) / trade.entryPrice) * 100 : 0;
      
      if (currentGain > 0) {
        await this.executePartialSell(mint, 0.75, 'timeout_profit', tokenData?.price);
      } else {
        await this.executePartialSell(mint, 1.00, 'timeout_loss', tokenData?.price);
      }
    }

    console.log(`🏁 Monitoreo finalizado: ${trade.symbol}`);
  }

  async executePartialSell(mint, percentage, reason, currentPrice) {
    const startTime = Date.now();
    const trade = this.activeTrades.get(mint);
    
    if (!trade) {
      return { success: false, remainingTokens: 0 };
    }

    const tokensToSell = trade.tokensBought * percentage;
    console.log(`💰 Ejecutando venta: ${(percentage * 100).toFixed(0)}% de ${trade.symbol} (${tokensToSell.toFixed(2)} tokens) - ${reason}`);

    // DRY_RUN
    if (trade.simulated || this.config.DRY_RUN) {
      const simulatedSlippage = -(Math.random() * 0.015); // 0-1.5% slippage negativo
      const slippageFactor = 1 + simulatedSlippage;
      const execPrice = currentPrice * slippageFactor;
      const pnl = ((execPrice - trade.entryPrice) / trade.entryPrice) * 100;
      const executionTime = Date.now() - startTime;
      
      console.log(`🧪 DRY_RUN SELL: ${(percentage * 100).toFixed(0)}% @ $${execPrice.toFixed(8)} | PNL: ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}% (${executionTime}ms)`);
      
      const remainingTokens = trade.tokensBought * (1 - percentage);
      
      if (remainingTokens <= 0) {
        this.activeTrades.delete(mint);
      } else {
        trade.tokensBought = remainingTokens;
      }
      
      // Registrar métricas de venta
      this.recordTradeMetrics(startTime, true, Math.abs(simulatedSlippage) * 100, 0, pnl);
      
      if (pnl > 0) {
        this.circuitBreaker.recordSuccess();
      } else if (pnl < -10) {
        this.circuitBreaker.recordFailure();
      }
      
      return { 
        simulated: true, 
        success: true, 
        remainingTokens,
        pnl,
        execPrice,
        executionTime
      };
    }

    // LIVE - Venta real con retry
    let txHash = null;
    let retries = 0;
    const maxRetries = 2;

    while (!txHash && retries <= maxRetries) {
      try {
        txHash = await this.trading.sell(mint, tokensToSell);
        
        if (txHash) break;
        
        retries++;
        if (retries <= maxRetries) {
          console.log(`⚠️ Reintento venta ${retries}/${maxRetries}...`);
          await this.sleep(1000);
        }
      } catch (error) {
        console.error(`❌ Error en venta: ${error.message}`);
        retries++;
      }
    }

    const executionTime = Date.now() - startTime;

    if (txHash) {
      const remainingTokens = trade.tokensBought * (1 - percentage);
      
      if (remainingTokens <= 0) {
        this.activeTrades.delete(mint);
      } else {
        trade.tokensBought = remainingTokens;
      }
      
      const pnl = ((currentPrice - trade.entryPrice) / trade.entryPrice) * 100;
      
      this.recordTradeMetrics(startTime, true, 0, retries, pnl);
      
      if (pnl > 0) {
        this.circuitBreaker.recordSuccess();
      } else if (pnl < -10) {
        this.circuitBreaker.recordFailure();
      }
      
      console.log(`✅ VENTA EXITOSA: ${(percentage * 100).toFixed(0)}% de ${trade.symbol} | PNL: ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}% - TX: ${txHash} (${executionTime}ms)`);
      
      return { 
        success: true, 
        txHash, 
        remainingTokens,
        pnl,
        executionTime
      };
      
    } else {
      this.circuitBreaker.recordFailure();
      this.recordTradeMetrics(startTime, false, 0, retries);
      
      console.log(`❌ VENTA FALLADA después de ${maxRetries} intentos`);
      return { 
        success: false,
        remainingTokens: trade.tokensBought,
        executionTime
      };
    }
  }

  recordTradeMetrics(startTime, success, slippage, retries, pnl = 0) {
    this.metrics.recordTrade({
      success,
      executionTimeMs: Date.now() - startTime,
      slippage,
      pnl,
      retries
    });
  }

  incrementFailCount(mint) {
    const current = this.failedAttempts.get(mint) || 0;
    this.failedAttempts.set(mint, current + 1);
    
    setTimeout(() => {
      this.failedAttempts.delete(mint);
    }, 3600000);
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  getActiveTrades() {
    return Array.from(this.activeTrades.entries()).map(([mint, trade]) => ({
      mint,
      ...trade,
      holdTimeMin: (Date.now() - trade.entryTime) / 60000,
      currentPnL: trade.entryPrice ? ((trade.entryPrice / trade.entryPrice - 1) * 100).toFixed(2) : 0
    }));
  }

  getStats() {
    const circuitStatus = this.circuitBreaker.getStatus();
    const performanceMetrics = this.metrics.getMetrics();
    const recentPerformance = this.metrics.getRecentPerformance(10);
    
    return {
      activeTrades: this.activeTrades.size,
      failedMints: this.failedAttempts.size,
      rpcHealthy: this.rpcHealthy,
      circuitBreaker: {
        isOpen: circuitStatus.isOpen,
        failures: circuitStatus.failures,
        maxLosses: circuitStatus.maxLosses,
        resetInMinutes: (circuitStatus.resetInMs / 60000).toFixed(1)
      },
      performance: performanceMetrics,
      recent: recentPerformance,
      trades: this.getActiveTrades()
    };
  }

  // Método para obtener reporte detallado
  getDetailedReport() {
    const stats = this.getStats();
    
    console.log('\n' + '='.repeat(70));
    console.log('📊 REPORTE DE TRADING ENGINE');
    console.log('='.repeat(70));
    
    console.log('\n🔄 Estado General:');
    console.log(`  • Trades Activos: ${stats.activeTrades}`);
    console.log(`  • RPC Saludable: ${stats.rpcHealthy ? '✅' : '❌'}`);
    console.log(`  • Circuit Breaker: ${stats.circuitBreaker.isOpen ? '🔴 ABIERTO' : '🟢 CERRADO'}`);
    
    if (stats.circuitBreaker.isOpen) {
      console.log(`  • Reset en: ${stats.circuitBreaker.resetInMinutes} minutos`);
    }
    
    console.log('\n📈 Métricas de Performance:');
    console.log(`  • Total Trades: ${stats.performance.totalTrades}`);
    console.log(`  • Success Rate: ${stats.performance.successRate}%`);
    console.log(`  • Win Rate: ${stats.performance.winRate}%`);
    console.log(`  • Avg Execution Time: ${stats.performance.avgExecutionTime}ms`);
    console.log(`  • Avg Slippage: ${stats.performance.avgSlippage}%`);
    console.log(`  • Avg PnL: ${stats.performance.avgPnL}%`);
    console.log(`  • Avg Retries: ${stats.performance.avgRetries}`);
    
    console.log('\n🔥 Performance Reciente (últimos 10):');
    console.log(`  • Wins: ${stats.recent.wins} | Losses: ${stats.recent.losses}`);
    console.log(`  • PnL Total: ${stats.recent.totalPnL}%`);
    console.log(`  • Racha: ${stats.recent.streak.count}x ${stats.recent.streak.type}`);
    
    if (stats.trades.length > 0) {
      console.log('\n💼 Trades Activos:');
      stats.trades.forEach(t => {
        console.log(`  • ${t.symbol}: ${t.currentPnL}% (${t.holdTimeMin.toFixed(1)}min)`);
      });
    }
    
    console.log('\n' + '='.repeat(70) + '\n');
    
    return stats;
  }
}

module.exports = TradingEngine;
