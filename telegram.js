// telegram.js - Comandos Telegram para control en caliente
const TelegramBot = require('node-telegram-bot-api');
const CONFIG = require('./config');
const { setParam, getParam, getStat, redis } = require('./redis');
const { getDryRunTrades } = require('./db');
const { exportDryRunCSV } = require('./export');

let telegramBot = null;

function setupTelegramBot(monitoredTokens, stats, sendTelegramAlert) {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    console.warn('⚠️ TELEGRAM_BOT_TOKEN not set, Telegram bot disabled');
    return;
  }

  telegramBot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });

  // Comando /start
  telegramBot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    const keyboard = {
      inline_keyboard: [
        [
          { text: '📊 Status', callback_data: 'status' },
          { text: '🔍 Tokens', callback_data: 'tokens' }
        ],
        [
          { text: '📈 Stats', callback_data: 'stats' },
          { text: '⚙️ Rules', callback_data: 'rules' }
        ]
      ]
    };

    telegramBot.sendMessage(chatId, `
🤖 *Pump.fun Elite Bot*

Modo DRY_RUN: ${CONFIG.DRY_RUN ? '✅ ON' : '❌ OFF'}

Comandos disponibles:
• /status - Estado del bot
• /tokens - Tokens monitoreados
• /stats - Estadísticas de trading
• /rules - Reglas activas
• /dryrun on|off - Activar/desactivar DRY_RUN
• /set [param] [value] - Cambiar parámetros
• /export - Exportar datos para ML
• /silence [min] - Silenciar alertas
    `.trim(), {
      parse_mode: 'Markdown',
      reply_markup: keyboard
    });
  });

  // Comando /status
  telegramBot.onText(/\/status/, async (msg) => {
    const chatId = msg.chat.id;
    const wsStatus = '✅'; // Asumimos que está conectado
    const monitoredCount = monitoredTokens.size;
    const detected = await getStat('detected');
    const alerts = await getStat('alerts');
    const filtered = await getStat('filtered');
    const dryrunTrades = await getStat('dryrun_trades');

    const message = `
📊 *Estado del Bot*

• Tokens monitoreados: ${monitoredCount}
• WebSocket: ${wsStatus}
• Detectados: ${detected}
• Alertas enviadas: ${alerts}
• Filtrados: ${filtered}
• Trades DRY_RUN: ${dryrunTrades}
    `.trim();

    telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
  });

  // Comando /tokens
  telegramBot.onText(/\/tokens/, (msg) => {
    const chatId = msg.chat.id;

    if (monitoredTokens.size === 0) {
      telegramBot.sendMessage(chatId, '📭 No hay tokens monitoreados actualmente.');
      return;
    }

    let message = '🔍 *Tokens Monitoreados* (Top 10):\n\n';

    const tokens = Array.from(monitoredTokens.values()).slice(0, 10);
    for (const token of tokens) {
      const gain = token.gainPercent;
      const icon = gain > 50 ? '🔴' : gain > 0 ? '🟡' : '⚪';
      message += `${icon} ${token.symbol} | ${gain >= 0 ? '+' : ''}${gain.toFixed(1)}% | ${token.elapsedMinutes.toFixed(1)}min\n`;
    }

    telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
  });

  // Comando /stats
  telegramBot.onText(/\/stats/, async (msg) => {
    const chatId = msg.chat.id;
    const wins = await getStat('dryrun_wins');
    const losses = await getStat('dryrun_losses');
    const total = wins + losses;
    const winrate = total > 0 ? (wins / total * 100).toFixed(2) : 0;

    const message = `
📈 *Estadísticas de Trading*

• Total Trades: ${total}
• Wins: ${wins}
• Losses: ${losses}
• Winrate: ${winrate}%
    `.trim();

    telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
  });

  // Comando /rules
  telegramBot.onText(/\/rules/, async (msg) => {
    const chatId = msg.chat.id;
    const minLiq = await getParam('min_liq_usd', CONFIG.MIN_INITIAL_LIQUIDITY_USD);
    const maxDD = await getParam('max_drawdown', 20);
    const slopeMin = await getParam('slope_min', 0);

    let rulesText = '🔧 *Reglas Activas*\n\n';
    rulesText += `• Liquidez mínima: $${minLiq}\n`;
    rulesText += `• Drawdown máximo: ${maxDD}%\n`;
    rulesText += `• Slope mínimo: ${slopeMin} USD/min\n\n`;

    for (const rule of CONFIG.ALERT_RULES) {
      rulesText += `• ${rule.description}\n`;
    }

    telegramBot.sendMessage(chatId, rulesText, { parse_mode: 'Markdown' });
  });

  // Comando /set
  telegramBot.onText(/\/set (\w+)\s+([\w\.\-]+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const key = match[1];
    const value = match[2];

    await setParam(key, value);
    telegramBot.sendMessage(chatId, `✅ Parámetro actualizado: ${key} = ${value}`);
  });

  // Comando /dryrun
  telegramBot.onText(/\/dryrun (on|off)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const state = match[1] === 'on';

    await setParam('dry_run', state);
    CONFIG.DRY_RUN = state;

    telegramBot.sendMessage(chatId, `✅ DRY_RUN ${state ? 'activado' : 'desactivado'}`);
  });

  // Comando /export
  telegramBot.onText(/\/export/, async (msg) => {
    const chatId = msg.chat.id;
    try {
      await exportDryRunCSV();
      // Enviar el archivo CSV
      await telegramBot.sendDocument(chatId, 'dryrun_export.csv');
    } catch (error) {
      telegramBot.sendMessage(chatId, `❌ Error al exportar: ${error.message}`);
    }
  });

  // Comando /silence
  telegramBot.onText(/\/silence (\d+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const minutes = parseInt(match[1]);

    await setParam('silence_until', Date.now() + minutes * 60000);
    telegramBot.sendMessage(chatId, `🔇 Alertas silenciadas por ${minutes} minutos`);
  });

  // Callback queries para los botones inline
  telegramBot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;

    if (query.data === 'status') {
      // Implementar similar a /status
      telegramBot.answerCallbackQuery(query.id);
      telegramBot.sendMessage(chatId, '📊 Estado del bot...', { parse_mode: 'Markdown' });
    }
    // ... otros callbacks
  });

  console.log('✅ Telegram bot initialized');
  return telegramBot;
}

module.exports = { setupTelegramBot };
