// queue.js
const PENDING = [];
let ACTIVE = 0;
const MAX_CONCURRENCY = parseInt(process.env.MAX_CONCURRENCY || '8');

function enqueueEvent(evt, handler) {
  PENDING.push({ evt, handler });
  pump();
}

function pump() {
  while (ACTIVE < MAX_CONCURRENCY && PENDING.length) {
    const { evt, handler } = PENDING.shift();
    ACTIVE++;
    Promise.resolve()
      .then(() => handler(evt))
      .catch(err => console.error(`[ERROR] ${new Date().toISOString()} - ${err.message}`))
      .finally(() => { ACTIVE--; pump(); });
  }
}

module.exports = { enqueueEvent };
