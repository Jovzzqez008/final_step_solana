// redis.js
const Redis = require('ioredis');

const redis = new Redis(process.env.REDIS_URL, {
  maxRetriesPerRequest: 2,
  enableReadyCheck: true,
  lazyConnect: false,
});

async function seenMint(mint) {
  const key = `mint_seen:${mint}`;
  const ok = await redis.set(key, 1, 'NX', 'EX', 86400);
  return ok === 'OK';
}

async function lockMonitor(mint, ttlSec = 1800) {
  const key = `monitor_lock:${mint}`;
  const ok = await redis.set(key, 1, 'NX', 'EX', ttlSec);
  return ok === 'OK';
}

async function releaseMonitor(mint) {
  await redis.del(`monitor_lock:${mint}`);
}

async function incrStat(stat) {
  await redis.incr(`stats:${stat}`);
}

async function getStat(stat) {
  const v = await redis.get(`stats:${stat}`);
  return Number(v || 0);
}

async function setParam(key, value) {
  await redis.set(`param:${key}`, String(value));
}

async function getParam(key, fallback) {
  const v = await redis.get(`param:${key}`);
  return v !== null ? (isNaN(Number(v)) ? v : Number(v)) : fallback;
}

module.exports = {
  redis,
  seenMint,
  lockMonitor,
  releaseMonitor,
  incrStat,
  getStat,
  setParam,
  getParam,
};
