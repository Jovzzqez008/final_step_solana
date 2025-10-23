// config.js - Configuración centralizada con trading real
require('dotenv').config();

const CONFIG = {
  // Telegram
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || '',
  
  // PumpPortal
  PUMPPORTAL_WSS: process.env.PUMPPORTAL_WSS || 'wss://pumpportal.fun/api/data',
  
  // Database
  REDIS_URL: process.env.REDIS_URL || '',
  DATABASE_URL: process.env.DATABASE_URL || '',
  
  // Solana
  RPC_URL: process.env.RPC_URL || 'https://api.mainnet-beta.solana.com',
  SOL_PRICE_USD: Number(process.env.SOL_PRICE_USD || 150),
  
  // Trading Mode
  TRADING_MODE: process.env.TRADING_MODE || 'DRY_RUN', // DRY_RUN | LIVE
  DRY_RUN: process.env.DRY_RUN === 'true' || process.env.TRADING_MODE === 'DRY_RUN',
  TRADE_AMOUNT_SOL: Number(process.env.TRADE_AMOUNT_SOL || 0.01),
  SLIPPAGE_BPS: Number(process.env.SLIPPAGE_BPS || 100),
  
  // Wallet Real (de tu código)
  SOLANA_WALLET_PATH: process.env.SOLANA_WALLET_PATH,
  DEVELOPER_ADDRESS: process.env.DEVELOPER_ADDRESS || '8bXf8Rg3u4Prz71LgKR5mpa7aMe2F4cSKYYRctmqro6x',
  PRIORITY_FEE_BASE: Number(process.env.PRIORITY_FEE_BASE || 0.0003),
  
  // Estrategia TreeCityWes
  MINIMUM_BUY_AMOUNT: Number(process.env.MINIMUM_BUY_AMOUNT || 0.015),
  MAX_BONDING_CURVE_PROGRESS: Number(process.env.MAX_BONDING_CURVE_PROGRESS || 10),
  SELL_BONDING_CURVE_PROGRESS: Number(process.env.SELL_BONDING_CURVE_PROGRESS || 15),
  PROFIT_TARGET_1: 1.25,    // 25%
  PROFIT_TARGET_2: 1.50,    // 50% total  
  STOP_LOSS: 0.90,          // -10%
  MOON_BAG: 0.25,           // 25% moon bag
  
  // Alert Rules
  ALERT_RULES: [
    { name: 'fast_2x', percent: 100, timeWindowMin: 20, description: '2x en 20 minutos' },
    { name: 'momentum_150', percent: 150, timeWindowMin: 15, description: '2.5x en 15 minutos' }
  ],
  
  // Monitoring
  MAX_MONITOR_TIME_MIN: Number(process.env.MAX_MONITOR_TIME_MIN || 30),
  DUMP_THRESHOLD_PERCENT: Number(process.env.DUMP_THRESHOLD_PERCENT || -60),
  PRICE_CHECK_INTERVAL_SEC: Number(process.env.PRICE_CHECK_INTERVAL_SEC || 3),
  MIN_INITIAL_LIQUIDITY_USD: Number(process.env.MIN_INITIAL_LIQUIDITY_USD || 300),
  
  // Performance
  MAX_CONCURRENCY: Number(process.env.MAX_CONCURRENCY || 8),
  
  // Health
  HEALTH_PORT: process.env.PORT || 8080,
  
  // Logging
  LOG_LEVEL: process.env.LOG_LEVEL || 'INFO'
};

// Validaciones
if (CONFIG.TRADING_MODE === 'LIVE' && !CONFIG.SOLANA_WALLET_PATH) {
  console.warn('⚠️ TRADING_MODE=LIVE pero SOLANA_WALLET_PATH no está configurado');
}

module.exports = CONFIG;
