async function monitorPosition(mint) {
  const pos = STATE.positions.get(mint);
  if (!pos) return;
  
  try {
    while (STATE.positions.has(mint) && pos.remainingPercent > 0) {
      // Obtener precio actual de DexScreener
      const data = await getTokenData(mint);
      
      if (!data || !data.price) {
        await sleep(3000);
        continue;
      }
      
      const currentPrice = data.price;
      const pnlPercent = ((currentPrice - pos.entryPrice) / pos.entryPrice) * 100;
      const holdTime = (Date.now() - pos.entryTime) / 60000;
      
      pos.currentPrice = currentPrice;
      pos.pnl = pnlPercent;
      
      // Actualizar peak
      if (pnlPercent > pos.peak) {
        pos.peak = pnlPercent;
      }
      
      // Activar trailing stop
      if (pnlPercent >= CONFIG.TRAILING_ACTIVATION && !pos.trailingActive) {
        pos.trailingActive = true;
        pos.trailingStop = pnlPercent + CONFIG.TRAILING_PERCENT;
        log('INFO', `[POSITION] 🛡️ Trailing activado para ${pos.symbol} @ +${pnlPercent.toFixed(1)}%`);
        await sendTelegram(
          `🛡️ <b>TRAILING STOP ACTIVADO</b>\n\n` +
          `${pos.symbol}: +${pnlPercent.toFixed(1)}%\n` +
          `Stop: +${pos.trailingStop.toFixed(1)}%`
        );
      }
      
      // Actualizar trailing
      if (pos.trailingActive) {
        const newStop = pnlPercent + CONFIG.TRAILING_PERCENT;
        if (newStop > pos.trailingStop) {
          pos.trailingStop = newStop;
        }
      }
      
      // Log estado
      const stopIndicator = pos.trailingActive ? '🛡️' : '';
      log('INFO', `[POSITION] 📊 ${pos.symbol}: ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(1)}% | ${currentPrice.toFixed(8)} | ${holdTime.toFixed(1)}min ${stopIndicator}`);
      
      // Checks de salida
      let shouldSell = false;
      let sellReason = '';
      let sellPercentage = 100;
      
      // Hard stop
      if (pnlPercent <= CONFIG.HARD_STOP_LOSS) {
        shouldSell = true;
        sellReason = `Hard Stop (${pnlPercent.toFixed(1)}%)`;
      }
      // Quick stop
      else if (holdTime < 2 && pnlPercent <= CONFIG.QUICK_STOP) {
        shouldSell = true;
        sellReason = `Quick Stop (${pnlPercent.toFixed(1)}%)`;
      }
      // Trailing stop
      else if (pos.trailingActive && pnlPercent <= pos.trailingStop) {
        shouldSell = true;
        sellReason = `Trailing Stop (${pnlPercent.toFixed(1)}%)`;
      }
      // Take profit escalonado
      else if (pnlPercent >= CONFIG.TAKE_PROFIT_3 && !pos.tp3Taken) {
        shouldSell = true;
        sellPercentage = 50;
        sellReason = `TP3 (${pnlPercent.toFixed(1)}%)`;
        pos.tp3Taken = true;
      }
      else if (pnlPercent >= CONFIG.TAKE_PROFIT_2 && !pos.tp2Taken) {
        shouldSell = true;
        sellPercentage = 30;
        sellReason = `TP2 (${pnlPercent.toFixed(1)}%)`;
        pos.tp2Taken = true;
      }
      else if (pnlPercent >= CONFIG.TAKE_PROFIT_1 && !pos.tp1Taken) {
        shouldSell = true;
        sellPercentage = 25;
        sellReason = `TP1 (${pnlPercent.toFixed(1)}%)`;
        pos.tp1Taken = true;
      }
      // Max hold time
      else if (holdTime >= CONFIG.MAX_HOLD_TIME_MIN) {
        shouldSell = true;
        sellReason = `Max Hold Time (${holdTime.toFixed(1)}min)`;
      }
      // Stagnant (sin movimiento)
      else if (holdTime >= CONFIG.STAGNANT_TIME_MIN && Math.abs(pnlPercent - pos.lastPnl) < 2) {
        shouldSell = true;
        sellReason = `Stagnant (${holdTime.toFixed(1)}min)`;
      }
      
      pos.lastPnl = pnlPercent;
      
      if (shouldSell) {
        log('INFO', `[POSITION] 🔔 Vendiendo ${sellPercentage}% de ${pos.symbol}: ${sellReason}`);
        
        const result = await executeSell(mint, sellPercentage);
        
        if (result.success) {
          const finalPnl = pnlPercent;
          
          // Si vendió todo, cerrar posición
          if (sellPercentage === 100) {
            STATE.positions.delete(mint);
            STATE.stats.tradesExecuted++;
            STATE.stats.totalPnL += finalPnl;
            
            if (finalPnl > 0) {
              STATE.stats.wins++;
              if (finalPnl > STATE.stats.bestTrade) STATE.stats.bestTrade = finalPnl;
            } else {
              STATE.stats.losses++;
              if (finalPnl < STATE.stats.worstTrade) STATE.stats.worstTrade = finalPnl;
            }
          } else {
            pos.remainingPercent -= sellPercentage;
          }
          
          log('SUCCESS', `[POSITION] ✅ Vendido ${sellPercentage}% de ${pos.symbol}: ${finalPnl > 0 ? '+' : ''}${finalPnl.toFixed(2)}%`);
          
          await sendTelegram(
            `${finalPnl > 0 ? '✅' : '❌'} <b>VENTA EJECUTADA</b>\n\n` +
            `Token: ${pos.symbol}\n` +
            `Razón: ${sellReason}\n` +
            `Cantidad: ${sellPercentage}%\n\n` +
            `PnL: ${finalPnl > 0 ? '+' : ''}${finalPnl.toFixed(2)}%\n` +
            `Tiempo: ${holdTime.toFixed(1)}min\n` +
            `Tx: <code>${result.signature.slice(0, 8)}...</code>`
          );
          
          if (sellPercentage === 100) {
            return; // Salir del loop
          }
        }
      }
      
      await sleep(3000);
    }
  } catch (error) {
    log('ERROR', `Error monitoreando posición ${mint.slice(0, 8)}: ${error.message}`);
  }
}// trading-bot-hybrid.js - PUMP.FUN TRADING BOT - VERSIÓN FINAL
// 🚀 Bot completo usando DexScreener (sin Helius, sin rate limits)
// 💰 Optimizado para operar automáticamente en pump.fun

const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, PublicKey, Keypair, VersionedTransaction, LAMPORTS_PER_SOL } = require('@solana/web3.js');
const bs58 = require('bs58');
require('dotenv').config();

// ═══════════════════════════════════════════════════════════════
// 🔧 HELPER: BASE58 DECODE (compatible con todas las versiones)
// ═══════════════════════════════════════════════════════════════

function decodeBase58(str) {
  try {
    // Intentar con bs58 normal
    if (typeof bs58.decode === 'function') {
      return bs58.decode(str);
    }
    // Intentar con default export
    if (bs58.default && typeof bs58.default.decode === 'function') {
      return bs58.default.decode(str);
    }
    // Fallback: decodificador manual
    const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
    let num = BigInt(0);
    for (let i = 0; i < str.length; i++) {
      const value = ALPHABET.indexOf(str[i]);
      if (value === -1) throw new Error(`Invalid base58 character: ${str[i]}`);
      num = num * 58n + BigInt(value);
    }
    let hex = num.toString(16);
    if (hex.length % 2) hex = '0' + hex;
    for (let i = 0; i < str.length && str[i] === '1'; i++) {
      hex = '00' + hex;
    }
    return Buffer.from(hex, 'hex');
  } catch (error) {
    throw new Error(`Error decodificando base58: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// 📋 CONFIGURACIÓN
// ═══════════════════════════════════════════════════════════════

const CONFIG = {
  // Wallet
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY,
  
  // RPC (sin rate limits)
  RPC_ENDPOINTS: [
    'https://api.mainnet-beta.solana.com',
    'https://solana-api.projectserum.com',
    'https://rpc.ankr.com/solana'
  ],
  
  // Helius (opcional, solo para transacciones rápidas)
  HELIUS_API_KEY: process.env.HELIUS_API_KEY,
  
  // Telegram
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
  
  // Trading
  DRY_RUN: process.env.DRY_RUN === 'true',
  TRADE_AMOUNT_SOL: parseFloat(process.env.TRADE_AMOUNT_SOL || '0.007'),
  SLIPPAGE: parseInt(process.env.SLIPPAGE || '25'),
  PRIORITY_FEE: parseFloat(process.env.PRIORITY_FEE || '0.0005'),
  
  // Pump.fun Program
  PUMP_PROGRAM: '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',
  PUMP_GLOBAL: '4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf',
  
  // Stop Loss / Take Profit
  HARD_STOP_LOSS: parseFloat(process.env.HARD_STOP_LOSS || '-45'),
  QUICK_STOP: parseFloat(process.env.QUICK_STOP || '-25'),
  TRAILING_ACTIVATION: parseFloat(process.env.TRAILING_ACTIVATION || '40'),
  TRAILING_PERCENT: parseFloat(process.env.TRAILING_PERCENT || '-20'),
  TAKE_PROFIT_1: parseFloat(process.env.TAKE_PROFIT_1 || '80'),
  TAKE_PROFIT_2: parseFloat(process.env.TAKE_PROFIT_2 || '150'),
  TAKE_PROFIT_3: parseFloat(process.env.TAKE_PROFIT_3 || '300'),
  
  // Smart Trader - Detección
  EARLY_VELOCITY_MIN: parseFloat(process.env.EARLY_VELOCITY_MIN || '15'),
  EARLY_TIME_WINDOW: parseInt(process.env.EARLY_TIME_WINDOW || '30'),
  CONFIRMATION_VELOCITY: parseFloat(process.env.CONFIRMATION_VELOCITY || '35'),
  CONFIRMATION_TIME: parseInt(process.env.CONFIRMATION_TIME || '60'),
  MIN_LIQUIDITY_SOL: parseFloat(process.env.MIN_LIQUIDITY_SOL || '0.5'),
  MIN_BUY_COUNT: parseInt(process.env.MIN_BUY_COUNT || '5'),
  MIN_UNIQUE_BUYERS: parseInt(process.env.MIN_UNIQUE_BUYERS || '4'),
  MIN_HOLDERS: parseInt(process.env.MIN_HOLDERS || '8'),
  MAX_TOP_HOLDER_PERCENT: parseFloat(process.env.MAX_TOP_HOLDER_PERCENT || '40'),
  
  // Timing
  MAX_HOLD_TIME_MIN: parseInt(process.env.MAX_HOLD_TIME_MIN || '12'),
  STAGNANT_TIME_MIN: parseInt(process.env.STAGNANT_TIME_MIN || '4'),
  MAX_WATCH_TIME_SEC: parseInt(process.env.MAX_WATCH_TIME_SEC || '60'),
  MAX_CONCURRENT_POSITIONS: parseInt(process.env.MAX_CONCURRENT_POSITIONS || '3'),
  
  // Intervals
  MONITOR_INTERVAL: 3000,
  POSITION_CHECK_INTERVAL: 2000
};

// ═══════════════════════════════════════════════════════════════
// 🌐 ESTADO GLOBAL
// ═══════════════════════════════════════════════════════════════

const STATE = {
  wallet: null,
  connection: null,
  bot: null,
  pumpWs: null,
  tradeWs: new Map(),
  watchlist: new Map(),
  positions: new Map(),
  stats: {
    tokensDetected: 0,
    tradesExecuted: 0,
    wins: 0,
    losses: 0,
    totalPnL: 0,
    bestTrade: 0,
    worstTrade: 0
  },
  lastBuyAttempts: new Map(),
  rpcIndex: 0
};

// ═══════════════════════════════════════════════════════════════
// 🔧 UTILIDADES
// ═══════════════════════════════════════════════════════════════

function log(level, message) {
  const timestamp = new Date().toISOString();
  const prefix = level === 'ERROR' ? '❌' : level === 'WARN' ? '⚠️' : level === 'SUCCESS' ? '✅' : 'ℹ️';
  console.log(`[${level}] ${timestamp} ${prefix} ${message}`);
}

function getNextRPC() {
  const rpc = CONFIG.RPC_ENDPOINTS[STATE.rpcIndex];
  STATE.rpcIndex = (STATE.rpcIndex + 1) % CONFIG.RPC_ENDPOINTS.length;
  return rpc;
}

async function sendTelegram(message) {
  if (!STATE.bot || !CONFIG.TELEGRAM_CHAT_ID) return;
  try {
    await STATE.bot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, { parse_mode: 'HTML' });
  } catch (error) {
    log('WARN', `Error enviando Telegram: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// 📊 DEXSCREENER - OBTENER DATOS DEL TOKEN
// ═══════════════════════════════════════════════════════════════

async function getTokenData(mint) {
  try {
    const response = await axios.get(`${CONFIG.DEXSCREENER_API}${mint}`, {
      timeout: 5000
    });
    
    if (response.data.pairs && response.data.pairs.length > 0) {
      const pair = response.data.pairs[0];
      return {
        price: parseFloat(pair.priceUsd || 0),
        liquidity: parseFloat(pair.liquidity?.usd || 0),
        marketCap: parseFloat(pair.marketCap || 0),
        volume24h: parseFloat(pair.volume?.h24 || 0),
        priceChange5m: parseFloat(pair.priceChange?.m5 || 0),
        priceChange1h: parseFloat(pair.priceChange?.h1 || 0),
        txns24h: pair.txns?.h24 || { buys: 0, sells: 0 },
        symbol: pair.baseToken?.symbol || 'UNKNOWN',
        name: pair.baseToken?.name || 'UNKNOWN'
      };
    }
    
    return null;
  } catch (error) {
    // Silent fail - lo intentamos después
    return null;
  }
}

// ═══════════════════════════════════════════════════════════════
// 🔗 BLOCKCHAIN HELPERS (solo para transacciones)
// ═══════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════
// 💰 TRADING
// ═══════════════════════════════════════════════════════════════

async function executeBuy(mint, amountSol) {
  try {
    if (CONFIG.DRY_RUN) {
      log('INFO', `[DRY RUN] Comprando ${amountSol} SOL de ${mint.slice(0, 8)}`);
      return { success: true, signature: 'dry-run-signature' };
    }
    
    const canBuy = await checkBuyLimits(mint);
    if (!canBuy.allowed) {
      log('WARN', `Compra bloqueada: ${canBuy.reason}`);
      return { success: false, error: canBuy.reason };
    }
    
    const response = await axios.post('https://pumpportal.fun/api/trade-local', {
      publicKey: STATE.wallet.publicKey.toString(),
      action: 'buy',
      mint: mint,
      denominatedInSol: 'true',
      amount: amountSol,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    });
    
    if (response.status !== 200) {
      throw new Error(`PumpPortal error: ${response.statusText}`);
    }
    
    const txData = new Uint8Array(await response.data);
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([STATE.wallet]);
    
    const signature = await STATE.connection.sendTransaction(tx, {
      skipPreflight: true,
      maxRetries: 3
    });
    
    await STATE.connection.confirmTransaction(signature, 'confirmed');
    
    STATE.lastBuyAttempts.set(mint, Date.now());
    
    return { success: true, signature };
  } catch (error) {
    log('ERROR', `Error comprando ${mint.slice(0, 8)}: ${error.message}`);
    return { success: false, error: error.message };
  }
}

async function executeSell(mint, percentage = 100) {
  try {
    if (CONFIG.DRY_RUN) {
      log('INFO', `[DRY RUN] Vendiendo ${percentage}% de ${mint.slice(0, 8)}`);
      return { success: true, signature: 'dry-run-signature' };
    }
    
    const response = await axios.post('https://pumpportal.fun/api/trade-local', {
      publicKey: STATE.wallet.publicKey.toString(),
      action: 'sell',
      mint: mint,
      denominatedInSol: 'false',
      amount: `${percentage}%`,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    });
    
    if (response.status !== 200) {
      throw new Error(`PumpPortal error: ${response.statusText}`);
    }
    
    const txData = new Uint8Array(await response.data);
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([STATE.wallet]);
    
    const signature = await STATE.connection.sendTransaction(tx, {
      skipPreflight: true,
      maxRetries: 3
    });
    
    await STATE.connection.confirmTransaction(signature, 'confirmed');
    
    return { success: true, signature };
  } catch (error) {
    log('ERROR', `Error vendiendo ${mint.slice(0, 8)}: ${error.message}`);
    return { success: false, error: error.message };
  }
}

async function checkBuyLimits(mint) {
  // Límite de posiciones concurrentes
  if (STATE.positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
    return { allowed: false, reason: 'Max posiciones alcanzadas' };
  }
  
  // Cooldown entre compras del mismo token
  const lastAttempt = STATE.lastBuyAttempts.get(mint);
  if (lastAttempt && Date.now() - lastAttempt < 60000) {
    return { allowed: false, reason: 'Cooldown activo (60s)' };
  }
  
  return { allowed: true };
}

// ═══════════════════════════════════════════════════════════════
// 🎯 SMART TRADER
// ═══════════════════════════════════════════════════════════════

async function analyzeToken(mint) {
  const token = STATE.watchlist.get(mint);
  if (!token) return null;
  
  try {
    // Obtener datos de DexScreener
    const data = await getTokenData(mint);
    if (!data) return null;
    
    const currentPrice = data.price;
    const liquidityUSD = data.liquidity;
    
    // Actualizar precio en token
    if (!token.initialPrice) {
      token.initialPrice = currentPrice;
    }
    token.currentPrice = currentPrice;
    
    // Calcular velocidad
    const elapsed = (Date.now() - token.firstSeen) / 1000;
    const priceChange = ((currentPrice - token.initialPrice) / token.initialPrice) * 100;
    
    // Log progreso
    log('INFO', `[TRADER] 📊 ${token.symbol}: ${priceChange.toFixed(1)}% | Liq: ${liquidityUSD.toFixed(0)} | Buys: ${token.buyCount} | ${elapsed.toFixed(0)}s`);
    
    // Validaciones tempranas
    if (elapsed < CONFIG.EARLY_TIME_WINDOW) {
      if (priceChange >= CONFIG.EARLY_VELOCITY_MIN) {
        token.earlySignal = true;
        log('INFO', `[TRADER] ⚡ Señal temprana en ${token.symbol}: +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s`);
      }
    }
    
    // Validación de confirmación
    if (elapsed >= CONFIG.EARLY_TIME_WINDOW && elapsed <= CONFIG.CONFIRMATION_TIME) {
      if (token.earlySignal && priceChange >= CONFIG.CONFIRMATION_VELOCITY) {
        
        log('INFO', `[TRADER] 🔍 Validando ${token.symbol}: ${token.buyCount} buys, ${token.uniqueBuyers.size} únicos`);
        
        // Validaciones
        const checks = {
          velocity: priceChange >= CONFIG.CONFIRMATION_VELOCITY,
          liquidity: liquidityUSD >= CONFIG.MIN_LIQUIDITY_SOL * 150, // Convert SOL to USD (~$150)
          buys: token.buyCount >= CONFIG.MIN_BUY_COUNT,
          uniqueBuyers: token.uniqueBuyers.size >= CONFIG.MIN_UNIQUE_BUYERS,
          price: currentPrice > 0
        };
        
        const passed = Object.values(checks).every(v => v);
        
        if (passed) {
          log('SUCCESS', `[TRADER] 🚀 SEÑAL CONFIRMADA: ${token.symbol} | +${priceChange.toFixed(1)}% | ${token.buyCount} buys | ${liquidityUSD.toFixed(0)}`);
          await sendTelegram(
            `🚀 <b>SEÑAL DE COMPRA</b>\n\n` +
            `Token: ${token.name} (${token.symbol})\n` +
            `Mint: <code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
            `📈 Precio: +${priceChange.toFixed(1)}%\n` +
            `💰 Liquidez: ${liquidityUSD.toFixed(0)}\n` +
            `🛒 Compras: ${token.buyCount}\n` +
            `⏱ Tiempo: ${elapsed.toFixed(0)}s`
          );
          return { shouldBuy: true, price: currentPrice, checks };
        } else {
          log('WARN', `[TRADER] ❌ ${token.symbol} no cumple: ${JSON.stringify(checks)}`);
        }
      }
    }
    
    // Timeout
    if (elapsed > CONFIG.MAX_WATCH_TIME_SEC) {
      log('INFO', `[TRADER] ⏰ Timeout para ${token.symbol} (${elapsed.toFixed(0)}s)`);
      STATE.watchlist.delete(mint);
      unsubscribeFromTrades(mint);
    }
    
    return { shouldBuy: false };
  } catch (error) {
    log('ERROR', `Error analizando ${mint.slice(0, 8)}: ${error.message}`);
    return null;
  }
}

async function monitorPosition(mint) {
  const pos = STATE.positions.get(mint);
  if (!pos) return;
  
  try {
    const curve = await getBondingCurveData(mint);
    if (!curve) return;
    
    const currentPrice = calculatePrice(curve);
    const pnlPercent = ((currentPrice - pos.entryPrice) / pos.entryPrice) * 100;
    const holdTime = (Date.now() - pos.entryTime) / 60000;
    
    pos.currentPrice = currentPrice;
    pos.pnl = pnlPercent;
    
    // Actualizar peak
    if (pnlPercent > pos.peak) {
      pos.peak = pnlPercent;
    }
    
    // Activar trailing stop
    if (pnlPercent >= CONFIG.TRAILING_ACTIVATION && !pos.trailingActive) {
      pos.trailingActive = true;
      pos.trailingStop = pnlPercent + CONFIG.TRAILING_PERCENT;
      log('INFO', `[POSITION] 🛡️ Trailing activado para ${pos.symbol} @ +${pnlPercent.toFixed(1)}%`);
      await sendTelegram(
        `🛡️ <b>TRAILING STOP ACTIVADO</b>\n\n` +
        `${pos.symbol}: +${pnlPercent.toFixed(1)}%\n` +
        `Stop: +${pos.trailingStop.toFixed(1)}%`
      );
    }
    
    // Actualizar trailing
    if (pos.trailingActive) {
      const newStop = pnlPercent + CONFIG.TRAILING_PERCENT;
      if (newStop > pos.trailingStop) {
        pos.trailingStop = newStop;
      }
    }
    
    // Log estado
    const stopIndicator = pos.trailingActive ? '🛡️' : '';
    log('INFO', `[POSITION] 📊 ${pos.symbol}: ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(1)}% | $${currentPrice.toFixed(8)} | ${holdTime.toFixed(1)}min ${stopIndicator}`);
    
    // Checks de salida
    let shouldSell = false;
    let sellReason = '';
    let sellPercentage = 100;
    
    // Hard stop
    if (pnlPercent <= CONFIG.HARD_STOP_LOSS) {
      shouldSell = true;
      sellReason = `Hard Stop (${pnlPercent.toFixed(1)}%)`;
    }
    // Quick stop (solo primeros minutos)
    else if (holdTime < 2 && pnlPercent <= CONFIG.QUICK_STOP) {
      shouldSell = true;
      sellReason = `Quick Stop (${pnlPercent.toFixed(1)}%)`;
    }
    // Trailing stop
    else if (pos.trailingActive && pnlPercent <= pos.trailingStop) {
      shouldSell = true;
      sellReason = `Trailing Stop (${pnlPercent.toFixed(1)}%)`;
    }
    // Take profit escalonado
    else if (pnlPercent >= CONFIG.TAKE_PROFIT_3 && !pos.tp3Taken) {
      shouldSell = true;
      sellPercentage = 50;
      sellReason = `TP3 (${pnlPercent.toFixed(1)}%)`;
      pos.tp3Taken = true;
    }
    else if (pnlPercent >= CONFIG.TAKE_PROFIT_2 && !pos.tp2Taken) {
      shouldSell = true;
      sellPercentage = 30;
      sellReason = `TP2 (${pnlPercent.toFixed(1)}%)`;
      pos.tp2Taken = true;
    }
    else if (pnlPercent >= CONFIG.TAKE_PROFIT_1 && !pos.tp1Taken) {
      shouldSell = true;
      sellPercentage = 25;
      sellReason = `TP1 (${pnlPercent.toFixed(1)}%)`;
      pos.tp1Taken = true;
    }
    // Max hold time
    else if (holdTime >= CONFIG.MAX_HOLD_TIME_MIN) {
      shouldSell = true;
      sellReason = `Max Hold Time (${holdTime.toFixed(1)}min)`;
    }
    // Stagnant (sin movimiento)
    else if (holdTime >= CONFIG.STAGNANT_TIME_MIN && Math.abs(pnlPercent - pos.lastPnl) < 2) {
      shouldSell = true;
      sellReason = `Stagnant (${holdTime.toFixed(1)}min)`;
    }
    
    pos.lastPnl = pnlPercent;
    
    if (shouldSell) {
      log('INFO', `[POSITION] 🔔 Vendiendo ${sellPercentage}% de ${pos.symbol}: ${sellReason}`);
      
      const result = await executeSell(mint, sellPercentage);
      
      if (result.success) {
        const finalPnl = pnlPercent;
        
        // Si vendió todo, cerrar posición
        if (sellPercentage === 100) {
          STATE.positions.delete(mint);
          STATE.stats.tradesExecuted++;
          STATE.stats.totalPnL += finalPnl;
          
          if (finalPnl > 0) {
            STATE.stats.wins++;
            if (finalPnl > STATE.stats.bestTrade) STATE.stats.bestTrade = finalPnl;
          } else {
            STATE.stats.losses++;
            if (finalPnl < STATE.stats.worstTrade) STATE.stats.worstTrade = finalPnl;
          }
        }
        
        log('SUCCESS', `[POSITION] ✅ Vendido ${sellPercentage}% de ${pos.symbol}: ${finalPnl > 0 ? '+' : ''}${finalPnl.toFixed(2)}%`);
        
        await sendTelegram(
          `${finalPnl > 0 ? '✅' : '❌'} <b>VENTA EJECUTADA</b>\n\n` +
          `Token: ${pos.symbol}\n` +
          `Razón: ${sellReason}\n` +
          `Cantidad: ${sellPercentage}%\n\n` +
          `PnL: ${finalPnl > 0 ? '+' : ''}${finalPnl.toFixed(2)}%\n` +
          `Tiempo: ${holdTime.toFixed(1)}min\n` +
          `Tx: <code>${result.signature.slice(0, 8)}...</code>`
        );
      }
    }
  } catch (error) {
    log('ERROR', `Error monitoreando posición ${mint.slice(0, 8)}: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// 🌐 WEBSOCKETS
// ═══════════════════════════════════════════════════════════════

function connectPumpPortal() {
  STATE.pumpWs = new WebSocket('wss://pumpportal.fun/api/data');
  
  STATE.pumpWs.on('open', () => {
    log('SUCCESS', '✅ Conectado a PumpPortal');
    STATE.pumpWs.send(JSON.stringify({ method: 'subscribeNewToken' }));
  });
  
  STATE.pumpWs.on('message', async (data) => {
    try {
      const msg = JSON.parse(data);
      
      if (msg.mint) {
        await handleNewToken(msg);
      }
    } catch (error) {
      log('ERROR', `Error procesando mensaje PumpPortal: ${error.message}`);
    }
  });
  
  STATE.pumpWs.on('error', (error) => {
    log('ERROR', `Error WebSocket PumpPortal: ${error.message}`);
  });
  
  STATE.pumpWs.on('close', () => {
    log('WARN', '⚠️ Desconectado de PumpPortal, reconectando en 5s...');
    setTimeout(connectPumpPortal, 5000);
  });
}

function subscribeToTokenTrades(mint) {
  if (STATE.tradeWs.has(mint)) return;
  
  const ws = new WebSocket('wss://pumpportal.fun/api/data');
  
  ws.on('open', () => {
    ws.send(JSON.stringify({
      method: 'subscribeTokenTrade',
      keys: [mint]
    }));
    log('INFO', `📡 Suscrito a trades de ${mint.slice(0, 8)}`);
  });
  
  ws.on('message', (data) => {
    try {
      const trade = JSON.parse(data);
      const token = STATE.watchlist.get(mint);
      if (token && trade.txType) {
        if (trade.txType === 'buy') {
          token.buyCount++;
          token.uniqueBuyers.add(trade.traderPublicKey);
        } else if (trade.txType === 'sell') {
          token.sellCount++;
        }
      }
    } catch (error) {
      // Ignorar errores de parsing
    }
  });
  
  ws.on('error', () => {
    STATE.tradeWs.delete(mint);
  });
  
  STATE.tradeWs.set(mint, ws);
}

function unsubscribeFromTrades(mint) {
  const ws = STATE.tradeWs.get(mint);
  if (ws) {
    ws.close();
    STATE.tradeWs.delete(mint);
  }
}

async function handleNewToken(token) {
  const mint = token.mint;
  
  if (STATE.watchlist.has(mint) || STATE.positions.has(mint)) {
    return;
  }
  
  STATE.stats.tokensDetected++;
  
  log('INFO', `🆕 Token: ${token.name || 'Unknown'} (${token.symbol || 'UNK'}) - ${mint.slice(0, 8)}...`);
  
  STATE.watchlist.set(mint, {
    mint,
    name: token.name || 'Unknown',
    symbol: token.symbol || 'UNK',
    firstSeen: Date.now(),
    initialPrice: null,
    currentPrice: null,
    buyCount: 0,
    sellCount: 0,
    uniqueBuyers: new Set(),
    earlySignal: false,
    lastPnl: 0
  });
  
  subscribeToTokenTrades(mint);
  
  await sendTelegram(
    `🆕 <b>Nuevo Token Detectado</b>\n\n` +
    `${token.name} (${token.symbol})\n` +
    `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>`
  );
}

// ═══════════════════════════════════════════════════════════════
// 🔄 LOOPS PRINCIPALES
// ═══════════════════════════════════════════════════════════════

async function monitorWatchlist() {
  for (const [mint, token] of STATE.watchlist.entries()) {
    const analysis = await analyzeToken(mint);
    
    if (analysis && analysis.shouldBuy) {
      const result = await executeBuy(mint, CONFIG.TRADE_AMOUNT_SOL);
      
      if (result.success) {
        STATE.positions.set(mint, {
          mint,
          name: token.name,
          symbol: token.symbol,
          entryPrice: analysis.price,
          currentPrice: analysis.price,
          entryTime: Date.now(),
          pnl: 0,
          peak: 0,
          trailingActive: false,
          trailingStop: 0,
          lastPnl: 0,
          tp1Taken: false,
          tp2Taken: false,
          tp3Taken: false
        });
        
        STATE.watchlist.delete(mint);
        unsubscribeFromTrades(mint);
        
        log('SUCCESS', `[TRADE] ✅ POSICIÓN ABIERTA: ${token.symbol} @ ${analysis.price.toFixed(8)}`);
        
        await sendTelegram(
          `✅ <b>COMPRA EJECUTADA</b>\n\n` +
          `Token: ${token.name} (${token.symbol})\n` +
          `Precio: ${analysis.price.toFixed(8)}\n` +
          `Monto: ${CONFIG.TRADE_AMOUNT_SOL} SOL\n` +
          `Tx: <code>${result.signature.slice(0, 8)}...</code>`
        );
      }
    }
  }
}

async function monitorPositions() {
  for (const [mint] of STATE.positions.entries()) {
    await monitorPosition(mint);
  }
}

// ═══════════════════════════════════════════════════════════════
// 💬 COMANDOS TELEGRAM
// ═══════════════════════════════════════════════════════════════

function setupTelegramCommands() {
  if (!STATE.bot) return;
  
  STATE.bot.onText(/\/start/, (msg) => {
    STATE.bot.sendMessage(msg.chat.id, 
      `🚀 <b>Pump.fun Trading Bot</b>\n\n` +
      `Comandos disponibles:\n` +
      `/status - Estado del bot\n` +
      `/stats - Estadísticas de trading\n` +
      `/positions - Posiciones abiertas\n` +
      `/watchlist - Tokens en observación\n` +
      `/balance - Balance de wallet`,
      { parse_mode: 'HTML' }
    );
  });
  
  STATE.bot.onText(/\/status/, async (msg) => {
    const uptime = process.uptime();
    const hours = Math.floor(uptime / 3600);
    const minutes = Math.floor((uptime % 3600) / 60);
    
    STATE.bot.sendMessage(msg.chat.id,
      `📊 <b>Estado del Bot</b>\n\n` +
      `⏱ Uptime: ${hours}h ${minutes}m\n` +
      `🎯 Tokens detectados: ${STATE.stats.tokensDetected}\n` +
      `👀 En watchlist: ${STATE.watchlist.size}\n` +
      `📈 Posiciones: ${STATE.positions.size}/${CONFIG.MAX_CONCURRENT_POSITIONS}\n` +
      `💰 Trades ejecutados: ${STATE.stats.tradesExecuted}\n\n` +
      `${CONFIG.DRY_RUN ? '⚠️ <b>MODO DRY RUN</b>' : '✅ <b>MODO REAL</b>'}`,
      { parse_mode: 'HTML' }
    );
  });
  
  STATE.bot.onText(/\/stats/, (msg) => {
    const winRate = STATE.stats.tradesExecuted > 0 
      ? (STATE.stats.wins / STATE.stats.tradesExecuted * 100).toFixed(1)
      : 0;
    
    STATE.bot.sendMessage(msg.chat.id,
      `📈 <b>Estadísticas</b>\n\n` +
      `Total Trades: ${STATE.stats.tradesExecuted}\n` +
      `✅ Wins: ${STATE.stats.wins}\n` +
      `❌ Losses: ${STATE.stats.losses}\n` +
      `📊 Win Rate: ${winRate}%\n\n` +
      `💰 PnL Total: ${STATE.stats.totalPnL > 0 ? '+' : ''}${STATE.stats.totalPnL.toFixed(2)}%\n` +
      `🚀 Mejor Trade: +${STATE.stats.bestTrade.toFixed(2)}%\n` +
      `💥 Peor Trade: ${STATE.stats.worstTrade.toFixed(2)}%`,
      { parse_mode: 'HTML' }
    );
  });
  
  STATE.bot.onText(/\/positions/, (msg) => {
    if (STATE.positions.size === 0) {
      STATE.bot.sendMessage(msg.chat.id, '📭 No hay posiciones abiertas');
      return;
    }
    
    let message = '📈 <b>Posiciones Abiertas</b>\n\n';
    
    for (const [mint, pos] of STATE.positions.entries()) {
      const holdTime = (Date.now() - pos.entryTime) / 60000;
      message += `<b>${pos.symbol}</b>\n`;
      message += `PnL: ${pos.pnl > 0 ? '+' : ''}${pos.pnl.toFixed(2)}%\n`;
      message += `Precio: ${pos.currentPrice.toFixed(8)}\n`;
      message += `Tiempo: ${holdTime.toFixed(1)}min\n`;
      message += `${pos.trailingActive ? '🛡️ Trailing activo' : ''}\n\n`;
    }
    
    STATE.bot.sendMessage(msg.chat.id, message, { parse_mode: 'HTML' });
  });
  
  STATE.bot.onText(/\/watchlist/, (msg) => {
    if (STATE.watchlist.size === 0) {
      STATE.bot.sendMessage(msg.chat.id, '📭 Watchlist vacía');
      return;
    }
    
    let message = '👀 <b>Tokens en Observación</b>\n\n';
    
    for (const [mint, token] of STATE.watchlist.entries()) {
      const elapsed = (Date.now() - token.firstSeen) / 1000;
      message += `<b>${token.symbol}</b>\n`;
      message += `Buys: ${token.buyCount} | Sells: ${token.sellCount}\n`;
      message += `Tiempo: ${elapsed.toFixed(0)}s\n`;
      message += `${token.earlySignal ? '⚡ Señal temprana' : ''}\n\n`;
    }
    
    STATE.bot.sendMessage(msg.chat.id, message, { parse_mode: 'HTML' });
  });
  
  STATE.bot.onText(/\/balance/, async (msg) => {
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey);
      const solBalance = balance / LAMPORTS_PER_SOL;
      
      STATE.bot.sendMessage(msg.chat.id,
        `💰 <b>Balance</b>\n\n` +
        `Wallet: <code>${STATE.wallet.publicKey.toString().slice(0, 8)}...</code>\n` +
        `Balance: ${solBalance.toFixed(4)} SOL`,
        { parse_mode: 'HTML' }
      );
    } catch (error) {
      STATE.bot.sendMessage(msg.chat.id, `❌ Error obteniendo balance: ${error.message}`);
    }
  });
}

// ═══════════════════════════════════════════════════════════════
// 🚀 INICIALIZACIÓN
// ═══════════════════════════════════════════════════════════════

async function setupWallet() {
  try {
    if (!CONFIG.WALLET_PRIVATE_KEY) {
      throw new Error('WALLET_PRIVATE_KEY no configurada en .env');
    }
    
    // Decodificar private key - soportar diferentes versiones de bs58
    let privateKeyBytes;
    try {
      // Intentar con bs58.decode
      if (typeof bs58.decode === 'function') {
        privateKeyBytes = bs58.decode(CONFIG.WALLET_PRIVATE_KEY);
      } 
      // Intentar con default export
      else if (bs58.default && typeof bs58.default.decode === 'function') {
        privateKeyBytes = bs58.default.decode(CONFIG.WALLET_PRIVATE_KEY);
      }
      // Si bs58 en sí es la función
      else if (typeof bs58 === 'function') {
        privateKeyBytes = bs58(CONFIG.WALLET_PRIVATE_KEY);
      }
      else {
        throw new Error('No se pudo encontrar función decode en bs58');
      }
    } catch (decodeError) {
      throw new Error(`Error decodificando private key: ${decodeError.message}`);
    }
    
    STATE.wallet = Keypair.fromSecretKey(privateKeyBytes);
    log('SUCCESS', `✅ Wallet: ${STATE.wallet.publicKey.toString().slice(0, 8)}...`);
    
    // Usar RPC público por defecto
    let rpcUrl = getNextRPC();
    
    // Si hay Helius key válida, usarla para transacciones
    if (CONFIG.HELIUS_API_KEY && CONFIG.HELIUS_API_KEY.length > 20) {
      rpcUrl = `https://mainnet.helius-rpc.com/?api-key=${CONFIG.HELIUS_API_KEY}`;
      log('INFO', '🚀 Usando Helius RPC para transacciones');
    }
    
    STATE.connection = new Connection(rpcUrl, {
      commitment: 'confirmed',
      confirmTransactionInitialTimeout: 60000
    });
    
    // Verificar balance (con retry)
    let balance = 0;
    for (let i = 0; i < 3; i++) {
      try {
        balance = await STATE.connection.getBalance(STATE.wallet.publicKey);
        break;
      } catch (error) {
        if (i === 2) {
          log('WARN', '⚠️ No se pudo verificar balance, continuando...');
        } else {
          await new Promise(resolve => setTimeout(resolve, 2000));
        }
      }
    }
    
    if (balance > 0) {
      log('SUCCESS', `💰 Balance: ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL`);
    }
    
    return true;
  } catch (error) {
    log('ERROR', `❌ Error configurando wallet: ${error.message}`);
    return false;
  }
}

async function setupTelegram() {
  try {
    if (!CONFIG.TELEGRAM_BOT_TOKEN) {
      log('WARN', '⚠️ Telegram no configurado (opcional)');
      return true;
    }
    
    STATE.bot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
    setupTelegramCommands();
    
    await sendTelegram(
      `🚀 <b>Bot Iniciado</b>\n\n` +
      `Modo: ${CONFIG.DRY_RUN ? 'DRY RUN' : 'REAL'}\n` +
      `Trade Amount: ${CONFIG.TRADE_AMOUNT_SOL} SOL\n` +
      `Max Posiciones: ${CONFIG.MAX_CONCURRENT_POSITIONS}`
    );
    
    log('SUCCESS', '✅ Telegram conectado');
    return true;
  } catch (error) {
    log('WARN', `⚠️ Error configurando Telegram: ${error.message}`);
    return true; // No es crítico
  }
}

function startHealthServer() {
  const http = require('http');
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'ok',
        uptime: process.uptime(),
        watchlist: STATE.watchlist.size,
        positions: STATE.positions.size,
        stats: STATE.stats
      }));
    } else {
      res.writeHead(404);
      res.end('Not Found');
    }
  });
  
  server.listen(3000, () => {
    log('INFO', '🏥 Health server en puerto 3000');
  });
}

async function main() {
  log('INFO', '═══════════════════════════════════════════════════════════════');
  log('INFO', '🚀 PUMP.FUN TRADING BOT - VERSIÓN FINAL');
  log('INFO', '═══════════════════════════════════════════════════════════════');
  
  // Setup
  log('INFO', '💼 Configurando wallet...');
  const walletOk = await setupWallet();
  if (!walletOk) {
    log('ERROR', '❌ No se pudo configurar wallet. Abortando.');
    process.exit(1);
  }
  
  log('INFO', '💬 Configurando Telegram...');
  await setupTelegram();
  
  log('INFO', '🌐 Conectando a PumpPortal...');
  connectPumpPortal();
  
  log('INFO', '🏥 Iniciando health server...');
  startHealthServer();
  
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  log('SUCCESS', `✅ BOT INICIADO - ${CONFIG.DRY_RUN ? 'MODO DRY RUN' : 'MODO REAL'}`);
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  log('INFO', `📊 Trade Amount: ${CONFIG.TRADE_AMOUNT_SOL} SOL`);
  log('INFO', `🎯 Max Posiciones: ${CONFIG.MAX_CONCURRENT_POSITIONS}`);
  log('INFO', `🛑 Hard Stop: ${CONFIG.HARD_STOP_LOSS}%`);
  log('INFO', `⚡ Quick Stop: ${CONFIG.QUICK_STOP}%`);
  log('INFO', `🛡️ Trailing: ${CONFIG.TRAILING_ACTIVATION}% → ${CONFIG.TRAILING_PERCENT}%`);
  log('INFO', `💰 Take Profits: ${CONFIG.TAKE_PROFIT_1}% / ${CONFIG.TAKE_PROFIT_2}% / ${CONFIG.TAKE_PROFIT_3}%`);
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  
  // Loops de monitoreo
  setInterval(async () => {
    try {
      await monitorWatchlist();
    } catch (error) {
      log('ERROR', `Error en monitor watchlist: ${error.message}`);
    }
  }, CONFIG.MONITOR_INTERVAL);
  
  setInterval(async () => {
    try {
      await monitorPositions();
    } catch (error) {
      log('ERROR', `Error en monitor positions: ${error.message}`);
    }
  }, CONFIG.POSITION_CHECK_INTERVAL);
  
  // Stats cada 5 minutos
  setInterval(() => {
    log('INFO', `📊 Stats: ${STATE.stats.tokensDetected} detectados | ${STATE.watchlist.size} watching | ${STATE.positions.size} posiciones | ${STATE.stats.tradesExecuted} trades`);
  }, 300000);
}

// Manejo de señales
process.on('SIGTERM', async () => {
  log('INFO', '🛑 Recibida señal SIGTERM, cerrando...');
  
  if (STATE.pumpWs) STATE.pumpWs.close();
  STATE.tradeWs.forEach(ws => ws.close());
  
  if (STATE.bot) {
    await sendTelegram('🛑 <b>Bot detenido</b>');
    STATE.bot.stopPolling();
  }
  
  process.exit(0);
});

process.on('SIGINT', async () => {
  log('INFO', '🛑 Recibida señal SIGINT, cerrando...');
  
  if (STATE.pumpWs) STATE.pumpWs.close();
  STATE.tradeWs.forEach(ws => ws.close());
  
  if (STATE.bot) {
    await sendTelegram('🛑 <b>Bot detenido</b>');
    STATE.bot.stopPolling();
  }
  
  process.exit(0);
});

// Manejo de errores no capturados
process.on('uncaughtException', (error) => {
  log('ERROR', `💥 Excepción no capturada: ${error.message}`);
  console.error(error);
});

process.on('unhandledRejection', (reason, promise) => {
  log('ERROR', `💥 Promesa rechazada no manejada: ${reason}`);
  console.error(promise);
});

// Iniciar
main().catch(error => {
  log('ERROR', `💥 Error fatal: ${error.message}`);
  console.error(error);
  process.exit(1);
});
