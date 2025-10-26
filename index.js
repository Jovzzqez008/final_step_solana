const TelegramBot = require('node-telegram-bot-api');
const express = require('express');
const app = express();

const TOKEN = process.env.BOT_TOKEN;
const CHAT_ID = process.env.CHAT_ID;
const bot = new TelegramBot(TOKEN);

app.get('/', (req, res) => {
  bot.sendPhoto(CHAT_ID, 'https://yourdomain.com/chill.jpg', {
    caption: `
🕘 9:00 PM CST — $CHILL detonates.
📉 Price dipped. Vibes loaded.
💰 Market cap: $5.6K
🔗 https://pump.fun/CHILLCOIN
    `
  });
  res.send('Message sent!');
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Bot running on port ${PORT}`));
