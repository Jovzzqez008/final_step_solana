// trading-bot-hybrid.js - Pump.fun Trading Bot con Helius Smart Trader
// 🚀 Bot completo de trading con detección inteligente y stop-loss híbrido
// 💰 Optimizado para operar automáticamente en pump.fun

const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, Keypair, VersionedTransaction, PublicKey } = require('@solana/web3.js');
const bs58 = require('bs58');

// ============================================================================
// CONFIGURACIÓN
// ============================================================================

const CONFIG = {
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || '',
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY || '',
  HELIUS_API_KEY: process.env.HELIUS_API_KEY || '',
  SOLANA_RPC: process.env.SOLANA_RPC || 'https://api.mainnet-beta.solana.com',
  PUMPPORTAL_WSS: 'wss://pumpportal.fun/api/data',
  PUMPPORTAL_API: 'https://pumpportal.fun/api/trade-local',
  
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
  
  EARLY_VELOCITY_MIN: parseFloat(process.env.EARLY_VELOCITY_MIN || '15'),
  EARLY_TIME_WINDOW: parseFloat(process.env.EARLY_TIME_WINDOW || '20'),
  CONFIRMATION_VELOCITY: parseFloat(process.env.CONFIRMATION_VELOCITY || '35'),
  CONFIRMATION_TIME: parseFloat(process.env.CONFIRMATION_TIME || '45'),
  MIN_VOLUME_SOL: parseFloat(process.env.MIN_VOLUME_SOL || '0.4'),
  MIN_TX_COUNT: parseInt(process.env.MIN_TX_COUNT || '8'),
  MIN_UNIQUE_BUYERS: parseInt(process.env.MIN_UNIQUE_BUYERS || '6'),
  MIN_HOLDERS: parseInt(process.env.MIN_HOLDERS || '12'),
  MAX_TOP_HOLDER_PERCENT: parseFloat(process.env.MAX_TOP_HOLDER_PERCENT || '35'),
  
  MIN_LIQUIDITY_USD: parseFloat(process.env.MIN_LIQUIDITY_USD || '400'),
  MAX_CONCURRENT_POSITIONS: parseInt(process.env.MAX_CONCURRENT_POSITIONS || '3'),
  MIN_PRICE_USD: 0.00000001,
  
  DUMP_DETECTION_PERCENT: parseFloat(process.env.DUMP_DETECTION_PERCENT || '-15'),
  DUMP_TIME_WINDOW: parseFloat(process.env.DUMP_TIME_WINDOW || '30'),
  
  TRADING_ENABLED: process.env.TRADING_ENABLED !== 'false',
  DRY_RUN: process.env.DRY_RUN !== 'false',
  
  SLIPPAGE: parseFloat(process.env.SLIPPAGE || '30'),
  PRIORITY_FEE: parseFloat(process.env.PRIORITY_FEE || '0.0005'),
  
  PRICE_CHECK_INTERVAL_MS: parseInt(process.env.CHECK_INTERVAL_MS || '2500'),
  
  HEALTH_PORT: process.env.PORT || 8080
};

// ============================================================================
// HELIUS SMART TRADER CLASS
// ============================================================================

class HeliusSmartTrader {
  constructor(config) {
    this.config = config;
    this.connection = null;
    this.watchlist = new Map();
    this.stats = {
      tokensAnalyzed: 0,
      tokensEntered: 0,
      tokensRejected: 0,
      wins: 0,
      losses: 0,
      totalProfit: 0,
      totalHoldTime: 0,
      heliusCalls: 0
    };
  }

  init(connection) {
    this.connection = connection;
  }

  async analyzeToken({ mint, symbol, name, initialPrice, marketCap }) {
    this.stats.tokensAnalyzed++;
    
    if (this.watchlist.has(mint)) {
      return { shouldWatch: false, reason: 'already_watching' };
    }

    const watch = {
      mint,
      symbol: symbol || 'UNKNOWN',
      name: name || 'UNKNOWN',
      phase: 'watching',
      firstSeenTime: Date.now(),
      firstSeenPrice: initialPrice || 0,
      currentPrice: initialPrice || 0,
      maxPrice: initialPrice || 0,
      minPrice: initialPrice || 0,
      volumeSOL: 0,
      txCount: 0,
      uniqueBuyers: 0,
      uniqueSellers: 0,
      holders: 0,
      topHolderPercent: 0,
      liquidityUSD: marketCap || 0,
      priceHistory: [],
      checksCount: 0,
      lastCheckTime: Date.now(),
      entryPrice: 0,
      exitReason: null,
      trailingStopActive: false,
      lastMoveTime: Date.now(),
      partialSellsDone: []
    };

    this.watchlist.set(mint, watch);
    this.monitorToken(mint);
    
    return { shouldWatch: true };
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
      
      const [priceData, holdersData, txData] = await Promise.all([
        this.getTokenPrice(mint).catch(() => null),
        this.getHoldersInfo(mint).catch(() => null),
        this.getRecentTransactions(mint).catch(() => null)
      ]);

      if (priceData && priceData.priceUSD > 0) {
        watch.currentPrice = priceData.priceUSD;
        watch.maxPrice = Math.max(watch.maxPrice, priceData.priceUSD);
        watch.minPrice = watch.minPrice === 0 ? priceData.priceUSD : Math.min(watch.minPrice, priceData.priceUSD);
        watch.volumeSOL = priceData.volumeSOL || watch.volumeSOL;
        watch.liquidityUSD = priceData.liquidityUSD || watch.liquidityUSD;

        if (watch.firstSeenPrice === 0) {
          watch.firstSeenPrice = priceData.priceUSD;
        }

        watch.priceHistory.push({
          time: Date.now(),
          price: priceData.priceUSD
        });

        const cutoff = Date.now() - 60000;
        watch.priceHistory = watch.priceHistory.filter(p => p.time > cutoff);
      }

      if (holdersData) {
        watch.holders = holdersData.holderCount || watch.holders;
        watch.topHolderPercent = holdersData.topHolderPercent || watch.topHolderPercent;
      }

      if (txData) {
        watch.txCount = txData.txCount || watch.txCount;
        watch.uniqueBuyers = txData.uniqueBuyers || watch.uniqueBuyers;
        watch.uniqueSellers = txData.uniqueSellers || watch.uniqueSellers;
      }

      watch.lastCheckTime = Date.now();

      if (watch.checksCount % 8 === 0 && watch.phase === 'watching') {
        const elapsed = (Date.now() - watch.firstSeenTime) / 1000;
        const velocity = this.getVelocity(watch);
        console.log(`[HELIUS] 📊 ${watch.symbol}: ${velocity >= 0 ? '+' : ''}${velocity.toFixed(1)}% | Vol: ${watch.volumeSOL.toFixed(1)} | Holders: ${watch.holders} | Tx: ${watch.txCount} | ${elapsed.toFixed(0)}s`);
      }

    } catch (error) {
      console.log(`[DEBUG] Error updating watch ${mint.slice(0, 8)}: ${error.message}`);
    }
  }

  async checkSignals(mint) {
    const watch = this.watchlist.get(mint);
    if (!watch) return;

    const elapsed = (Date.now() - watch.firstSeenTime) / 1000;

    if (watch.phase === 'watching') {
      if (elapsed > this.config.MAX_WATCH_TIME_SEC) {
        this.rejectToken(mint, 'timeout');
        return;
      }

      const velocity = this.getVelocity(watch);
      const hasEarlySignal = velocity >= this.config.EARLY_VELOCITY_MIN && elapsed <= this.config.EARLY_TIME_WINDOW;
      const hasConfirmation = velocity >= this.config.CONFIRMATION_VELOCITY && elapsed <= this.config.CONFIRMATION_TIME;

      if (!hasEarlySignal && !hasConfirmation) return;

      if (watch.volumeSOL < this.config.MIN_VOLUME_SOL) {
        this.rejectToken(mint, `vol_bajo_${watch.volumeSOL.toFixed(2)}`);
        return;
      }

      if (watch.txCount < this.config.MIN_TX_COUNT) {
        this.rejectToken(mint, `tx_bajo_${watch.txCount}`);
        return;
      }

      if (watch.uniqueBuyers < this.config.MIN_UNIQUE_BUYERS) {
        this.rejectToken(mint, `buyers_bajo_${watch.uniqueBuyers}`);
        return;
      }

      if (watch.holders < this.config.MIN_HOLDERS) {
        this.rejectToken(mint, `holders_bajo_${watch.holders}`);
        return;
      }

      if (watch.topHolderPercent > this.config.MAX_TOP_HOLDER_PERCENT) {
        this.rejectToken(mint, `concentracion_${watch.topHolderPercent.toFixed(0)}`);
        return;
      }

      if (watch.liquidityUSD < this.config.MIN_LIQUIDITY_USD) {
        this.rejectToken(mint, `liq_baja_${watch.liquidityUSD.toFixed(0)}`);
        return;
      }

      watch.phase = 'entering';
      watch.entryPrice = watch.currentPrice;
      this.stats.tokensEntered++;

      console.log(`[HELIUS] 🚀 SEÑAL DE ENTRADA: ${watch.symbol}`);
      console.log(`         Velocidad: +${velocity.toFixed(1)}% en ${elapsed.toFixed(0)}s | Vol: ${watch.volumeSOL.toFixed(1)} SOL`);
    }
    else if (watch.phase === 'holding') {
      const holdTime = (Date.now() - watch.firstSeenTime) / 60000;
      const profit = this.getProfitPercent(watch);
      const profitFromMax = this.getProfitFromMax(watch);

      if (profit <= this.config.HARD_STOP_LOSS_PERCENT) {
        watch.phase = 'exiting';
        watch.exitReason = 'hard_stop_loss';
        console.log(`[HELIUS] 🛑 HARD STOP: ${watch.symbol} @ ${profit.toFixed(1)}%`);
        return;
      }

      if (holdTime < (this.config.QUICK_STOP_TIME_SEC / 60) && profit <= this.config.QUICK_STOP_PERCENT) {
        watch.phase = 'exiting';
        watch.exitReason = 'quick_stop';
        console.log(`[HELIUS] ⚡ QUICK STOP: ${watch.symbol} @ ${profit.toFixed(1)}%`);
        return;
      }

      if (profit >= this.config.TRAILING_STOP_ACTIVATION && !watch.trailingStopActive) {
        watch.trailingStopActive = true;
        console.log(`[HELIUS] 🛡️ TRAILING ACTIVADO: ${watch.symbol} @ +${profit.toFixed(1)}%`);
      }

      if (watch.trailingStopActive && profitFromMax <= this.config.TRAILING_STOP_PERCENT) {
        watch.phase = 'exiting';
        watch.exitReason = `trailing_stop_${profitFromMax.toFixed(1)}%_from_max`;
        console.log(`[HELIUS] 📉 TRAILING STOP: ${watch.symbol} cayó ${profitFromMax.toFixed(1)}% desde máximo`);
        return;
      }

      for (const tp of this.config.TAKE_PROFIT_TARGETS) {
        const alreadyDone = watch.partialSellsDone.includes(tp.percent);
        
        if (!alreadyDone && profit >= tp.percent) {
          watch.phase = 'exiting';
          watch.exitReason = `take_profit_${tp.percent}%_sell_${tp.sellPercent}%`;
          watch.partialSellsDone.push(tp.percent);
          console.log(`[HELIUS] 💚 TAKE PROFIT: ${watch.symbol} @ +${profit.toFixed(1)}% | Vender ${tp.sellPercent}%`);
          
          if (tp.sellPercent < 100) {
            setTimeout(() => {
              const w = this.watchlist.get(mint);
              if (w && w.phase === 'exiting') {
                w.phase = 'holding';
              }
            }, 5000);
          }
          return;
        }
      }

      if (holdTime >= this.config.MAX_HOLD_TIME_MIN) {
        watch.phase = 'exiting';
        watch.exitReason = 'max_hold_time';
        console.log(`[HELIUS] ⏰ MAX HOLD: ${watch.symbol} @ ${holdTime.toFixed(1)}min`);
        return;
      }

      const timeSinceLastMove = (Date.now() - watch.lastMoveTime) / 60000;
      if (timeSinceLastMove >= this.config.STAGNANT_TIME_MIN && profit > 0) {
        watch.phase = 'exiting';
        watch.exitReason = 'stagnant';
        console.log(`[HELIUS] 😴 STAGNANT: ${watch.symbol} @ ${timeSinceLastMove.toFixed(1)}min sin movimiento`);
        return;
      }

      const recentDump = this.detectDump(watch);
      if (recentDump) {
        watch.phase = 'exiting';
        watch.exitReason = 'dump_detected';
        console.log(`[HELIUS] 💥 DUMP DETECTADO: ${watch.symbol}`);
        return;
      }

      if (Math.abs(profit) > 5) {
        watch.lastMoveTime = Date.now();
      }
    }
  }

  async getTokenPrice(mint) {
    try {
      this.stats.heliusCalls++;
      
      const response = await axios.get(
        `https://api.dexscreener.com/latest/dex/tokens/${mint}`,
        { timeout: 3000 }
      );

      if (response.data.pairs && response.data.pairs.length > 0) {
        const pair = response.data.pairs[0];
        return {
          priceUSD: parseFloat(pair.priceUsd || 0),
          volumeSOL: parseFloat(pair.volume?.h24 || 0) / 150,
          liquidityUSD: parseFloat(pair.liquidity?.usd || 0)
        };
      }
      return null;
    } catch (error) {
      return null;
    }
  }

  async getHoldersInfo(mint) {
    try {
      this.stats.heliusCalls++;
      
      const response = await axios.post(
        `https://mainnet.helius-rpc.com/?api-key=${this.config.HELIUS_API_KEY}`,
        {
          jsonrpc: '2.0',
          id: 'holders-' + Date.now(),
          method: 'getTokenAccounts',
          params: { mint: mint, limit: 100 }
        },
        { timeout: 3000 }
      );

      if (response.data.result?.token_accounts) {
        const accounts = response.data.result.token_accounts;
        const holderCount = accounts.length;
        
        let maxBalance = 0;
        let totalSupply = 0;
        
        for (const account of accounts) {
          const balance = parseFloat(account.amount || 0);
          totalSupply += balance;
          if (balance > maxBalance) maxBalance = balance;
        }
        
        const topHolderPercent = totalSupply > 0 ? (maxBalance / totalSupply) * 100 : 0;
        
        return { holderCount, topHolderPercent };
      }
      return null;
    } catch (error) {
      return null;
    }
  }

  async getRecentTransactions(mint) {
    try {
      this.stats.heliusCalls++;
      
      const response = await axios.get(
        `https://api.helius.xyz/v0/addresses/${mint}/transactions?api-key=${this.config.HELIUS_API_KEY}&limit=50`,
        { timeout: 3000 }
      );

      if (response.data && Array.isArray(response.data)) {
        const txs = response.data;
        const buyers = new Set();
        const sellers = new Set();

        for (const tx of txs) {
          if (tx.type === 'SWAP' || tx.type === 'TRANSFER') {
            if (tx.feePayer) sellers.add(tx.feePayer);
            if (tx.accountData?.[0]?.account) buyers.add(tx.accountData[0].account);
          }
        }

        return {
          txCount: txs.length,
          uniqueBuyers: buyers.size,
          uniqueSellers: sellers.size
        };
      }
      return null;
    } catch (error) {
      return null;
    }
  }

  rejectToken(mint, reason) {
    const watch = this.watchlist.get(mint);
    if (watch) {
      console.log(`[HELIUS] 🚫 ${watch.symbol} - ${reason}`);
    }
    this.watchlist.delete(mint);
    this.stats.tokensRejected++;
  }

  getVelocity(watch) {
    if (watch.firstSeenPrice === 0) return 0;
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
    if (watch.priceHistory.length < 2) return false;
    
    const cutoff = Date.now() - (this.config.DUMP_TIME_WINDOW * 1000);
    const recentPrices = watch.priceHistory.filter(p => p.time >= cutoff);
    
    if (recentPrices.length < 2) return false;
    
    const maxRecent = Math.max(...recentPrices.map(p => p.price));
    const change = ((watch.currentPrice - maxRecent) / maxRecent) * 100;
    
    return change <= this.config.DUMP_DETECTION_PERCENT;
  }

  generateLinks(mint) {
    return {
      pumpfun: `https://pump.fun/${mint}`,
      dexscreener: `https://dexscreener.com/solana/${mint}`,
      rugcheck: `https://rugcheck.xyz/tokens/${mint}`
    };
  }

  getStats() {
    const totalTrades = this.stats.wins + this.stats.losses;
    const winRate = totalTrades > 0 ? (this.stats.wins / totalTrades) * 100 : 0;
    const avgHoldTime = totalTrades > 0 ? this.stats.totalHoldTime / totalTrades : 0;
    
    return {
      tokensAnalyzed: this.stats.tokensAnalyzed,
      tokensEntered: this.stats.tokensEntered,
      tokensRejected: this.stats.tokensRejected,
      wins: this.stats.wins,
      losses: this.stats.losses,
      winRate: winRate.toFixed(1),
      totalProfit: this.stats.totalProfit,
      avgHoldTime: avgHoldTime,
      heliusCalls: this.stats.heliusCalls,
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
  info: (msg) => console.log(`[INFO] ${new Date().toISOString()} ${msg}`),
  warn: (msg) => console.warn(`[WARN] ${new Date().toISOString()} ${msg}`),
  error: (msg) => console.error(`[ERROR] ${new Date().toISOString()} ${msg}`),
  trade: (msg) => console.log(`[TRADE] ${new Date().toISOString()} ${msg}`),
  debug: (msg) => {
    if (process.env.LOG_LEVEL === 'DEBUG') {
      console.log(`[DEBUG] ${new Date().toISOString()} ${msg}`);
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
    log.error('❌ WALLET_PRIVATE_KEY not configured');
    return false;
  }
  
  try {
    const secretKey = bs58.decode(CONFIG.WALLET_PRIVATE_KEY);
    wallet = Keypair.fromSecretKey(secretKey);
    
    const rpcUrl = CONFIG.HELIUS_API_KEY 
      ? `https://mainnet.helius-rpc.com/?api-key=${CONFIG.HELIUS_API_KEY}`
      : CONFIG.SOLANA_RPC;
    
    connection = new Connection(rpcUrl, 'confirmed');
    
    const balance = await connection.getBalance(wallet.publicKey);
    const balanceSOL = balance / 1e9;
    
    log.info(`✅ Wallet: ${wallet.publicKey.toBase58()}`);
    log.info(`💰 Balance: ${balanceSOL.toFixed(4)} SOL`);
    
    if (balanceSOL < CONFIG.TRADE_AMOUNT_SOL * 2) {
      log.warn(`⚠️ Balance bajo. Necesitas al menos ${(CONFIG.TRADE_AMOUNT_SOL * 2).toFixed(3)} SOL`);
    }
    
    return true;
  } catch (error) {
    log.error(`❌ Wallet setup failed: ${error.message}`);
    return false;
  }
}

function initSmartTrader() {
  if (!CONFIG.HELIUS_API_KEY) {
    log.error('❌ HELIUS_API_KEY requerido para Smart Trader!');
    return false;
  }
  
  smartTrader = new HeliusSmartTrader(CONFIG);
  smartTrader.init(connection);
  
  log.info('✅ Helius Smart Trader inicializado');
  return true;
}

// ============================================================================
// TRADING FUNCTIONS
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
      maxRetries: 3
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
      maxRetries: 3
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
    log.error(`Telegram failed: ${error.message}`);
  }
}

// ============================================================================
// HANDLE NEW TOKEN
// ============================================================================

async function handleNewToken(data) {
  try {
    stats.detected++;
    
    const payload = data.data || data;
    const mint = payload.mint || payload.token;
    
    if (!mint || positions.has(mint)) return;
    
    if (!CONFIG.TRADING_ENABLED || !wallet) {
      return;
    }
    
    if (positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
      stats.filtered++;
      return;
    }
    
    const symbol = payload.symbol || payload.tokenSymbol || 'UNKNOWN';
    const name = payload.name || payload.tokenName || symbol;
    
    let initialPrice = 0;
    let marketCap = 0;
    
    if (payload.pairs && Array.isArray(payload.pairs) && payload.pairs.length > 0) {
      const pair = payload.pairs[0];
      initialPrice = parseFloat(pair.priceUsd || pair.price || 0);
      marketCap = parseFloat(pair.marketCap || pair.fdv || 0);
    }
    
    log.info(`🆕 Nuevo token: ${symbol} (${mint.slice(0, 8)})`);
    
    if (!smartTrader) {
      log.warn('⚠️ Smart Trader no inicializado');
      return;
    }
    
    const result = await smartTrader.analyzeToken({
      mint,
      symbol,
      name,
      initialPrice,
      marketCap
    });
    
    if (!result.shouldWatch) {
      stats.filtered++;
      return;
    }
    
    monitorSmartTraderSignals(mint, symbol, name);
    
  } catch (error) {
    log.error(`Error manejando nuevo token: ${error.message}`);
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
    
    if (currentWatch.phase === 'entering' && !positions.has(mint)) {
      const buyPrice = currentWatch.entryPrice;
      const priceChange = smartTrader.getPriceChange(currentWatch, 'first');
      const links = smartTrader.generateLinks(mint);
      
      await sendTelegram(`
🧠 *SMART TRADER - SEÑAL DE COMPRA*

*Token:* ${name} (${symbol})
*Mint:* \`${mint}\`

📊 *Análisis Helius:*
• Cambio: +${priceChange.toFixed(1)}% en ${((Date.now() - currentWatch.firstSeenTime) / 1000).toFixed(0)}s
• Volumen: ${currentWatch.volumeSOL.toFixed(2)} SOL
• Compradores únicos: ${currentWatch.uniqueBuyers}
• Holders: ${currentWatch.holders}
• Liquidez: ${currentWatch.liquidityUSD.toFixed(0)}
• Transacciones: ${currentWatch.txCount}

💰 *Acción:* Comprando ${CONFIG.TRADE_AMOUNT_SOL} SOL ahora...

🔍 [Pump.fun](${links.pumpfun}) | [DexScreener](${links.dexscreener})
      `.trim());
      
      log.trade(`🔥 SMART BUY: ${symbol} @ ${buyPrice.toFixed(8)} | +${priceChange.toFixed(1)}%`);
      
      const buyResult = await executeBuy(mint, CONFIG.TRADE_AMOUNT_SOL);
      
      if (!buyResult.success) {
        log.error(`❌ Compra falló: ${buyResult.error}`);
        stats.errors++;
        await sendTelegram(`❌ *ERROR EN COMPRA*\n\n${symbol}: ${buyResult.error}`);
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
      log.trade(`${dryTag}✅ POSICIÓN ABIERTA: ${symbol} @ ${buyPrice.toFixed(8)}`);
      
      await sendTelegram(`
${dryTag}✅ *COMPRA EJECUTADA*

*${name}* (${symbol})
*Precio:* ${buyPrice.toFixed(8)}
*Invertido:* ${CONFIG.TRADE_AMOUNT_SOL} SOL

📈 *Entrada:*
• Pump detectado: +${priceChange.toFixed(1)}%
• Tiempo análisis: ${((Date.now() - currentWatch.firstSeenTime) / 1000).toFixed(0)}s

🛡️ *Smart Stops activos:*
• Hard Stop: ${CONFIG.HARD_STOP_LOSS_PERCENT}%
• Quick Stop: ${CONFIG.QUICK_STOP_PERCENT}% (<2min)
• Trailing: ${CONFIG.TRAILING_STOP_PERCENT}% (activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%)

${buyResult.dryRun ? '' : `[Tx](https://solscan.io/tx/${buyResult.signature})`}
      `.trim());
      
      monitorSmartPosition(mint).catch(err => {
        log.error(`Error en smart monitor: ${err.message}`);
      });
    }
    
    if (currentWatch.phase === 'exiting' && positions.has(mint)) {
      const position = positions.get(mint);
      const sellPercent = currentWatch.exitReason?.includes('take_profit') 
        ? parseInt(currentWatch.exitReason.match(/\d+/)?.[0] || 100)
        : 100;
      
      log.trade(`🚪 SMART SELL: ${symbol} | ${currentWatch.exitReason} | ${sellPercent}%`);
      
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
  
  log.info(`👀 Smart Monitor: ${position.symbol}...`);
  
  while (positions.has(mint) && smartTrader.watchlist.has(mint) && watch.phase === 'holding') {
    
    const currentWatch = smartTrader.watchlist.get(mint);
    if (!currentWatch) break;
    
    position.currentPrice = currentWatch.currentPrice;
    position.maxPrice = Math.max(position.maxPrice, currentWatch.currentPrice);
    position.minPrice = Math.min(position.minPrice || currentWatch.currentPrice, currentWatch.currentPrice);
    
    if (currentWatch.checksCount % 8 === 0) {
      const profit = position.profitPercent;
      const holdTime = position.elapsedMinutes;
      
      log.info(
        `📊 ${position.symbol}: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}% | ` +
        `${currentWatch.currentPrice.toFixed(8)} | ` +
        `${position.remainingPercent}% | ` +
        `${holdTime.toFixed(1)}min | ` +
        `Trailing: ${currentWatch.trailingStopActive ? '🛡️' : '❌'}`
      );
    }
    
    await sleep(3000);
  }
}

// ============================================================================
// CLOSE & FINALIZE POSITION
// ============================================================================

async function closePosition(mint, percentage, reason) {
  const position = positions.get(mint);
  if (!position || percentage === 0) return;
  
  log.trade(`Cerrando posición: ${position.symbol} (${percentage}%) - ${reason}`);
  
  const sellResult = await executeSell(mint, percentage);
  
  if (!sellResult.success) {
    log.error(`❌ No se pudo vender ${position.symbol}`);
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
  const profitUSD = totalProfit * 150;
  
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
  
  log.trade(`${dryTag}${emoji} CERRADO: ${position.symbol} | ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}% (${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL) | ${reason}`);
  
  await sendTelegram(`
${dryTag}${emoji} *POSICIÓN CERRADA*

*Token:* ${position.name} (${position.symbol})
*Compra:* ${position.buyPrice.toFixed(8)}
*Venta:* ${position.currentPrice.toFixed(8)}
*Ganancia:* ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}%

*Profit:*
• ${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL
• ${profitUSD.toFixed(2)} USD

*Tiempo:* ${position.elapsedMinutes.toFixed(1)} min
*Razón:* ${reason}

*Balance Hoy:*
• ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
• W/L: ${stats.wins}/${stats.losses}
  `.trim());
  
  setTimeout(() => positions.delete(mint), 60000);
}

// ============================================================================
// WEBSOCKET
// ============================================================================

function connectWebSocket() {
  log.info('🔌 Conectando a PumpPortal WebSocket...');
  
  ws = new WebSocket(CONFIG.PUMPPORTAL_WSS);
  
  ws.on('open', () => {
    log.info('✅ WebSocket conectado');
    
    ws.send(JSON.stringify({
      method: 'subscribeNewToken'
    }));
    
    log.info('✅ Suscrito a nuevos tokens');
    
    if (CONFIG.TRADING_ENABLED && wallet) {
      const mode = CONFIG.DRY_RUN ? '🟡 DRY RUN (Simulación)' : '🔴 LIVE (Dinero Real)';
      log.warn(`🤖 MODO TRADING: ${mode}`);
      log.info(`💰 Monto por trade: ${CONFIG.TRADE_AMOUNT_SOL} SOL`);
    } else {
      log.warn('⚠️ Trading deshabilitado - Solo monitoreo');
    }
  });
  
  ws.on('message', (data) => {
    try {
      const parsed = JSON.parse(data);
      handleNewToken(parsed);
    } catch (error) {
      log.error(`Error parseando mensaje: ${error.message}`);
    }
  });
  
  ws.on('error', (error) => {
    log.error(`❌ WebSocket error: ${error.message}`);
  });
  
  ws.on('close', () => {
    log.warn('⚠️ WebSocket desconectado, reconectando en 5s...');
    setTimeout(connectWebSocket, 5000);
  });
}

// ============================================================================
// TELEGRAM BOT COMMANDS
// ============================================================================

function setupTelegramBot() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    log.warn('⚠️ TELEGRAM_BOT_TOKEN no configurado');
    return;
  }
  
  telegramBot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
  
  telegramBot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    const mode = CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE';
    
    telegramBot.sendMessage(chatId, `
🤖 *Pump.fun Trading Bot con Helius*

*Estado:* ${CONFIG.TRADING_ENABLED ? '✅ ACTIVO' : '❌ INACTIVO'}
*Modo:* ${mode}
*Por trade:* ${CONFIG.TRADE_AMOUNT_SOL} SOL

*Comandos:*
/status - Estado del bot
/stats - Estadísticas
/smartstats - Stats Smart Trader
/positions - Posiciones abiertas
/balance - Balance wallet
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/status/, async (msg) => {
    const chatId = msg.chat.id;
    const wsStatus = ws && ws.readyState === WebSocket.OPEN ? '✅' : '❌';
    
    let positionsText = '';
    if (positions.size > 0) {
      positionsText = '\n\n*Posiciones:*\n';
      for (const [mint, pos] of positions) {
        const profit = pos.profitPercent;
        const emoji = profit > 0 ? '💚' : '❌';
        positionsText += `${emoji} ${pos.symbol}: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}%\n`;
      }
    }
    
    telegramBot.sendMessage(chatId, `
📊 *ESTADO*

*Conexión:*
• WebSocket: ${wsStatus}
• Trading: ${CONFIG.TRADING_ENABLED ? '✅' : '❌'}

*Actividad:*
• Detectados: ${stats.detected}
• Comprados: ${stats.bought}
• Posiciones: ${positions.size}
${positionsText}
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/stats/, (msg) => {
    const chatId = msg.chat.id;
    const winRate = (stats.wins + stats.losses) > 0 
      ? ((stats.wins / (stats.wins + stats.losses)) * 100).toFixed(1) 
      : 0;
    
    telegramBot.sendMessage(chatId, `
📈 *ESTADÍSTICAS*

*Profit Total:*
• ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL

*Trades:*
• Wins: ${stats.wins} (${winRate}%)
• Losses: ${stats.losses}
• Mejor: +${stats.bestTrade.toFixed(1)}%
• Peor: ${stats.worstTrade.toFixed(1)}%
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/smartstats/, (msg) => {
    const chatId = msg.chat.id;
    
    if (!smartTrader) {
      telegramBot.sendMessage(chatId, '❌ Smart Trader no inicializado');
      return;
    }
    
    const smartStats = smartTrader.getStats();
    
    telegramBot.sendMessage(chatId, `
🧠 *SMART TRADER STATS*

*Performance:*
• Analizados: ${smartStats.tokensAnalyzed}
• Entradas: ${smartStats.tokensEntered}
• Rechazados: ${smartStats.tokensRejected}
• Win rate: ${smartStats.winRate}%

*Trading:*
• Wins: ${smartStats.wins}
• Losses: ${smartStats.losses}
• Profit: ${smartStats.totalProfit.toFixed(4)} SOL

*Helius:*
• API calls: ${smartStats.heliusCalls}
• Watching: ${smartStats.currentlyWatching}
  `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/positions/, (msg) => {
    const chatId = msg.chat.id;
    
    if (positions.size === 0) {
      telegramBot.sendMessage(chatId, '🔭 Sin posiciones abiertas');
      return;
    }
    
    let message = '📊 *POSICIONES ABIERTAS*\n\n';
    
    for (const [mint, pos] of positions) {
      const profit = pos.profitPercent;
      const emoji = profit > 0 ? '💚' : '❌';
      
      message += `${emoji} *${pos.symbol}*\n`;
      message += `Ganancia: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}%\n`;
      message += `Tiempo: ${pos.elapsedMinutes.toFixed(1)} min\n\n`;
    }
    
    telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/balance/, async (msg) => {
    const chatId = msg.chat.id;
    
    if (!wallet || !connection) {
      telegramBot.sendMessage(chatId, '❌ Wallet no configurado');
      return;
    }
    
    try {
      const balance = await connection.getBalance(wallet.publicKey);
      const balanceSOL = balance / 1e9;
      
      telegramBot.sendMessage(chatId, `
💰 *BALANCE*

• ${balanceSOL.toFixed(4)} SOL
• Posiciones: ${positions.size}

*Profit Sesión:*
• ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
      `.trim(), { parse_mode: 'Markdown' });
    } catch (error) {
      telegramBot.sendMessage(chatId, `❌ Error: ${error.message}`);
    }
  });
  
  log.info('✅ Telegram bot inicializado');
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
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('Bot Running ✅');
    }
  });
  
  server.listen(CONFIG.HEALTH_PORT, () => {
    log.info(`✅ Health server: http://0.0.0.0:${CONFIG.HEALTH_PORT}`);
  });
}

// ============================================================================
// UTILITIES
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
  log.info('🚀 PUMP.FUN TRADING BOT CON HELIUS - INICIANDO');
  log.info('═══════════════════════════════════════════════════════════════');
  
  if (!CONFIG.HELIUS_API_KEY) {
    log.error('❌ HELIUS_API_KEY requerido!');
    process.exit(1);
  }
  
  if (CONFIG.TRADING_ENABLED) {
    if (!CONFIG.WALLET_PRIVATE_KEY) {
      log.error('❌ WALLET_PRIVATE_KEY requerido!');
      process.exit(1);
    }
    
    log.info('💼 Configurando wallet...');
    const walletReady = await setupWallet();
    
    if (!walletReady) {
      log.error('❌ Error configurando wallet!');
      process.exit(1);
    }
    
    log.info('🧠 Inicializando Smart Trader...');
    const smartReady = initSmartTrader();
    
    if (!smartReady) {
      log.error('❌ Error inicializando Smart Trader!');
      process.exit(1);
    }
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
  log.info('✅ Bot iniciado correctamente');
  log.info('═══════════════════════════════════════════════════════════════');
  
  if (telegramBot && CONFIG.TELEGRAM_CHAT_ID) {
    await sendTelegram(`
🤖 *Bot Iniciado*

*Modo:* ${CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE'}
*Monto:* ${CONFIG.TRADE_AMOUNT_SOL} SOL/trade

🧠 Helius Smart Trader activo 👀
    `.trim());
  }
}

process.on('SIGTERM', async () => {
  log.info('🛑 Cerrando...');
  if (ws) ws.close();
  process.exit(0);
});

process.on('SIGINT', async () => {
  log.info('🛑 Cerrando...');
  if (ws) ws.close();
  process.exit(0);
});

main().catch(error => {
  log.error(`❌ Error fatal: ${error.message}`);
  process.exit(1);
});
