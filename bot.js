// trading-bot-hybrid.js - Pump.fun Trading Bot PRO (Híbrido)
// 🚀 Sistema de dos niveles: Stop loss fijo + Trailing stop dinámico
// 💰 Configurado para $100 MXN (~0.03 SOL) por trade

const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, Keypair, VersionedTransaction } = require('@solana/web3.js');
const bs58 = require('bs58');

// ============================================================================
// CONFIG OPTIMIZADA PARA PUMP.FUN
// ============================================================================

const CONFIG = {
  // Telegram
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || '',
  
  // Solana Wallet (⚠️ CREA UNA WALLET NUEVA SOLO PARA EL BOT)
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY || '', // Base58
  
  // RPC (⚠️ USA UN RPC DE PAGO PARA MEJOR EJECUCIÓN)
  SOLANA_RPC: process.env.SOLANA_RPC || 'https://api.mainnet-beta.solana.com',
  
  // PumpPortal
  PUMPPORTAL_WSS: 'wss://pumpportal.fun/api/data',
  PUMPPORTAL_API: 'https://pumpportal.fun/api/trade-local',
  
  // ═══════════════════════════════════════════════════════════════════
  // 💰 ESTRATEGIA DE TRADING (OPTIMIZADA PARA $100 MXN)
  // ═══════════════════════════════════════════════════════════════════
  
  // Monto por trade
  TRADE_AMOUNT_SOL: parseFloat(process.env.TRADE_AMOUNT_SOL || '0.03'), // ~$5 USD / ~$100 MXN
  
  // Sistema de STOP LOSS de dos niveles:
  // Nivel 1: HARD STOP (nunca pierdas más de esto)
  HARD_STOP_LOSS_PERCENT: -40, // Si cae -40% desde compra → VENDE (protección)
  
  // Nivel 2: TRAILING STOP (activo después de +30% ganancia)
  TRAILING_STOP_ACTIVATION: 30, // Activa trailing stop al +30%
  TRAILING_STOP_PERCENT: -25, // Vende si cae -25% desde máximo
  
  // TAKE PROFIT (objetivos de ganancia)
  TAKE_PROFIT_TARGETS: [
    { percent: 100, sellPercent: 50 },  // Al +100%: vende 50% (asegura ganancia)
    { percent: 200, sellPercent: 30 },  // Al +200%: vende 30% más
    { percent: 500, sellPercent: 100 }  // Al +500%: vende todo (moonshot)
  ],
  
  // Time-based exit
  MAX_HOLD_TIME_MIN: 15, // Vende después de 15 min (pump.fun es rápido)
  
  // ═══════════════════════════════════════════════════════════════════
  // 🛡️ FILTROS DE SEGURIDAD
  // ═══════════════════════════════════════════════════════════════════
  
  MIN_INITIAL_LIQUIDITY_USD: 500, // Solo tokens con $500+ liquidez
  MAX_CONCURRENT_POSITIONS: 3, // Máximo 3 tokens simultáneos
  MIN_PRICE_USD: 0.00000001, // Evita tokens con precio 0
  
  // ═══════════════════════════════════════════════════════════════════
  // ⚙️ CONFIGURACIÓN DE TRADING
  // ═══════════════════════════════════════════════════════════════════
  
  TRADING_ENABLED: process.env.TRADING_ENABLED === 'true', // Debe ser 'true' para operar
  DRY_RUN: process.env.DRY_RUN !== 'false', // Default: simular (seguridad)
  
  SLIPPAGE: 30, // 30% slippage (pump.fun es volátil)
  PRIORITY_FEE: 0.0005, // 0.0005 SOL para ejecución rápida
  
  // Health
  HEALTH_PORT: process.env.PORT || 8080
};

// ============================================================================
// GLOBALS
// ============================================================================

let telegramBot = null;
let ws = null;
let wallet = null;
let connection = null;

const positions = new Map(); // mint -> PositionData
const stats = {
  detected: 0,
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
  info: (msg) => console.log(`[INFO] ${new Date().toISOString()} - ${msg}`),
  warn: (msg) => console.warn(`[WARN] ${new Date().toISOString()} - ${msg}`),
  error: (msg) => console.error(`[ERROR] ${new Date().toISOString()} - ${msg}`),
  trade: (msg) => console.log(`[TRADE] ${new Date().toISOString()} - ${msg}`)
};

// ============================================================================
// POSITION DATA (Mejorado con trailing stop)
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
    this.partialSells = []; // Track ventas parciales
    this.remainingPercent = 100; // % que aún tienes
    this.txBuy = null;
    this.txSells = [];
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
// WALLET SETUP
// ============================================================================

async function setupWallet() {
  if (!CONFIG.WALLET_PRIVATE_KEY) {
    log.error('❌ WALLET_PRIVATE_KEY not set!');
    return false;
  }
  
  try {
    const secretKey = bs58.decode(CONFIG.WALLET_PRIVATE_KEY);
    wallet = Keypair.fromSecretKey(secretKey);
    connection = new Connection(CONFIG.SOLANA_RPC, 'confirmed');
    
    // Check balance
    const balance = await connection.getBalance(wallet.publicKey);
    const balanceSOL = balance / 1e9;
    
    log.info(`✅ Wallet: ${wallet.publicKey.toBase58()}`);
    log.info(`💰 Balance: ${balanceSOL.toFixed(4)} SOL ($${(balanceSOL * 150).toFixed(2)} USD)`);
    
    if (balanceSOL < CONFIG.TRADE_AMOUNT_SOL * 2) {
      log.warn(`⚠️ Low balance! Need at least ${(CONFIG.TRADE_AMOUNT_SOL * 2).toFixed(3)} SOL`);
    }
    
    return true;
  } catch (error) {
    log.error(`❌ Wallet setup failed: ${error.message}`);
    return false;
  }
}

// ============================================================================
// PUMPPORTAL TRADING
// ============================================================================

async function executeBuy(mint, amountSOL) {
  if (CONFIG.DRY_RUN) {
    log.trade(`[DRY RUN] Would buy ${amountSOL} SOL of ${mint.slice(0, 8)}`);
    return { success: true, signature: `dry-run-${Date.now()}`, dryRun: true };
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
    
    if (response.status === 200) {
      const txData = new Uint8Array(response.data);
      const tx = VersionedTransaction.deserialize(txData);
      tx.sign([wallet]);
      
      const signature = await connection.sendTransaction(tx, {
        skipPreflight: false,
        maxRetries: 3
      });
      
      // Wait for confirmation
      await connection.confirmTransaction(signature, 'confirmed');
      
      log.trade(`✅ BUY: ${mint.slice(0, 8)} | ${amountSOL} SOL | Tx: ${signature.slice(0, 16)}...`);
      return { success: true, signature };
    }
    
    throw new Error(`API returned ${response.status}`);
  } catch (error) {
    log.error(`❌ Buy failed: ${error.message}`);
    return { success: false, error: error.message };
  }
}

async function executeSell(mint, percentage) {
  if (CONFIG.DRY_RUN) {
    log.trade(`[DRY RUN] Would sell ${percentage}% of ${mint.slice(0, 8)}`);
    return { success: true, signature: `dry-run-${Date.now()}`, dryRun: true };
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
    
    if (response.status === 200) {
      const txData = new Uint8Array(response.data);
      const tx = VersionedTransaction.deserialize(txData);
      tx.sign([wallet]);
      
      const signature = await connection.sendTransaction(tx, {
        skipPreflight: false,
        maxRetries: 3
      });
      
      await connection.confirmTransaction(signature, 'confirmed');
      
      log.trade(`✅ SELL: ${mint.slice(0, 8)} | ${percentage}% | Tx: ${signature.slice(0, 16)}...`);
      return { success: true, signature };
    }
    
    throw new Error(`API returned ${response.status}`);
  } catch (error) {
    log.error(`❌ Sell failed: ${error.message}`);
    return { success: false, error: error.message };
  }
}

// ============================================================================
// PRICE FETCHER
// ============================================================================

async function getCurrentPrice(mint) {
  try {
    const response = await axios.get(
      `https://api.dexscreener.com/latest/dex/tokens/${mint}`,
      { timeout: 5000 }
    );
    
    if (response.data.pairs && response.data.pairs.length > 0) {
      return parseFloat(response.data.pairs[0].priceUsd || 0);
    }
  } catch (error) {
    // Silent fail, intentamos en el siguiente loop
  }
  
  return null;
}

// ============================================================================
// TELEGRAM
// ============================================================================

async function sendTelegram(message) {
  if (!telegramBot || !CONFIG.TELEGRAM_CHAT_ID) return;
  
  try {
    await telegramBot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, {
      parse_mode: 'Markdown',
      disable_web_page_preview: true
    });
  } catch (error) {
    log.error(`Telegram failed: ${error.message}`);
  }
}

// ============================================================================
// TRADING LOGIC (Sistema de Dos Niveles)
// ============================================================================

async function handleNewToken(data) {
  try {
    stats.detected++;
    
    const payload = data.data || data;
    const mint = payload.mint || payload.token;
    
    if (!mint || !wallet || !CONFIG.TRADING_ENABLED) return;
    
    if (positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
      log.warn(`⚠️ Max positions (${CONFIG.MAX_CONCURRENT_POSITIONS}), skipping`);
      return;
    }
    
    if (positions.has(mint)) return;
    
    const symbol = payload.symbol || 'UNKNOWN';
    const name = payload.name || symbol;
    
    let initialPrice = 0;
    let marketCap = 0;
    
    if (payload.pairs?.[0]) {
      initialPrice = parseFloat(payload.pairs[0].priceUsd || 0);
      marketCap = parseFloat(payload.pairs[0].marketCap || 0);
    }
    
    // Filtros
    if (marketCap < CONFIG.MIN_INITIAL_LIQUIDITY_USD) {
      log.info(`🚫 ${symbol} - Low liquidity ($${marketCap.toFixed(0)})`);
      return;
    }
    
    if (initialPrice < CONFIG.MIN_PRICE_USD) {
      log.warn(`⚠️ ${symbol} - Price too low`);
      return;
    }
    
    log.info(`🎯 TARGET: ${symbol} @ $${initialPrice.toFixed(8)} | MCap: $${marketCap.toFixed(0)}`);
    
    // BUY
    const buyResult = await executeBuy(mint, CONFIG.TRADE_AMOUNT_SOL);
    
    if (!buyResult.success) {
      stats.errors++;
      return;
    }
    
    // Create position
    const position = new PositionData({
      mint,
      symbol,
      name,
      buyPrice: initialPrice,
      amountSOL: CONFIG.TRADE_AMOUNT_SOL
    });
    
    position.txBuy = buyResult.signature;
    positions.set(mint, position);
    stats.bought++;
    
    const dryTag = buyResult.dryRun ? '[DRY RUN] ' : '';
    log.trade(`${dryTag}💰 BUY: ${symbol} @ $${initialPrice.toFixed(8)} | ${CONFIG.TRADE_AMOUNT_SOL} SOL`);
    
    const mxnAmount = CONFIG.TRADE_AMOUNT_SOL * 150 * 20; // SOL * USD * MXN
    
    await sendTelegram(`
${dryTag}💰 *COMPRA EJECUTADA*

*Token:* ${name} (${symbol})
*Precio:* $${initialPrice.toFixed(8)}
*Invertido:* ${CONFIG.TRADE_AMOUNT_SOL} SOL (~$${mxnAmount.toFixed(0)} MXN)

*Estrategia:*
• Hard Stop: ${CONFIG.HARD_STOP_LOSS_PERCENT}%
• Trailing Stop: ${CONFIG.TRAILING_STOP_PERCENT}% (activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%)
• Take Profit: +100% (50%), +200% (30%), +500% (100%)
• Max Hold: ${CONFIG.MAX_HOLD_TIME_MIN} min

${buyResult.dryRun ? '' : `[Tx](https://solscan.io/tx/${buyResult.signature})`}
    `.trim());
    
    // Monitor
    monitorPosition(mint);
    
  } catch (error) {
    log.error(`Error handling token: ${error.message}`);
    stats.errors++;
  }
}

async function monitorPosition(mint) {
  const position = positions.get(mint);
  if (!position) return;
  
  try {
    while (positions.has(mint) && position.status === 'holding' && position.remainingPercent > 0) {
      const price = await getCurrentPrice(mint);
      
      if (!price) {
        await sleep(3000);
        continue;
      }
      
      position.currentPrice = price;
      position.maxPrice = Math.max(position.maxPrice, price);
      position.minPrice = Math.min(position.minPrice, price);
      
      const profit = position.profitPercent;
      const profitFromMax = position.profitFromMax;
      
      // ═══════════════════════════════════════════════════════════════
      // NIVEL 1: HARD STOP LOSS (Protección absoluta)
      // ═══════════════════════════════════════════════════════════════
      if (profit <= CONFIG.HARD_STOP_LOSS_PERCENT) {
        log.trade(`🛑 HARD STOP: ${position.symbol} ${profit.toFixed(1)}%`);
        await closePosition(mint, position.remainingPercent, 'hard_stop_loss');
        return;
      }
      
      // ═══════════════════════════════════════════════════════════════
      // NIVEL 2: TRAILING STOP (Después de +30% ganancia)
      // ═══════════════════════════════════════════════════════════════
      if (profit >= CONFIG.TRAILING_STOP_ACTIVATION && !position.trailingStopActive) {
        position.trailingStopActive = true;
        log.trade(`✅ TRAILING STOP ACTIVATED: ${position.symbol} @ +${profit.toFixed(1)}%`);
        
        await sendTelegram(`
✅ *TRAILING STOP ACTIVADO*

*${position.symbol}*
Ganancia actual: +${profit.toFixed(1)}%
Stop dinámico: ${CONFIG.TRAILING_STOP_PERCENT}% desde máximo

El bot ahora protegerá tus ganancias automáticamente.
        `.trim());
      }
      
      if (position.trailingStopActive && profitFromMax <= CONFIG.TRAILING_STOP_PERCENT) {
        log.trade(`📉 TRAILING STOP: ${position.symbol} cayó ${profitFromMax.toFixed(1)}% desde máximo`);
        await closePosition(mint, position.remainingPercent, 'trailing_stop');
        return;
      }
      
      // ═══════════════════════════════════════════════════════════════
      // TAKE PROFIT PARCIAL (Asegura ganancias)
      // ═══════════════════════════════════════════════════════════════
      for (const target of CONFIG.TAKE_PROFIT_TARGETS) {
        const alreadyTaken = position.partialSells.find(s => s.targetPercent === target.percent);
        
        if (!alreadyTaken && profit >= target.percent && position.remainingPercent > 0) {
          const sellPercent = Math.min(target.sellPercent, position.remainingPercent);
          
          log.trade(`💚 TAKE PROFIT: ${position.symbol} @ +${profit.toFixed(1)}% | Selling ${sellPercent}%`);
          
          const sellResult = await executeSell(mint, sellPercent);
          
          if (sellResult.success) {
            position.partialSells.push({
              targetPercent: target.percent,
              sellPercent: sellPercent,
              price: price,
              time: Date.now(),
              tx: sellResult.signature
            });
            
            position.remainingPercent -= sellPercent;
            
            // Estimate sold amount
            const soldValue = (position.amountSOL * sellPercent / 100) * (1 + profit / 100);
            position.soldAmountSOL += soldValue;
            
            await sendTelegram(`
💚 *TAKE PROFIT PARCIAL*

*${position.symbol}*
*Ganancia:* +${profit.toFixed(1)}%
*Vendido:* ${sellPercent}%
*Quedan:* ${position.remainingPercent}%

${sellResult.dryRun ? '' : `[Tx](https://solscan.io/tx/${sellResult.signature})`}
            `.trim());
            
            if (position.remainingPercent === 0) {
              await finalizePosition(mint, 'take_profit_complete');
              return;
            }
          }
        }
      }
      
      // ═══════════════════════════════════════════════════════════════
      // MAX HOLD TIME
      // ═══════════════════════════════════════════════════════════════
      if (position.elapsedMinutes >= CONFIG.MAX_HOLD_TIME_MIN) {
        log.trade(`⏰ MAX HOLD: ${position.symbol} after ${position.elapsedMinutes.toFixed(1)}min`);
        await closePosition(mint, position.remainingPercent, 'timeout');
        return;
      }
      
      // Log progress
      if (Math.random() < 0.15) {
        log.info(`📊 ${position.symbol}: $${price.toFixed(8)} (${profit >= 0 ? '+' : ''}${profit.toFixed(1)}%) | ${position.remainingPercent}% | ${position.elapsedMinutes.toFixed(1)}min`);
      }
      
      await sleep(3000);
    }
  } catch (error) {
    log.error(`Monitor error: ${error.message}`);
    await closePosition(mint, position.remainingPercent, 'error');
  }
}

async function closePosition(mint, percentage, reason) {
  const position = positions.get(mint);
  if (!position || percentage === 0) return;
  
  const sellResult = await executeSell(mint, percentage);
  
  if (!sellResult.success) {
    log.error(`❌ Sell failed for ${position.symbol}`);
    return;
  }
  
  position.txSells.push(sellResult.signature);
  
  // Estimate final sold amount
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
  const profitMXN = profitUSD * 20;
  
  stats.sold++;
  stats.totalProfitSOL += totalProfit;
  stats.totalProfitUSD += profitUSD;
  
  if (totalProfit > 0) {
    stats.wins++;
    if (profitPercent > stats.bestTrade) stats.bestTrade = profitPercent;
  } else {
    stats.losses++;
    if (profitPercent < stats.worstTrade) stats.worstTrade = profitPercent;
  }
  
  const emoji = totalProfit > 0 ? '💚' : '❌';
  const dryTag = position.txSells.some(tx => tx.includes('dry-run')) ? '[DRY RUN] ' : '';
  
  log.trade(`${dryTag}${emoji} CLOSED: ${position.symbol} | ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}% (${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL / $${profitMXN.toFixed(0)} MXN) | ${reason}`);
  
  await sendTelegram(`
${dryTag}${emoji} *POSICIÓN CERRADA*

*Token:* ${position.name} (${position.symbol})
*Compra:* $${position.buyPrice.toFixed(8)}
*Venta:* $${position.currentPrice.toFixed(8)}
*Ganancia:* ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}%

*Profit:*
• ${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL
• $${profitUSD.toFixed(2)} USD
• $${profitMXN.toFixed(0)} MXN

*Tiempo:* ${position.elapsedMinutes.toFixed(1)} min
*Razón:* ${reason}

*Balance Hoy:*
• Total: ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
• USD: $${stats.totalProfitUSD.toFixed(2)}
• MXN: $${(stats.totalProfitUSD * 20).toFixed(0)}
• W/L: ${stats.wins}/${stats.losses}
  `.trim());
  
  setTimeout(() => positions.delete(mint), 60000);
}

// ============================================================================
// WEBSOCKET
// ============================================================================

function connectWebSocket() {
  log.info(`🔌 Connecting to PumpPortal...`);
  
  ws = new WebSocket(CONFIG.PUMPPORTAL_WSS);
  
  ws.on('open', () => {
    log.info('✅ Connected');
    ws.send(JSON.stringify({ method: 'subscribeNewToken' }));
    
    if (CONFIG.TRADING_ENABLED && wallet) {
      const mode = CONFIG.DRY_RUN ? 'DRY RUN (Simulado)' : 'LIVE (Dinero Real)';
      log.warn(`🤖 TRADING: ${mode}`);
      log.info(`💰 Amount: ${CONFIG.TRADE_AMOUNT_SOL} SOL (~$${(CONFIG.TRADE_AMOUNT_SOL * 150 * 20).toFixed(0)} MXN) per trade`);
    }
  });
  
  ws.on('message', (data) => {
    try {
      handleNewToken(JSON.parse(data));
    } catch (error) {
      log.error(`Parse error: ${error.message}`);
    }
  });
  
  ws.on('error', (error) => {
    log.error(`❌ WebSocket error: ${error.message}`);
  });
  
  ws.on('close', () => {
    log.warn('⚠️ Disconnected, reconnecting in 5s...');
    setTimeout(connectWebSocket, 5000);
  });
}

// ============================================================================
// TELEGRAM BOT COMMANDS
// ============================================================================

function setupTelegramBot() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    log.warn('⚠️ Telegram disabled');
    return;
  }
  
  telegramBot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
  
  telegramBot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    const mode = CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE';
    const mxnPerTrade = (CONFIG.TRADE_AMOUNT_SOL * 150 * 20).toFixed(0);
    
    telegramBot.sendMessage(chatId, `
🤖 *Pump.fun Trading Bot PRO*

*Status:* ${CONFIG.TRADING_ENABLED ? '✅ ACTIVO' : '❌ INACTIVO'}
*Modo:* ${mode}
*Por trade:* ${CONFIG.TRADE_AMOUNT_SOL} SOL (~${mxnPerTrade} MXN)

*Estrategia:*
• Hard Stop: ${CONFIG.HARD_STOP_LOSS_PERCENT}%
• Trailing Stop: ${CONFIG.TRAILING_STOP_PERCENT}% (activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%)
• Max Hold: ${CONFIG.MAX_HOLD_TIME_MIN} min

*Comandos:*
/status - Ver estado y posiciones
/stats - Ver estadísticas
/balance - Ver balance de wallet
/positions - Posiciones abiertas
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/status/, async (msg) => {
    const chatId = msg.chat.id;
    const wsStatus = ws && ws.readyState === WebSocket.OPEN ? '✅' : '❌';
    
    let positionsText = '';
    if (positions.size > 0) {
      positionsText = '\n\n*Posiciones Abiertas:*\n';
      for (const [mint, pos] of positions) {
        const profit = pos.profitPercent;
        const emoji = profit > 0 ? '💚' : profit < -20 ? '❌' : '🟡';
        positionsText += `${emoji} ${pos.symbol}: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}% (${pos.remainingPercent}% quedan)\n`;
      }
    } else {
      positionsText = '\n\n📭 Sin posiciones abiertas';
    }
    
    telegramBot.sendMessage(chatId, `
📊 *ESTADO DEL BOT*

*WebSocket:* ${wsStatus}
*Trading:* ${CONFIG.TRADING_ENABLED ? '✅' : '❌'}
*Modo:* ${CONFIG.DRY_RUN ? 'DRY RUN' : 'LIVE'}

*Actividad:*
• Detectados: ${stats.detected}
• Comprados: ${stats.bought}
• Vendidos: ${stats.sold}
• Posiciones: ${positions.size}/${CONFIG.MAX_CONCURRENT_POSITIONS}
${positionsText}
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/stats/, (msg) => {
    const chatId = msg.chat.id;
    const winRate = stats.bought > 0 ? ((stats.wins / (stats.wins + stats.losses)) * 100).toFixed(1) : 0;
    const avgProfit = stats.sold > 0 ? (stats.totalProfitSOL / stats.sold).toFixed(4) : 0;
    const profitMXN = (stats.totalProfitUSD * 20).toFixed(0);
    
    telegramBot.sendMessage(chatId, `
📈 *ESTADÍSTICAS*

*Profit Total:*
• ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
• ${stats.totalProfitUSD.toFixed(2)} USD
• ${profitMXN} MXN

*Trades:*
• Total: ${stats.sold}
• Wins: ${stats.wins} (${winRate}%)
• Losses: ${stats.losses}

*Mejor/Peor:*
• Mejor: +${stats.bestTrade.toFixed(1)}%
• Peor: ${stats.worstTrade.toFixed(1)}%
• Promedio: ${avgProfit} SOL/trade

*Errores:* ${stats.errors}
    `.trim(), { parse_mode: 'Markdown' });
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
      const balanceUSD = balanceSOL * 150;
      const balanceMXN = balanceUSD * 20;
      
      telegramBot.sendMessage(chatId, `
💰 *BALANCE DE WALLET*

*Dirección:*
\`${wallet.publicKey.toBase58()}\`

*Balance:*
• ${balanceSOL.toFixed(4)} SOL
• ${balanceUSD.toFixed(2)} USD
• ${balanceMXN.toFixed(0)} MXN

*Trades disponibles:* ${Math.floor(balanceSOL / CONFIG.TRADE_AMOUNT_SOL)}
      `.trim(), { parse_mode: 'Markdown' });
    } catch (error) {
      telegramBot.sendMessage(chatId, `❌ Error: ${error.message}`);
    }
  });
  
  telegramBot.onText(/\/positions/, (msg) => {
    const chatId = msg.chat.id;
    
    if (positions.size === 0) {
      telegramBot.sendMessage(chatId, '📭 No hay posiciones abiertas');
      return;
    }
    
    let message = '📊 *POSICIONES ABIERTAS*\n\n';
    
    for (const [mint, pos] of positions) {
      const profit = pos.profitPercent;
      const profitSOL = pos.estimatedTotalProfitSOL;
      const emoji = profit > 0 ? '💚' : '❌';
      
      message += `${emoji} *${pos.symbol}*\n`;
      message += `Ganancia: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}% (${profitSOL >= 0 ? '+' : ''}${profitSOL.toFixed(4)} SOL)\n`;
      message += `Precio: ${pos.buyPrice.toFixed(8)} → ${pos.currentPrice.toFixed(8)}\n`;
      message += `Tiempo: ${pos.elapsedMinutes.toFixed(1)} min | Quedan: ${pos.remainingPercent}%\n`;
      message += `Trailing: ${pos.trailingStopActive ? '✅' : '❌'}\n\n`;
    }
    
    telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
  });
  
  log.info('✅ Telegram bot initialized');
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
        websocket: ws && ws.readyState === WebSocket.OPEN,
        trading: CONFIG.TRADING_ENABLED,
        mode: CONFIG.DRY_RUN ? 'dry_run' : 'live',
        positions: positions.size,
        stats
      }));
    } else if (req.url === '/metrics') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(stats));
    } else {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('Pump.fun Trading Bot PRO - Running');
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
  log.info('🚀 Starting Pump.fun Trading Bot PRO...');
  log.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  
  // Validate config
  if (!CONFIG.TELEGRAM_BOT_TOKEN || !CONFIG.TELEGRAM_CHAT_ID) {
    log.warn('⚠️ Telegram not configured - notifications disabled');
  }
  
  if (!CONFIG.TRADING_ENABLED) {
    log.warn('⚠️ TRADING_ENABLED=false - Bot will only monitor (no trading)');
  } else {
    if (!CONFIG.WALLET_PRIVATE_KEY) {
      log.error('❌ WALLET_PRIVATE_KEY required for trading!');
      process.exit(1);
    }
    
    const walletReady = await setupWallet();
    if (!walletReady) {
      log.error('❌ Wallet setup failed!');
      process.exit(1);
    }
  }
  
  log.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  log.info('⚙️  CONFIGURACIÓN:');
  log.info(`   • Monto: ${CONFIG.TRADE_AMOUNT_SOL} SOL (~${(CONFIG.TRADE_AMOUNT_SOL * 150 * 20).toFixed(0)} MXN)`);
  log.info(`   • Hard Stop: ${CONFIG.HARD_STOP_LOSS_PERCENT}%`);
  log.info(`   • Trailing: ${CONFIG.TRAILING_STOP_PERCENT}% (activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%)`);
  log.info(`   • Max Hold: ${CONFIG.MAX_HOLD_TIME_MIN} min`);
  log.info(`   • Max Posiciones: ${CONFIG.MAX_CONCURRENT_POSITIONS}`);
  log.info(`   • Min Liquidez: ${CONFIG.MIN_INITIAL_LIQUIDITY_USD}`);
  log.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  
  if (CONFIG.DRY_RUN) {
    log.warn('');
    log.warn('⚠️  ========================================');
    log.warn('⚠️  DRY RUN MODE - TRANSACCIONES SIMULADAS');
    log.warn('⚠️  No se gastará dinero real');
    log.warn('⚠️  Para trading real: DRY_RUN=false');
    log.warn('⚠️  ========================================');
    log.warn('');
  } else if (CONFIG.TRADING_ENABLED) {
    log.warn('');
    log.warn('🔴 ========================================');
    log.warn('🔴 LIVE TRADING MODE - DINERO REAL');
    log.warn('🔴 Asegúrate de entender los riesgos');
    log.warn('🔴 ========================================');
    log.warn('');
  }
  
  // Start components
  setupTelegramBot();
  startHealthServer();
  connectWebSocket();
  
  log.info('✅ Bot started successfully!');
  log.info('📊 Waiting for new tokens...');
}

// Start
main().catch(error => {
  log.error(`Fatal error: ${error.message}`);
  process.exit(1);
});

// Graceful shutdown
process.on('SIGTERM', async () => {
  log.info('🛑 SIGTERM received, closing positions...');
  
  // Close all positions
  for (const [mint, pos] of positions) {
    if (pos.remainingPercent > 0) {
      await closePosition(mint, pos.remainingPercent, 'shutdown');
    }
  }
  
  if (ws) ws.close();
  process.exit(0);
});

process.on('SIGINT', async () => {
  log.info('🛑 SIGINT received, closing positions...');
  
  for (const [mint, pos] of positions) {
    if (pos.remainingPercent > 0) {
      await closePosition(mint, pos.remainingPercent, 'shutdown');
    }
  }
  
  if (ws) ws.close();
  process.exit(0);
});
