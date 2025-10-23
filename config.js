// config.js - Configuración centralizada
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
  
  // Trading
  DRY_RUN: process.env.DRY_RUN === 'true',
  TRADE_AMOUNT_SOL: Number(process.env.TRADE_AMOUNT_SOL || 0.01),
  SLIPPAGE_BPS: Number(process.env.SLIPPAGE_BPS || 100),
  
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
  
  // Queue
  MAX_CONCURRENCY: Number(process.env.MAX_CONCURRENCY || 8),
  
  // Health
  HEALTH_PORT: process.env.PORT || 8080,
  
  // Logging
  LOG_LEVEL: process.env.LOG_LEVEL || 'INFO'
};

module.exports = CONFIG;
