// pump-data-fetcher.js - VERSIÓN CORREGIDA con Rate Limiting y PDA Fix
const { Connection, PublicKey } = require('@solana/web3.js');
const axios = require('axios');

// Constantes de Pump.fun CORREGIDAS
const PUMP_PROGRAM_ID = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');
const PUMP_GLOBAL_ACCOUNT = new PublicKey('4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf');
const PUMP_FEE_RECIPIENT = new PublicKey('CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM');

// Estructura de la Bonding Curve Account
const BONDING_CURVE_LAYOUT_SIZE = 97;
const VIRTUAL_SOL_OFFSET = 64;
const VIRTUAL_TOKEN_OFFSET = 72;
const REAL_SOL_OFFSET = 80;
const TOKEN_SUPPLY_OFFSET = 89;

// Rate Limiter para RPC requests
class RateLimiter {
  constructor(maxRequestsPerSecond = 10) {
    this.maxRequests = maxRequestsPerSecond;
    this.queue = [];
    this.processing = false;
    this.requestTimes = [];
  }

  async throttle(fn) {
    return new Promise((resolve, reject) => {
      this.queue.push({ fn, resolve, reject });
      this.processQueue();
    });
  }

  async processQueue() {
    if (this.processing || this.queue.length === 0) return;

    this.processing = true;

    while (this.queue.length > 0) {
      // Limpiar requests antiguos (más de 1 segundo)
      const now = Date.now();
      this.requestTimes = this.requestTimes.filter(t => now - t < 1000);

      // Si alcanzamos el límite, esperar
      if (this.requestTimes.length >= this.maxRequests) {
        const oldestRequest = this.requestTimes[0];
        const waitTime = 1000 - (now - oldestRequest);
        if (waitTime > 0) {
          await new Promise(resolve => setTimeout(resolve, waitTime));
        }
        continue;
      }

      // Procesar siguiente request
      const { fn, resolve, reject } = this.queue.shift();
      this.requestTimes.push(Date.now());

      try {
        const result = await fn();
        resolve(result);
      } catch (error) {
        reject(error);
      }

      // Pequeño delay entre requests
      await new Promise(resolve => setTimeout(resolve, 50));
    }

    this.processing = false;
  }

  getQueueSize() {
    return this.queue.length;
  }

  getRequestCount() {
    const now = Date.now();
    return this.requestTimes.filter(t => now - t < 1000).length;
  }
}

class PumpDataFetcher {
  constructor(rpcUrl = null) {
    this.connection = new Connection(
      rpcUrl || process.env.HELIUS_RPC_URL || 'https://api.mainnet-beta.solana.com',
      'confirmed'
    );
    this.solPriceUSD = 150;
    this.lastSolPriceUpdate = 0;
    
    // Rate limiter: 10 requests/segundo (seguro para RPC público)
    this.rateLimiter = new RateLimiter(10);
    
    // Cache para evitar requests duplicados
    this.cache = new Map();
    this.cacheTimeout = 3000; // 3 segundos
    
    console.log('✅ PumpDataFetcher inicializado con rate limiting');
  }

  /**
   * MÉTODO 1: Obtener datos desde la Bonding Curve Account (MÁS CONFIABLE)
   * ✅ FIX: Usar PUMP_PROGRAM_ID en lugar de PUMP_GLOBAL_ACCOUNT
   */
  async getTokenDataFromBondingCurve(mintAddress) {
    try {
      const mint = new PublicKey(mintAddress);
      
      // ✅ FIX CRÍTICO: Derivar la bonding curve address correctamente
      const [bondingCurveAddress] = PublicKey.findProgramAddressSync(
        [Buffer.from('bonding-curve'), mint.toBuffer()],
        PUMP_PROGRAM_ID  // ✅ Usar PUMP_PROGRAM_ID, no PUMP_GLOBAL_ACCOUNT
      );

      // Rate-limited request
      const accountInfo = await this.rateLimiter.throttle(async () => {
        return await this.connection.getAccountInfo(bondingCurveAddress);
      });
      
      if (!accountInfo || accountInfo.data.length < BONDING_CURVE_LAYOUT_SIZE) {
        return null;
      }

      // Parsear datos MANUALMENTE (sin Borsh)
      const data = accountInfo.data;
      
      const virtualSolReserves = data.readBigUInt64LE(VIRTUAL_SOL_OFFSET);
      const virtualTokenReserves = data.readBigUInt64LE(VIRTUAL_TOKEN_OFFSET);
      const realSolReserves = data.readBigUInt64LE(REAL_SOL_OFFSET);
      const tokenTotalSupply = data.readBigUInt64LE(TOKEN_SUPPLY_OFFSET);
      const complete = data.readUInt8(88) === 1;

      // CALCULAR PRECIO
      const pricePerToken = Number(virtualSolReserves) / Number(virtualTokenReserves);
      const priceInSol = pricePerToken / 1e9;
      const priceInUsd = priceInSol * this.solPriceUSD;

      // CALCULAR MARKET CAP
      const supply = Number(tokenTotalSupply) / 1e6;
      const marketCapUsd = priceInUsd * supply;

      // CALCULAR LIQUIDEZ
      const liquiditySol = Number(realSolReserves) / 1e9;
      const liquidityUsd = liquiditySol * this.solPriceUSD;

      // CALCULAR BONDING CURVE PROGRESS
      const bondingCurveProgress = Math.min(100, (liquiditySol / 85) * 100);

      return {
        price: priceInUsd,
        priceInSol: priceInSol,
        marketCap: marketCapUsd,
        liquidity: liquidityUsd,
        liquiditySol: liquiditySol,
        bondingCurve: Math.round(bondingCurveProgress),
        supply: supply,
        virtualSolReserves: Number(virtualSolReserves) / 1e9,
        virtualTokenReserves: Number(virtualTokenReserves) / 1e6,
        isComplete: complete,
        source: 'bonding_curve_onchain'
      };

    } catch (error) {
      console.error(`Error fetching bonding curve data: ${error.message}`);
      return null;
    }
  }

  /**
   * MÉTODO 2: API oficial de Pump.fun (BACKUP)
   */
  async getTokenDataFromAPI(mintAddress) {
    try {
      const response = await axios.get(
        `https://frontend-api.pump.fun/coins/${mintAddress}`,
        { timeout: 5000 }
      );

      if (response.data) {
        return {
          price: parseFloat(response.data.usd_market_cap) / parseFloat(response.data.total_supply || 1e9),
          marketCap: parseFloat(response.data.usd_market_cap || 0),
          liquidity: parseFloat(response.data.virtual_sol_reserves || 0) * this.solPriceUSD,
          bondingCurve: parseFloat(response.data.progress || 0),
          supply: parseFloat(response.data.total_supply || 0) / 1e6,
          creator: response.data.creator,
          createdAt: response.data.created_timestamp,
          source: 'pump_api'
        };
      }

      return null;
    } catch (error) {
      // No loguear errores de API para no saturar logs
      return null;
    }
  }

  /**
   * MÉTODO 3: DexScreener (ÚLTIMO RECURSO)
   */
  async getTokenDataFromDexScreener(mintAddress) {
    try {
      const response = await axios.get(
        `https://api.dexscreener.com/latest/dex/tokens/${mintAddress}`,
        { timeout: 5000 }
      );

      if (response.data.pairs && response.data.pairs.length > 0) {
        const pair = response.data.pairs[0];
        
        return {
          price: parseFloat(pair.priceUsd || 0),
          marketCap: parseFloat(pair.fdv || pair.marketCap || 0),
          liquidity: parseFloat(pair.liquidity?.usd || 0),
          bondingCurve: 0,
          volume24h: parseFloat(pair.volume?.h24 || 0),
          priceChange24h: parseFloat(pair.priceChange?.h24 || 0),
          source: 'dexscreener'
        };
      }

      return null;
    } catch (error) {
      return null;
    }
  }

  /**
   * MÉTODO PRINCIPAL: Intenta todos los métodos en cascada CON CACHE
   */
  async getTokenData(mintAddress) {
    // Verificar cache
    const cached = this.getCached(mintAddress);
    if (cached) {
      return cached;
    }

    // Actualizar precio SOL si es necesario
    await this.updateSolPrice();

    // 1. Intentar bonding curve (más confiable)
    let data = await this.getTokenDataFromBondingCurve(mintAddress);
    if (data && data.price > 0) {
      this.setCache(mintAddress, data);
      return data;
    }

    // 2. Intentar API de Pump.fun
    data = await this.getTokenDataFromAPI(mintAddress);
    if (data && data.price > 0) {
      this.setCache(mintAddress, data);
      return data;
    }

    // 3. Último recurso: DexScreener
    data = await this.getTokenDataFromDexScreener(mintAddress);
    if (data && data.price > 0) {
      this.setCache(mintAddress, data);
      return data;
    }

    return null;
  }

  /**
   * Cache management
   */
  getCached(mintAddress) {
    const cached = this.cache.get(mintAddress);
    if (!cached) return null;

    const age = Date.now() - cached.timestamp;
    if (age > this.cacheTimeout) {
      this.cache.delete(mintAddress);
      return null;
    }

    return cached.data;
  }

  setCache(mintAddress, data) {
    this.cache.set(mintAddress, {
      data,
      timestamp: Date.now()
    });

    // Limitar tamaño del cache
    if (this.cache.size > 100) {
      const firstKey = this.cache.keys().next().value;
      this.cache.delete(firstKey);
    }
  }

  /**
   * Actualizar precio de SOL desde CoinGecko (cada 60 segundos)
   */
  async updateSolPrice() {
    const now = Date.now();
    if (now - this.lastSolPriceUpdate < 60000) {
      return; // Ya actualizado recientemente
    }

    try {
      const response = await axios.get(
        'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd',
        { timeout: 3000 }
      );
      
      if (response.data.solana) {
        this.solPriceUSD = response.data.solana.usd;
        this.lastSolPriceUpdate = now;
        console.log(`💰 Precio SOL actualizado: $${this.solPriceUSD}`);
      }
    } catch (error) {
      // Usar precio previo si falla
      console.log(`⚠️ No se pudo actualizar precio SOL, usando $${this.solPriceUSD}`);
    }
  }

  /**
   * Obtener múltiples tokens en batch (más eficiente)
   */
  async getMultipleTokensData(mintAddresses) {
    const promises = mintAddresses.map(mint => 
      this.getTokenData(mint).catch(err => {
        console.error(`Error fetching ${mint.slice(0, 8)}: ${err.message}`);
        return null;
      })
    );

    const results = await Promise.allSettled(promises);
    
    return results
      .filter(r => r.status === 'fulfilled' && r.value !== null)
      .map(r => r.value);
  }

  /**
   * Monitorear cambios en el market cap en tiempo real
   */
  async watchTokenMarketCap(mintAddress, callback, intervalMs = 5000) {
    console.log(`👁️ Monitoreando ${mintAddress.slice(0, 8)}...`);
    
    let previousData = null;

    const monitor = setInterval(async () => {
      const currentData = await this.getTokenData(mintAddress);
      
      if (!currentData) {
        console.log(`⚠️ No se pudieron obtener datos, reintentando...`);
        return;
      }

      if (previousData) {
        const mcapChange = ((currentData.marketCap - previousData.marketCap) / previousData.marketCap) * 100;
        const priceChange = ((currentData.price - previousData.price) / previousData.price) * 100;

        callback({
          current: currentData,
          previous: previousData,
          changes: {
            marketCap: mcapChange,
            price: priceChange,
            liquidity: currentData.liquidity - previousData.liquidity
          }
        });
      }

      previousData = currentData;
    }, intervalMs);

    // Retornar función para detener el monitoreo
    return () => {
      clearInterval(monitor);
      console.log(`🛑 Monitoreo detenido para ${mintAddress.slice(0, 8)}`);
    };
  }

  /**
   * Obtener estadísticas del fetcher
   */
  getStats() {
    return {
      cacheSize: this.cache.size,
      queueSize: this.rateLimiter.getQueueSize(),
      requestsPerSecond: this.rateLimiter.getRequestCount(),
      solPrice: this.solPriceUSD,
      lastSolUpdate: new Date(this.lastSolPriceUpdate).toISOString()
    };
  }

  /**
   * Limpiar cache manualmente
   */
  clearCache() {
    const size = this.cache.size;
    this.cache.clear();
    console.log(`🧹 Cache limpiado (${size} entradas eliminadas)`);
  }

  /**
   * Verificar salud de la conexión RPC
   */
  async healthCheck() {
    try {
      const start = Date.now();
      await this.connection.getSlot();
      const latency = Date.now() - start;
      
      return {
        healthy: latency < 2000,
        latency,
        endpoint: this.connection.rpcEndpoint
      };
    } catch (error) {
      return {
        healthy: false,
        latency: -1,
        error: error.message
      };
    }
  }
}

// Función de utilidad para validar mint address
function isValidMintAddress(address) {
  try {
    new PublicKey(address);
    return true;
  } catch {
    return false;
  }
}

// Ejemplo de uso
async function example() {
  const fetcher = new PumpDataFetcher(process.env.HELIUS_RPC_URL);

  // Health check
  const health = await fetcher.healthCheck();
  console.log('🏥 RPC Health:', health);

  // Obtener datos de un token
  const mint = 'MINT_ADDRESS_AQUI';
  
  if (!isValidMintAddress(mint)) {
    console.error('❌ Mint address inválido');
    return;
  }

  const data = await fetcher.getTokenData(mint);
  
  if (data) {
    console.log(`\n📊 Datos del Token:`);
    console.log(`💵 Precio: $${data.price.toFixed(8)}`);
    console.log(`📈 Market Cap: $${data.marketCap.toLocaleString()}`);
    console.log(`💧 Liquidez: $${data.liquidity.toLocaleString()}`);
    console.log(`📊 Bonding Curve: ${data.bondingCurve}%`);
    console.log(`🔢 Supply: ${data.supply.toLocaleString()}`);
    console.log(`🔗 Fuente: ${data.source}`);
  }

  // Stats del fetcher
  console.log('\n📊 Fetcher Stats:', fetcher.getStats());

  // Monitorear cambios en tiempo real
  const stopMonitoring = await fetcher.watchTokenMarketCap(mint, (update) => {
    console.log(`\n🔄 Actualización:`);
    console.log(`Market Cap: $${update.current.marketCap.toLocaleString()} (${update.changes.marketCap >= 0 ? '+' : ''}${update.changes.marketCap.toFixed(2)}%)`);
    console.log(`Precio: $${update.current.price.toFixed(8)} (${update.changes.price >= 0 ? '+' : ''}${update.changes.price.toFixed(2)}%)`);
  });

  // Detener después de 1 minuto
  setTimeout(() => stopMonitoring(), 60000);
}

module.exports = PumpDataFetcher;

// Exportar utilidades
module.exports.isValidMintAddress = isValidMintAddress;
module.exports.PUMP_PROGRAM_ID = PUMP_PROGRAM_ID;

// Descomentar para probar
// if (require.main === module) {
//   example();
// }
