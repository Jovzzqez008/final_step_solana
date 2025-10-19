// trading-bot-hybrid.js - Pump.fun Trading Bot DEFINITIVO
// 🚀 Bot robusto con datos DIRECTOS de blockchain + Smart Trader
// 💰 Sin dependencias de APIs externas para pricing

const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, Keypair, VersionedTransaction, PublicKey } = require('@solana/web3.js');
const bs58 = require('bs58');

// ============================================================================
// PUMP.FUN PROGRAM CONSTANTS (desde el IDL)
// ============================================================================

const PUMP_PROGRAM = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');
const TOKEN_PROGRAM = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');

// Constantes de bonding curve (valores iniciales del programa)
const LAMPORTS_PER_SOL = 1_000_000_000;

// ============================================================================
// CONFIGURACIÓN
// ============================================================================

const CONFIG = {
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || '',
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY || '',
  
  // RPC - Prioridad: Helius > Públicos
  HELIUS_API_KEY: process.env.HELIUS_API_KEY || '',
  
  PUMPPORTAL_WSS: 'wss://pumpportal.fun/api/data',
  PUMPPORTAL_API: 'https://pumpportal.fun/api/trade-local',
  
  // Trading
  TRADE_AMOUNT_SOL: parseFloat(process.env.TRADE_AMOUNT_SOL || '0.007'),
  HARD_STOP_LOSS_PERCENT: parseFloat(process.env.HARD_STOP_LOSS || '-45'),
  QUICK_STOP_PERCENT: parseFloat(process.env.QUICK_STOP || '-25'),
  QUICK_STOP_TIME_SEC: 120,
  TRAILING_STOP_ACTIVATION: parseFloat(process.env.TRAILING_ACTIVATION || '40'),
  TRAILING_STOP_PERCENT: parseFloat(process.env.TRAILING_PERCENT || '-20'),
  
  TAKE_PROFIT_TARGETS: [
    { percent: 80, sellPercent: 40 },
    { percent: 150, sellPercent: 30 },
    { percent: 300, sellPercent: 100 }
  ],
  
  MAX_HOLD_TIME_MIN: parseFloat(process.env.MAX_HOLD_TIME_MIN || '12'),
  STAGNANT_TIME_MIN: parseFloat(process.env.STAGNANT_TIME_MIN || '4'),
  MAX_WATCH_TIME_SEC: parseFloat(process.env.MAX_WATCH_TIME_SEC || '60'),
  
  // Smart Trader - Umbrales de detección
  EARLY_VELOCITY_MIN: parseFloat(process.env.EARLY_VELOCITY_MIN || '15'),
  EARLY_TIME_WINDOW: parseFloat(process.env.EARLY_TIME_WINDOW || '30'),
  CONFIRMATION_VELOCITY: parseFloat(process.env.CONFIRMATION_VELOCITY || '35'),
  CONFIRMATION_TIME: parseFloat(process.env.CONFIRMATION_TIME || '60'),
  MIN_VOLUME_SOL: parseFloat(process.env.MIN_VOLUME_SOL || '0.3'),
  MIN_BUY_COUNT: parseInt(process.env.MIN_BUY_COUNT || '5'),
  MIN_UNIQUE_BUYERS: parseInt(process.env.MIN_UNIQUE_BUYERS || '4'),
  MIN_HOLDERS: parseInt(process.env.MIN_HOLDERS || '8'),
  MAX_TOP_HOLDER_PERCENT: parseFloat(process.env.MAX_TOP_HOLDER_PERCENT || '40'),
  
  MIN_LIQUIDITY_SOL: parseFloat(process.env.MIN_LIQUIDITY_SOL || '0.5'),
  MAX_CONCURRENT_POSITIONS: parseInt(process.env.MAX_CONCURRENT_POSITIONS || '3'),
  
  DUMP_DETECTION_PERCENT: parseFloat(process.env.DUMP_DETECTION_PERCENT || '-15'),
  DUMP_TIME_WINDOW: parseFloat(process.env.DUMP_TIME_WINDOW || '30'),
  
  TRADING_ENABLED: process.env.TRADING_ENABLED !== 'false',
  DRY_RUN: process.env.DRY_RUN !== 'false',
  
  SLIPPAGE: parseFloat(process.env.SLIPPAGE || '25'),
  PRIORITY_FEE: parseFloat(process.env.PRIORITY_FEE || '0.0005'),
  
  PRICE_CHECK_INTERVAL_MS: parseInt(process.env.CHECK_INTERVAL_MS || '3000'),
  
  HEALTH_PORT: process.env.PORT || 8080,
  
  SOL_PRICE_USD: 150 // Estimado para cálculos
};

// ============================================================================
// BLOCKCHAIN UTILITIES - PUMP.FUN
// ============================================================================

/**
 * Deriva la bonding curve PDA para un mint según el programa Pump.fun
 */
function getBondingCurvePDA(mint) {
  const [pda] = PublicKey.findProgramAddressSync(
    [Buffer.from('bonding-curve'), new PublicKey(mint).toBuffer()],
    PUMP_PROGRAM
  );
  return pda;
}

/**
 * Obtiene datos reales de la bonding curve desde blockchain
 */
async function getBondingCurveData(mint) {
  try {
    const bondingCurvePDA = getBondingCurvePDA(mint);
    const accountInfo = await connection.getAccountInfo(bondingCurvePDA);
    
    if (!accountInfo || accountInfo.data.length < 49) {
      return null;
    }
    
    const data = accountInfo.data;
    
    // Estructura según IDL de Pump.fun:
    // discriminator: 8 bytes
    // virtualTokenReserves: u64 (offset 8)
    // virtualSolReserves: u64 (offset 16)
    // realTokenReserves: u64 (offset 24)
    // realSolReserves: u64 (offset 32)
    // tokenTotalSupply: u64 (offset 40)
    // complete: bool (offset 48)
    
    const virtualTokenReserves = data.readBigUInt64LE(8);
    const virtualSolReserves = data.readBigUInt64LE(16);
    const realTokenReserves = data.readBigUInt64LE(24);
    const realSolReserves = data.readBigUInt64LE(32);
    const tokenTotalSupply = data.readBigUInt64LE(40);
    const complete = data.readUInt8(48) === 1;
    
    // Calcular precio usando la fórmula de bonding curve
    // price = virtualSolReserves / virtualTokenReserves
    const priceSOL = Number(virtualSolReserves) / Number(virtualTokenReserves);
    const priceUSD = priceSOL * CONFIG.SOL_PRICE_USD;
    
    // Market cap
    const circulatingSupply = Number(tokenTotalSupply) - Number(realTokenReserves);
    const marketCapSOL = (circulatingSupply * priceSOL) / 1e9;
    const marketCapUSD = marketCapSOL * CONFIG.SOL_PRICE_USD;
    
    // Liquidez (SOL en la curva)
    const liquiditySOL = Number(realSolReserves) / LAMPORTS_PER_SOL;
    const liquidityUSD = liquiditySOL * CONFIG.SOL_PRICE_USD;
    
    // Progreso hacia graduación (85 SOL = bonding curve completa)
    const progress = (liquiditySOL / 85) * 100;
    
    return {
      bondingCurve: bondingCurvePDA.toBase58(),
      virtualTokenReserves: Number(virtualTokenReserves),
      virtualSolReserves: Number(virtualSolReserves),
      realTokenReserves: Number(realTokenReserves),
      realSolReserves: Number(realSolReserves),
      tokenTotalSupply: Number(tokenTotalSupply),
      complete,
      priceSOL,
      priceUSD,
      marketCapSOL,
      marketCapUSD,
      liquiditySOL,
      liquidityUSD,
      circulatingSupply,
      progress: Math.min(progress, 100)
    };
  } catch (error) {
    log.debug(`Error getting bonding curve: ${error.message}`);
    return null;
  }
}

/**
 * Obtiene holders del token desde blockchain
 */
async function getTokenHolders(mint) {
  try {
    const mintPubkey = new PublicKey(mint);
    
    // Usar getProgramAccounts para obtener todas las token accounts
    const accounts = await connection.getProgramAccounts(
      TOKEN_PROGRAM,
      {
        filters: [
          { dataSize: 165 }, // Token account data size
          {
            memcmp: {
              offset: 0,
              bytes: mintPubkey.toBase58()
            }
          }
        ]
      }
    );
    
    let holders = [];
    let totalSupply = 0;
    
    for (const { account } of accounts) {
      // Token amount está en offset 64 (u64)
      const amount = account.data.readBigUInt64LE(64);
      const balance = Number(amount);
      
      if (balance > 0) {
        holders.push(balance);
        totalSupply += balance;
      }
    }
    
    holders.sort((a, b) => b - a);
    
    const topHolderPercent = holders.length > 0 && totalSupply > 0
      ? (holders[0] / totalSupply) * 100
      : 0;
    
    return {
      holderCount: holders.length,
      topHolderPercent,
      totalSupply,
      top10Sum: holders.slice(0, 10).reduce((a, b) => a + b, 0)
    };
  } catch (error) {
    log.debug(`Error getting holders: ${error.message}`);
    return null;
  }
}

// ============================================================================
// HELIUS SMART TRADER CLASS
// ============================================================================

class HeliusSmartTrader {
  constructor(config) {
    this.config = config;
    this.connection = null;
    this.watchlist = new Map();
    this.tradeWatchers = new Map(); // WebSocket watchers por token
    this.stats = {
      tokensAnalyzed: 0,
      tokensEntered: 0,
      tokensRejected: 0,
      wins: 0,
      losses: 0,
      totalProfit: 0,
      totalHoldTime: 0,
      blockchainCalls: 0
    };
  }

  init(connection) {
    this.connection = connection;
  }

  async analyzeToken({ mint, symbol, name, uri }) {
    this.stats.tokensAnalyzed++;
    
    if (this.watchlist.has(mint)) {
      return { shouldWatch: false, reason: 'already_watching' };
    }

    const watch = {
      mint,
      symbol: symbol || 'UNKNOWN',
      name: name || 'UNKNOWN',
      uri: uri || '',
      phase: 'watching',
      firstSeenTime: Date.now(),
      firstSeenPrice: 0,
      currentPrice: 0,
      maxPrice: 0,
      minPrice: 0,
      liquiditySOL: 0,
      liquidityUSD: 0,
      buyCount: 0,
      sellCount: 0,
      uniqueBuyers: new Set(),
      uniqueSellers: new Set(),
      holders: 0,
      topHolderPercent: 0,
      priceHistory: [],
      checksCount: 0,
      lastCheckTime: Date.now(),
      entryPrice: 0,
      exitReason: null,
      trailingStopActive: false,
      lastMoveTime: Date.now(),
      partialSellsDone: [],
      initialHolders: 0
    };

    this.watchlist.set(mint, watch);
    
    // Suscribirse a trades del token en tiempo real
    this.subscribeToTokenTrades(mint);
    
    // Iniciar monitoreo
    this.monitorToken(mint);
    
    return { shouldWatch: true };
  }

  /**
   * Suscribirse a trades en tiempo real del token usando PumpPortal
   */
  subscribeToTokenTrades(mint) {
    try {
      const ws = new WebSocket(CONFIG.PUMPPORTAL_WSS);
      
      ws.on('open', () => {
        ws.send(JSON.stringify({
          method: 'subscribeTokenTrade',
          keys: [mint]
        }));
        log.debug(`Suscrito a trades de ${mint.slice(0, 8)}`);
      });
      
      ws.on('message', (data) => {
        try {
          const trade = JSON.parse(data);
          this.handleTokenTrade(mint, trade);
        } catch (err) {
          // Ignorar errores de parsing
        }
      });
      
      ws.on('error', () => {
        // Silenciar errores
      });
      
      this.tradeWatchers.set(mint, ws);
      
      // Cerrar después de 2 minutos
      setTimeout(() => {
        if (this.tradeWatchers.has(mint)) {
          ws.close();
          this.tradeWatchers.delete(mint);
        }
      }, 120000);
      
    } catch (error) {
      // Continuar sin suscripción si falla
    }
  }

  /**
   * Manejar trade en tiempo real
   */
  handleTokenTrade(mint, trade) {
    const watch = this.watchlist.get(mint);
    if (!watch) return;
    
    if (trade.tradeType === 'buy') {
      watch.buyCount++;
      if (trade.user) watch.uniqueBuyers.add(trade.user);
    } else if (trade.tradeType === 'sell') {
      watch.sellCount++;
      if (trade.user) watch.uniqueSellers.add(trade.user);
    }
    
    // Actualizar volumen aproximado
    if (trade.sol_amount) {
      watch.liquiditySOL = Math.max(watch.liquiditySOL, trade.sol_amount / 1e9);
    }
  }

  async monitorToken(mint) {
    const watch = this.watchlist.get(mint);
    if (!watch) return;

    while (this.watchlist.has(mint) && watch.phase !== 'exited') {
      await this.sleep(this.config.PRICE_CHECK_INTERVAL_MS);
      
      const currentWatch = this.watchlist.get(mint);
      if (!currentWatch) break;

      await this.updateWatch(mint);
      await this.checkSignals(mint);
    }
  }

  async updateWatch(mint) {
    const watch = this.watchlist.get(mint);
    if (!watch) return;

    try {
      watch.checksCount++;
      this.stats.blockchainCalls++;
      
      // Obtener datos DIRECTOS de blockchain
      const [bondingData, holdersData] = await Promise.all([
        getBondingCurveData(mint),
        // Holders cada 3 checks (más costoso)
        watch.checksCount % 3 === 0 ? getTokenHolders(mint) : null
      ]);

      // Usar datos de bonding curve (siempre más confiable)
      if (bondingData) {
        watch.currentPrice = bondingData.priceUSD;
        watch.maxPrice = Math.max(watch.maxPrice || 0, bondingData.priceUSD);
        watch.minPrice = watch.minPrice === 0 ? bondingData.priceUSD : Math.min(watch.minPrice, bondingData.priceUSD);
        watch.liquiditySOL = bondingData.liquiditySOL;
        watch.liquidityUSD = bondingData.liquidityUSD;

        if (watch.firstSeenPrice === 0) {
          watch.firstSeenPrice = bondingData.priceUSD;
        }

        watch.priceHistory.push({
          time: Date.now(),
          price: bondingData.priceUSD
        });

        // Mantener solo últimos 90s
        const cutoff = Date.now() - 90000;
        watch.priceHistory = watch.priceHistory.filter(p => p.time > cutoff);
      }

      // Actualizar holders
      if (holdersData) {
        watch.holders = holdersData.holderCount;
        watch.topHolderPercent = holdersData.topHolderPercent;
        
        if (watch.initialHolders === 0) {
          watch.initialHolders = holdersData.holderCount;
        }
      }

      watch.lastCheckTime = Date.now();

      // Log cada 5 checks
      if (watch.checksCount % 5 === 0 && watch.phase === 'watching') {
        const elapsed = (Date.now() - watch.firstSeenTime) / 1000;
        const velocity = this.getVelocity(watch);
        console.log(
          `[TRADER] 📊 ${watch.symbol}: ${velocity >= 0 ? '+' : ''}${velocity.toFixed(1)}% | ` +
          `Liq: ${watch.liquiditySOL.toFixed(2)} SOL | ` +
          `Buys: ${watch.buyCount} | ` +
          `Holders: ${watch.holders} | ` +
          `${elapsed.toFixed(0)}s`
        );
      }

    } catch (error) {
      log.debug(`Error updating ${mint.slice(0, 8)}: ${error.message}`);
    }
  }

  async checkSignals(mint) {
    const watch = this.watchlist.get(mint);
    if (!watch) return;

    const elapsed = (Date.now() - watch.firstSeenTime) / 1000;

    // ════════════════════════════════════════════════════════════════════
    // FASE: WATCHING - Detectar entrada
    // ════════════════════════════════════════════════════════════════════
    if (watch.phase === 'watching') {
      if (elapsed > this.config.MAX_WATCH_TIME_SEC) {
        this.rejectToken(mint, 'timeout');
        return;
      }

      const velocity = this.getVelocity(watch);
      const hasEarlySignal = velocity >= this.config.EARLY_VELOCITY_MIN && elapsed <= this.config.EARLY_TIME_WINDOW;
      const hasConfirmation = velocity >= this.config.CONFIRMATION_VELOCITY && elapsed <= this.config.CONFIRMATION_TIME;

      if (!hasEarlySignal && !hasConfirmation) return;

      // Validaciones
      if (watch.liquiditySOL < this.config.MIN_LIQUIDITY_SOL) {
        this.rejectToken(mint, `liq_${watch.liquiditySOL.toFixed(2)}SOL`);
        return;
      }

      if (watch.buyCount < this.config.MIN_BUY_COUNT) {
        this.rejectToken(mint, `buys_${watch.buyCount}`);
        return;
      }

      if (watch.uniqueBuyers.size < this.config.MIN_UNIQUE_BUYERS) {
        this.rejectToken(mint, `buyers_${watch.uniqueBuyers.size}`);
        return;
      }

      if (watch.holders > 0 && watch.holders < this.config.MIN_HOLDERS) {
        this.rejectToken(mint, `holders_${watch.holders}`);
        return;
      }

      if (watch.topHolderPercent > this.config.MAX_TOP_HOLDER_PERCENT) {
        this.rejectToken(mint, `whale_${watch.topHolderPercent.toFixed(0)}%`);
        return;
      }

      // ✅ SEÑAL DE ENTRADA
      watch.phase = 'entering';
      watch.entryPrice = watch.currentPrice;
      this.stats.tokensEntered++;

      console.log(`[TRADER] 🚀 SEÑAL: ${watch.symbol} | +${velocity.toFixed(1)}% | ${watch.buyCount} buys | ${watch.liquiditySOL.toFixed(2)} SOL`);
    }
    
    // ════════════════════════════════════════════════════════════════════
    // FASE: HOLDING - Monitorear salidas
    // ════════════════════════════════════════════════════════════════════
    else if (watch.phase === 'holding') {
      const holdTime = (Date.now() - watch.firstSeenTime) / 60000;
      const profit = this.getProfitPercent(watch);
      const profitFromMax = this.getProfitFromMax(watch);

      // Hard Stop
      if (profit <= this.config.HARD_STOP_LOSS_PERCENT) {
        watch.phase = 'exiting';
        watch.exitReason = 'hard_stop_loss';
        console.log(`[TRADER] 🛑 HARD STOP: ${watch.symbol} @ ${profit.toFixed(1)}%`);
        return;
      }

      // Quick Stop
      if (holdTime < (this.config.QUICK_STOP_TIME_SEC / 60) && profit <= this.config.QUICK_STOP_PERCENT) {
        watch.phase = 'exiting';
        watch.exitReason = 'quick_stop';
        console.log(`[TRADER] ⚡ QUICK STOP: ${watch.symbol} @ ${profit.toFixed(1)}%`);
        return;
      }

      // Trailing Stop
      if (profit >= this.config.TRAILING_STOP_ACTIVATION && !watch.trailingStopActive) {
        watch.trailingStopActive = true;
        console.log(`[TRADER] 🛡️ TRAILING: ${watch.symbol} @ +${profit.toFixed(1)}%`);
      }

      if (watch.trailingStopActive && profitFromMax <= this.config.TRAILING_STOP_PERCENT) {
        watch.phase = 'exiting';
        watch.exitReason = `trailing_${profitFromMax.toFixed(1)}%`;
        console.log(`[TRADER] 📉 TRAILING STOP: ${watch.symbol}`);
        return;
      }

      // Take Profit
      for (const tp of this.config.TAKE_PROFIT_TARGETS) {
        if (!watch.partialSellsDone.includes(tp.percent) && profit >= tp.percent) {
          watch.phase = 'exiting';
          watch.exitReason = `tp_${tp.percent}%_sell_${tp.sellPercent}%`;
          watch.partialSellsDone.push(tp.percent);
          console.log(`[TRADER] 💚 TP: ${watch.symbol} @ +${profit.toFixed(1)}% | Sell ${tp.sellPercent}%`);
          
          if (tp.sellPercent < 100) {
            setTimeout(() => {
              const w = this.watchlist.get(mint);
              if (w && w.phase === 'exiting') w.phase = 'holding';
            }, 5000);
          }
          return;
        }
      }

      // Max Hold Time
      if (holdTime >= this.config.MAX_HOLD_TIME_MIN) {
        watch.phase = 'exiting';
        watch.exitReason = 'max_hold';
        console.log(`[TRADER] ⏰ MAX HOLD: ${watch.symbol}`);
        return;
      }

      // Stagnant
      const timeSinceMove = (Date.now() - watch.lastMoveTime) / 60000;
      if (timeSinceMove >= this.config.STAGNANT_TIME_MIN && profit > 0) {
        watch.phase = 'exiting';
        watch.exitReason = 'stagnant';
        console.log(`[TRADER] 😴 STAGNANT: ${watch.symbol}`);
        return;
      }

      // Dump Detection
      if (this.detectDump(watch)) {
        watch.phase = 'exiting';
        watch.exitReason = 'dump';
        console.log(`[TRADER] 💥 DUMP: ${watch.symbol}`);
        return;
      }

      if (Math.abs(profit) > 5) {
        watch.lastMoveTime = Date.now();
      }
    }
  }

  rejectToken(mint, reason) {
    const watch = this.watchlist.get(mint);
    if (watch) {
      console.log(`[TRADER] 🚫 ${watch.symbol} - ${reason}`);
    }
    
    // Cerrar WebSocket watcher
    if (this.tradeWatchers.has(mint)) {
      this.tradeWatchers.get(mint).close();
      this.tradeWatchers.delete(mint);
    }
    
    this.watchlist.delete(mint);
    this.stats.tokensRejected++;
  }

  getVelocity(watch) {
    if (watch.firstSeenPrice === 0 || watch.currentPrice === 0) return 0;
    return ((watch.currentPrice - watch.firstSeenPrice) / watch.firstSeenPrice) * 100;
  }

  getProfitPercent(watch) {
    if (watch.entryPrice === 0) return 0;
    return ((watch.currentPrice - watch.entryPrice) / watch.entryPrice) * 100;
  }

  getProfitFromMax(watch) {
    if (watch.maxPrice === 0) return 0;
    return ((watch.currentPrice - watch.maxPrice) / watch.maxPrice) * 100;
  }

  getPriceChange(watch, reference = 'entry') {
    const refPrice = reference === 'first' ? watch.firstSeenPrice : watch.entryPrice;
    if (refPrice === 0) return 0;
    return ((watch.currentPrice - refPrice) / refPrice) * 100;
  }

  detectDump(watch) {
    if (watch.priceHistory.length < 3) return false;
    
    const cutoff = Date.now() - (this.config.DUMP_TIME_WINDOW * 1000);
    const recent = watch.priceHistory.filter(p => p.time >= cutoff);
    
    if (recent.length < 2) return false;
    
    const maxRecent = Math.max(...recent.map(p => p.price));
    const change = ((watch.currentPrice - maxRecent) / maxRecent) * 100;
    
    return change <= this.config.DUMP_DETECTION_PERCENT;
  }

  generateLinks(mint) {
    return {
      pumpfun: `https://pump.fun/${mint}`,
      dexscreener: `https://dexscreener.com/solana/${mint}`,
      rugcheck: `https://rugcheck.xyz/tokens/${mint}`,
      solscan: `https://solscan.io/token/${mint}`
    };
  }

  getStats() {
    const total = this.stats.wins + this.stats.losses;
    const winRate = total > 0 ? (this.stats.wins / total) * 100 : 0;
    const avgHold = total > 0 ? this.stats.totalHoldTime / total : 0;
    
    return {
      tokensAnalyzed: this.stats.tokensAnalyzed,
      tokensEntered: this.stats.tokensEntered,
      tokensRejected: this.stats.tokensRejected,
      wins: this.stats.wins,
      losses: this.stats.losses,
      winRate: winRate.toFixed(1),
      totalProfit: this.stats.totalProfit,
      avgHoldTime: avgHold,
      blockchainCalls: this.stats.blockchainCalls,
      currentlyWatching: this.watchlist.size
    };
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

// ============================================================================
// ESTADO GLOBAL
// ============================================================================

let telegramBot = null;
let ws = null;
let wallet = null;
let connection = null;
let smartTrader = null;

const positions = new Map();
const stats = {
  detected: 0,
  filtered: 0,
  bought: 0,
  sold: 0,
  wins: 0,
  losses: 0,
  totalProfitSOL: 0,
  totalProfitUSD: 0,
  bestTrade: 0,
  worstTrade: 0,
  errors: 0
};

// ============================================================================
// LOGGER
// ============================================================================

const log = {
  info: (msg) => console.log(`[INFO] ${msg}`),
  warn: (msg) => console.warn(`[WARN] ${msg}`),
  error: (msg) => console.error(`[ERROR] ${msg}`),
  trade: (msg) => console.log(`[TRADE] ${msg}`),
  debug: (msg) => {
    if (process.env.LOG_LEVEL === 'DEBUG') {
      console.log(`[DEBUG] ${msg}`);
    }
  }
};

// ============================================================================
// POSITION DATA
// ============================================================================

class PositionData {
  constructor({ mint, symbol, name, buyPrice, amountSOL }) {
    this.mint = mint;
    this.symbol = symbol || 'UNKNOWN';
    this.name = name || 'UNKNOWN';
    this.buyPrice = buyPrice;
    this.buyTime = Date.now();
    this.amountSOL = amountSOL;
    this.soldAmountSOL = 0;
    this.currentPrice = buyPrice;
    this.maxPrice = buyPrice;
    this.minPrice = buyPrice;
    this.status = 'holding';
    this.trailingStopActive = false;
    this.partialSells = [];
    this.remainingPercent = 100;
    this.txBuy = null;
    this.txSells = [];
    this.checksCount = 0;
  }
  
  get elapsedMinutes() {
    return (Date.now() - this.buyTime) / 60000;
  }
  
  get profitPercent() {
    if (this.buyPrice === 0) return 0;
    return ((this.currentPrice - this.buyPrice) / this.buyPrice) * 100;
  }
  
  get profitFromMax() {
    if (this.maxPrice === 0) return 0;
    return ((this.currentPrice - this.maxPrice) / this.maxPrice) * 100;
  }
  
  get realizedProfitSOL() {
    return this.soldAmountSOL - (this.amountSOL * (100 - this.remainingPercent) / 100);
  }
  
  get estimatedTotalProfitSOL() {
    const soldProfit = this.realizedProfitSOL;
    const remainingValue = (this.amountSOL * this.remainingPercent / 100) * (1 + this.profitPercent / 100);
    const remainingProfit = remainingValue - (this.amountSOL * this.remainingPercent / 100);
    return soldProfit + remainingProfit;
  }
}

// ============================================================================
// WALLET & SMART TRADER SETUP
// ============================================================================

async function setupWallet() {
  if (!CONFIG.WALLET_PRIVATE_KEY) {
    log.error('❌ WALLET_PRIVATE_KEY no configurado');
    return false;
  }
  
  try {
    const secretKey = bs58.decode(CONFIG.WALLET_PRIVATE_KEY);
    wallet = Keypair.fromSecretKey(secretKey);
    
    // Seleccionar RPC
    let rpcUrl;
    if (CONFIG.HELIUS_API_KEY && CONFIG.HELIUS_API_KEY.length > 20) {
      rpcUrl = `https://mainnet.helius-rpc.com/?api-key=${CONFIG.HELIUS_API_KEY}`;
      log.info('🚀 Usando Helius RPC');
    } else {
      const publicRPCs = [
        'https://api.mainnet-beta.solana.com',
        'https://solana-api.projectserum.com',
        'https://rpc.ankr.com/solana'
      ];
      rpcUrl = publicRPCs[0];
      log.warn('⚠️ Usando RPC público (puede tener límites)');
    }
    
    connection = new Connection(rpcUrl, {
      commitment: 'confirmed',
      confirmTransactionInitialTimeout: 60000
    });
    
    log.info('🔍 Verificando wallet...');
    
    // Intentar obtener balance con retry
    let balance = 0;
    let attempts = 0;
    
    while (attempts < 3) {
      try {
        balance = await connection.getBalance(wallet.publicKey);
        break;
      } catch (err) {
        attempts++;
        if (err.message.includes('429')) {
          log.warn(`⏳ Rate limit, esperando ${attempts * 2}s...`);
          await sleep(attempts * 2000);
        } else if (attempts === 3) {
          log.warn('⚠️ No se pudo verificar balance, continuando...');
        }
      }
    }
    
    const balanceSOL = balance / LAMPORTS_PER_SOL;
    
    log.info(`✅ Wallet: ${wallet.publicKey.toBase58()}`);
    
    if (balance > 0) {
      log.info(`💰 Balance: ${balanceSOL.toFixed(4)} SOL`);
      
      if (balanceSOL < CONFIG.TRADE_AMOUNT_SOL * 2) {
        log.warn(`⚠️ Balance bajo. Recomendado: ${(CONFIG.TRADE_AMOUNT_SOL * 2).toFixed(3)} SOL`);
      }
    }
    
    return true;
  } catch (error) {
    log.error(`❌ Error en wallet: ${error.message}`);
    return false;
  }
}

function initSmartTrader() {
  smartTrader = new HeliusSmartTrader(CONFIG);
  smartTrader.init(connection);
  
  log.info('✅ Smart Trader inicializado (Blockchain Direct)');
  log.info(`   📊 Entrada: +${CONFIG.EARLY_VELOCITY_MIN}% (${CONFIG.EARLY_TIME_WINDOW}s) → +${CONFIG.CONFIRMATION_VELOCITY}% confirma`);
  log.info(`   🛡️ Stops: Hard ${CONFIG.HARD_STOP_LOSS_PERCENT}% | Quick ${CONFIG.QUICK_STOP_PERCENT}% | Trailing ${CONFIG.TRAILING_STOP_PERCENT}%`);
  
  return true;
}

// ============================================================================
// TRADING FUNCTIONS (PumpPortal)
// ============================================================================

async function executeBuy(mint, amountSOL) {
  if (CONFIG.DRY_RUN) {
    log.trade(`[DRY RUN] Comprando ${amountSOL} SOL de ${mint.slice(0, 8)}`);
    await sleep(1000);
    return { success: true, signature: `dry-run-buy-${Date.now()}`, dryRun: true };
  }
  
  try {
    const response = await axios.post(CONFIG.PUMPPORTAL_API, {
      publicKey: wallet.publicKey.toBase58(),
      action: 'buy',
      mint: mint,
      denominatedInSol: 'true',
      amount: amountSOL,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    }, {
      timeout: 15000,
      responseType: 'arraybuffer'
    });
    
    if (response.status !== 200) {
      throw new Error(`API returned ${response.status}`);
    }
    
    const txData = new Uint8Array(response.data);
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([wallet]);
    
    const signature = await connection.sendTransaction(tx, {
      skipPreflight: false,
      maxRetries: 3,
      preflightCommitment: 'confirmed'
    });
    
    await connection.confirmTransaction(signature, 'confirmed');
    
    log.trade(`✅ COMPRA: ${mint.slice(0, 8)} | ${amountSOL} SOL | Tx: ${signature.slice(0, 16)}...`);
    return { success: true, signature };
    
  } catch (error) {
    log.error(`❌ Compra falló: ${error.message}`);
    return { success: false, error: error.message };
  }
}

async function executeSell(mint, percentage) {
  if (CONFIG.DRY_RUN) {
    log.trade(`[DRY RUN] Vendiendo ${percentage}% de ${mint.slice(0, 8)}`);
    await sleep(1000);
    return { success: true, signature: `dry-run-sell-${Date.now()}`, dryRun: true };
  }
  
  try {
    const amount = percentage === 100 ? '100%' : `${percentage}%`;
    
    const response = await axios.post(CONFIG.PUMPPORTAL_API, {
      publicKey: wallet.publicKey.toBase58(),
      action: 'sell',
      mint: mint,
      denominatedInSol: 'false',
      amount: amount,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    }, {
      timeout: 15000,
      responseType: 'arraybuffer'
    });
    
    if (response.status !== 200) {
      throw new Error(`API returned ${response.status}`);
    }
    
    const txData = new Uint8Array(response.data);
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([wallet]);
    
    const signature = await connection.sendTransaction(tx, {
      skipPreflight: false,
      maxRetries: 3,
      preflightCommitment: 'confirmed'
    });
    
    await connection.confirmTransaction(signature, 'confirmed');
    
    log.trade(`✅ VENTA: ${mint.slice(0, 8)} | ${percentage}% | Tx: ${signature.slice(0, 16)}...`);
    return { success: true, signature };
    
  } catch (error) {
    log.error(`❌ Venta falló: ${error.message}`);
    return { success: false, error: error.message };
  }
}

// ============================================================================
// TELEGRAM
// ============================================================================

async function sendTelegram(message, options = {}) {
  if (!telegramBot || !CONFIG.TELEGRAM_CHAT_ID) return;
  
  try {
    await telegramBot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, {
      parse_mode: 'Markdown',
      disable_web_page_preview: true,
      ...options
    });
  } catch (error) {
    log.error(`Telegram error: ${error.message}`);
  }
}

// ============================================================================
// HANDLE NEW TOKEN
// ============================================================================

async function handleNewToken(data) {
  try {
    stats.detected++;
    
    const mint = data.mint;
    const symbol = data.symbol || 'UNKNOWN';
    const name = data.name || symbol;
    const uri = data.uri || '';
    
    if (!mint || positions.has(mint)) return;
    
    if (!CONFIG.TRADING_ENABLED || !wallet) {
      return;
    }
    
    if (positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
      stats.filtered++;
      return;
    }
    
    log.info(`🆕 Token: ${symbol} (${mint.slice(0, 8)})`);
    
    if (!smartTrader) {
      log.warn('⚠️ Smart Trader no inicializado');
      return;
    }
    
    const result = await smartTrader.analyzeToken({
      mint,
      symbol,
      name,
      uri
    });
    
    if (!result.shouldWatch) {
      stats.filtered++;
      return;
    }
    
    monitorSmartTraderSignals(mint, symbol, name);
    
  } catch (error) {
    log.error(`Error con token: ${error.message}`);
    stats.errors++;
  }
}

// ============================================================================
// MONITOR SMART TRADER SIGNALS
// ============================================================================

async function monitorSmartTraderSignals(mint, symbol, name) {
  const watch = smartTrader.watchlist.get(mint);
  if (!watch) return;
  
  while (smartTrader.watchlist.has(mint)) {
    await sleep(1000);
    
    const currentWatch = smartTrader.watchlist.get(mint);
    if (!currentWatch) break;
    
    // COMPRA
    if (currentWatch.phase === 'entering' && !positions.has(mint)) {
      const buyPrice = currentWatch.entryPrice;
      const priceChange = smartTrader.getPriceChange(currentWatch, 'first');
      const links = smartTrader.generateLinks(mint);
      
      await sendTelegram(`
🧠 *SMART TRADER - COMPRA*

*Token:* ${name} (${symbol})
\`${mint}\`

📊 *Blockchain Data:*
• Cambio: +${priceChange.toFixed(1)}% (${((Date.now() - currentWatch.firstSeenTime) / 1000).toFixed(0)}s)
• Liquidez: ${currentWatch.liquiditySOL.toFixed(2)} SOL
• Buys: ${currentWatch.buyCount} | Compradores: ${currentWatch.uniqueBuyers.size}
• Holders: ${currentWatch.holders} | Top: ${currentWatch.topHolderPercent.toFixed(1)}%

💰 Comprando ${CONFIG.TRADE_AMOUNT_SOL} SOL...

[Pump](${links.pumpfun}) | [Dex](${links.dexscreener}) | [Rug](${links.rugcheck})
      `.trim());
      
      log.trade(`🔥 BUY: ${symbol} @ ${buyPrice.toFixed(8)} | +${priceChange.toFixed(1)}%`);
      
      const buyResult = await executeBuy(mint, CONFIG.TRADE_AMOUNT_SOL);
      
      if (!buyResult.success) {
        log.error(`❌ Compra falló: ${buyResult.error}`);
        stats.errors++;
        await sendTelegram(`❌ *ERROR*\n\n${symbol}: ${buyResult.error}`);
        smartTrader.rejectToken(mint, 'buy_failed');
        return;
      }
      
      const position = new PositionData({
        mint,
        symbol,
        name,
        buyPrice: buyPrice,
        amountSOL: CONFIG.TRADE_AMOUNT_SOL
      });
      
      position.txBuy = buyResult.signature;
      positions.set(mint, position);
      stats.bought++;
      
      currentWatch.phase = 'holding';
      
      const dryTag = buyResult.dryRun ? '[DRY RUN] ' : '';
      log.trade(`${dryTag}✅ POSICIÓN: ${symbol} @ ${buyPrice.toFixed(8)}`);
      
      await sendTelegram(`
${dryTag}✅ *COMPRA EJECUTADA*

*${name}* (${symbol})
*Precio:* ${buyPrice.toFixed(8)}
*Invertido:* ${CONFIG.TRADE_AMOUNT_SOL} SOL

📈 +${priceChange.toFixed(1)}% detectado

${buyResult.dryRun ? '' : `[Tx](https://solscan.io/tx/${buyResult.signature})`}
      `.trim());
      
      monitorSmartPosition(mint).catch(err => {
        log.error(`Error monitor: ${err.message}`);
      });
    }
    
    // VENTA
    if (currentWatch.phase === 'exiting' && positions.has(mint)) {
      const position = positions.get(mint);
      const sellPercent = currentWatch.exitReason?.includes('tp_') 
        ? parseInt(currentWatch.exitReason.match(/sell_(\d+)/)?.[1] || 100)
        : 100;
      
      log.trade(`🚪 SELL: ${symbol} | ${currentWatch.exitReason} | ${sellPercent}%`);
      
      await closePosition(mint, sellPercent, currentWatch.exitReason);
      
      if (sellPercent === 100 || position.remainingPercent === 0) {
        smartTrader.watchlist.delete(mint);
      }
    }
  }
}

// ============================================================================
// MONITOR SMART POSITION
// ============================================================================

async function monitorSmartPosition(mint) {
  const position = positions.get(mint);
  const watch = smartTrader.watchlist.get(mint);
  
  if (!position || !watch) return;
  
  log.info(`👀 Monitoreando: ${position.symbol}...`);
  
  while (positions.has(mint) && smartTrader.watchlist.has(mint) && watch.phase === 'holding') {
    
    const currentWatch = smartTrader.watchlist.get(mint);
    if (!currentWatch) break;
    
    position.currentPrice = currentWatch.currentPrice;
    position.maxPrice = Math.max(position.maxPrice, currentWatch.currentPrice);
    position.minPrice = Math.min(position.minPrice || currentWatch.currentPrice, currentWatch.currentPrice);
    
    if (currentWatch.checksCount % 5 === 0) {
      const profit = position.profitPercent;
      const holdTime = position.elapsedMinutes;
      
      log.info(
        `📊 ${position.symbol}: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}% | ` +
        `${currentWatch.currentPrice.toFixed(8)} | ` +
        `${holdTime.toFixed(1)}min | ` +
        `Trail: ${currentWatch.trailingStopActive ? '🛡️' : '❌'}`
      );
    }
    
    await sleep(3000);
  }
}

// ============================================================================
// CLOSE POSITION
// ============================================================================

async function closePosition(mint, percentage, reason) {
  const position = positions.get(mint);
  if (!position || percentage === 0) return;
  
  log.trade(`Cerrando: ${position.symbol} (${percentage}%) - ${reason}`);
  
  const sellResult = await executeSell(mint, percentage);
  
  if (!sellResult.success) {
    log.error(`❌ Venta falló: ${position.symbol}`);
    stats.errors++;
    return;
  }
  
  position.txSells.push(sellResult.signature);
  
  const profit = position.profitPercent;
  const soldValue = (position.amountSOL * percentage / 100) * (1 + profit / 100);
  position.soldAmountSOL += soldValue;
  position.remainingPercent -= percentage;
  
  if (position.remainingPercent === 0) {
    await finalizePosition(mint, reason);
  }
}

async function finalizePosition(mint, reason) {
  const position = positions.get(mint);
  if (!position) return;
  
  position.status = 'sold';
  
  const totalProfit = position.estimatedTotalProfitSOL;
  const profitPercent = position.profitPercent;
  const profitUSD = totalProfit * CONFIG.SOL_PRICE_USD;
  
  stats.sold++;
  stats.totalProfitSOL += totalProfit;
  stats.totalProfitUSD += profitUSD;
  
  if (totalProfit > 0) {
    stats.wins++;
    if (smartTrader) smartTrader.stats.wins++;
    if (profitPercent > stats.bestTrade) stats.bestTrade = profitPercent;
  } else {
    stats.losses++;
    if (smartTrader) smartTrader.stats.losses++;
    if (profitPercent < stats.worstTrade) stats.worstTrade = profitPercent;
  }
  
  if (smartTrader) {
    smartTrader.stats.totalProfit += totalProfit;
    smartTrader.stats.totalHoldTime += position.elapsedMinutes;
  }
  
  const emoji = totalProfit > 0 ? '💚' : '❌';
  const dryTag = position.txSells.some(tx => tx && tx.includes('dry-run')) ? '[DRY RUN] ' : '';
  
  log.trade(`${dryTag}${emoji} CERRADO: ${position.symbol} | ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}% (${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL)`);
  
  await sendTelegram(`
${dryTag}${emoji} *CERRADO*

*${position.name}* (${position.symbol})
*Profit:* ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}%
*SOL:* ${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL
*Tiempo:* ${position.elapsedMinutes.toFixed(1)} min
*Razón:* ${reason}

*Balance Hoy:*
${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL | W/L: ${stats.wins}/${stats.losses}
  `.trim());
  
  setTimeout(() => positions.delete(mint), 60000);
}

// ============================================================================
// WEBSOCKET
// ============================================================================

function connectWebSocket() {
  log.info('🔌 Conectando a PumpPortal...');
  
  ws = new WebSocket(CONFIG.PUMPPORTAL_WSS);
  
  ws.on('open', () => {
    log.info('✅ WebSocket conectado');
    
    ws.send(JSON.stringify({
      method: 'subscribeNewToken'
    }));
    
    log.info('✅ Suscrito a nuevos tokens');
    
    if (CONFIG.TRADING_ENABLED && wallet) {
      const mode = CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE';
      log.warn(`🤖 MODO: ${mode}`);
    }
  });
  
  ws.on('message', (data) => {
    try {
      const parsed = JSON.parse(data);
      handleNewToken(parsed);
    } catch (error) {
      // Ignorar errores de parsing
    }
  });
  
  ws.on('error', (error) => {
    log.error(`WebSocket error: ${error.message}`);
  });
  
  ws.on('close', () => {
    log.warn('⚠️ WebSocket cerrado, reconectando...');
    setTimeout(connectWebSocket, 5000);
  });
}

// ============================================================================
// TELEGRAM BOT
// ============================================================================

function setupTelegramBot() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    log.warn('⚠️ Telegram no configurado');
    return;
  }
  
  telegramBot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
  
  telegramBot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    telegramBot.sendMessage(chatId, `
🤖 *Pump.fun Bot - Blockchain Direct*

*Estado:* ${CONFIG.TRADING_ENABLED ? '✅' : '❌'}
*Modo:* ${CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE'}

*Comandos:*
/status - Estado
/stats - Estadísticas
/positions - Posiciones
/balance - Balance
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/status/, (msg) => {
    const chatId = msg.chat.id;
    const wsStatus = ws && ws.readyState === WebSocket.OPEN ? '✅' : '❌';
    
    telegramBot.sendMessage(chatId, `
📊 *ESTADO*

*Conexión:* ${wsStatus}
*Posiciones:* ${positions.size}/${CONFIG.MAX_CONCURRENT_POSITIONS}
*Detectados:* ${stats.detected}
*Comprados:* ${stats.bought}
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/stats/, (msg) => {
    const chatId = msg.chat.id;
    const winRate = (stats.wins + stats.losses) > 0 
      ? ((stats.wins / (stats.wins + stats.losses)) * 100).toFixed(1) 
      : 0;
    
    telegramBot.sendMessage(chatId, `
📈 *STATS*

*Profit:* ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
*Trades:* ${stats.wins}W / ${stats.losses}L (${winRate}%)
*Mejor:* +${stats.bestTrade.toFixed(1)}%
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/positions/, (msg) => {
    const chatId = msg.chat.id;
    
    if (positions.size === 0) {
      telegramBot.sendMessage(chatId, '🔭 Sin posiciones');
      return;
    }
    
    let msg_text = '📊 *POSICIONES*\n\n';
    for (const [mint, pos] of positions) {
      const profit = pos.profitPercent;
      msg_text += `${profit > 0 ? '💚' : '❌'} ${pos.symbol}: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}%\n`;
    }
    
    telegramBot.sendMessage(chatId, msg_text, { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/balance/, async (msg) => {
    const chatId = msg.chat.id;
    
    if (!wallet) {
      telegramBot.sendMessage(chatId, '❌ Wallet no configurado');
      return;
    }
    
    try {
      const balance = await connection.getBalance(wallet.publicKey);
      const sol = balance / LAMPORTS_PER_SOL;
      
      telegramBot.sendMessage(chatId, `
💰 *BALANCE*

${sol.toFixed(4)} SOL

*Profit Sesión:*
${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
      `.trim(), { parse_mode: 'Markdown' });
    } catch (error) {
      telegramBot.sendMessage(chatId, `❌ ${error.message}`);
    }
  });
  
  log.info('✅ Telegram bot listo');
}

// ============================================================================
// HEALTH SERVER
// ============================================================================

function startHealthServer() {
  const http = require('http');
  
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'healthy',
        positions: positions.size,
        stats: stats
      }));
    } else {
      res.writeHead(200);
      res.end('Bot Running ✅');
    }
  });
  
  server.listen(CONFIG.HEALTH_PORT, () => {
    log.info(`✅ Health: http://0.0.0.0:${CONFIG.HEALTH_PORT}`);
  });
}

// ============================================================================
// UTILS
// ============================================================================

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ============================================================================
// MAIN
// ============================================================================

async function main() {
  console.log('\n\n');
  log.info('═══════════════════════════════════════════════════════════════');
  log.info('🚀 PUMP.FUN BOT - BLOCKCHAIN DIRECT');
  log.info('═══════════════════════════════════════════════════════════════');
  
  if (CONFIG.TRADING_ENABLED) {
    if (!CONFIG.WALLET_PRIVATE_KEY) {
      log.error('❌ WALLET_PRIVATE_KEY requerido');
      process.exit(1);
    }
    
    log.info('💼 Configurando wallet...');
    const walletOk = await setupWallet();
    
    if (!walletOk) {
      log.error('❌ Error en wallet');
      process.exit(1);
    }
    
    log.info('🧠 Inicializando Smart Trader...');
    initSmartTrader();
  }
  
  log.info('');
  if (CONFIG.DRY_RUN) {
    log.warn('═══════════════════════════════════════════════════════════════');
    log.warn('🟡 MODO DRY RUN - SIMULACIÓN');
    log.warn('═══════════════════════════════════════════════════════════════');
  } else if (CONFIG.TRADING_ENABLED) {
    log.warn('═══════════════════════════════════════════════════════════════');
    log.warn('🔴 MODO LIVE - DINERO REAL');
    log.warn('═══════════════════════════════════════════════════════════════');
  }
  
  log.info('');
  setupTelegramBot();
  startHealthServer();
  connectWebSocket();
  
  log.info('');
  log.info('✅ Bot iniciado');
  log.info('═══════════════════════════════════════════════════════════════');
  
  if (telegramBot && CONFIG.TELEGRAM_CHAT_ID) {
    await sendTelegram(`
🤖 *Bot Iniciado*

*Modo:* ${CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE'}
*Datos:* Blockchain Direct 🚀
*Monto:* ${CONFIG.TRADE_AMOUNT_SOL} SOL/trade

Bot monitoreando pump.fun 👀
    `.trim());
  }
}

process.on('SIGTERM', () => {
  log.info('🛑 Cerrando...');
  if (ws) ws.close();
  process.exit(0);
});

process.on('SIGINT', () => {
  log.info('🛑 Cerrando...');
  if (ws) ws.close();
  process.exit(0);
});

main().catch(error => {
  log.error(`❌ Error fatal: ${error.message}`);
  process.exit(1);
});
