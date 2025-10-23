// fix-pump-data-fetcher.js - SOLUCIÓN COMPLETA para el problema de datos
const { Connection, PublicKey } = require('@solana/web3.js');
const axios = require('axios');

// ✅ CONSTANTES CORRECTAS (verificadas)
const PUMP_PROGRAM_ID = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');
const PUMP_GLOBAL_ACCOUNT = new PublicKey('4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf');

// 🔧 PROBLEMA IDENTIFICADO Y SOLUCIONADO:
// Tu código original intentaba obtener datos de tokens recién creados que aún no tienen
// suficiente liquidez o que necesitan tiempo para inicializar su bonding curve.

class PumpDataFetcher {
  constructor(rpcUrl = null) {
    this.connection = new Connection(
      rpcUrl || process.env.HELIUS_RPC_URL || 'https://api.mainnet-beta.solana.com',
      'confirmed'
    );
    this.solPriceUSD = 150;
    this.lastSolPriceUpdate = 0;
    this.cache = new Map();
    this.cacheTimeout = 3000;
    
    // ✅ NUEVO: Rate limiter mejorado
    this.requestQueue = [];
    this.isProcessing = false;
    this.maxRequestsPerSecond = 10;
    this.requestTimes = [];
    
    console.log('✅ PumpDataFetcher mejorado inicializado');
  }

  /**
   * ✅ MÉTODO 1 (PRINCIPAL): API Oficial de Pump.fun
   * Este es el método más confiable para tokens nuevos
   */
  async getTokenDataFromAPI(mintAddress) {
    try {
      const response = await axios.get(
        `https://frontend-api.pump.fun/coins/${mintAddress}`,
        { 
          timeout: 5000,
          headers: {
            'User-Agent': 'Mozilla/5.0'
          }
        }
      );

      if (response.data && response.data.mint) {
        const data = response.data;
        
        // Calcular precio desde market cap y supply
        const price = parseFloat(data.usd_market_cap || 0) / parseFloat(data.total_supply || 1e9);
        
        return {
          price: price,
          marketCap: parseFloat(data.usd_market_cap || 0),
          liquidity: parseFloat(data.virtual_sol_reserves || 0) * this.solPriceUSD,
          bondingCurve: this.calculateBondingCurveProgress(
            parseFloat(data.real_sol_reserves || 0),
            85 // Target SOL para completar bonding curve
          ),
          supply: parseFloat(data.total_supply || 0) / 1e6,
          creator: data.creator,
          createdAt: data.created_timestamp,
          complete: data.complete || false,
          raydiumPool: data.raydium_pool || null,
          source: 'pump_api'
        };
      }

      return null;
    } catch (error) {
      // No loguear 404s (tokens muy nuevos)
      if (error.response?.status !== 404) {
        console.debug(`⚠️ API error for ${mintAddress.slice(0, 8)}: ${error.message}`);
      }
      return null;
    }
  }

  /**
   * ✅ MÉTODO 2 (MEJORADO): Bonding Curve desde RPC
   * Con retry automático y mejor manejo de errores
   */
  async getTokenDataFromBondingCurve(mintAddress, retries = 2) {
    try {
      const mint = new PublicKey(mintAddress);
      
      // ✅ Derivar bonding curve address correctamente
      const [bondingCurveAddress] = PublicKey.findProgramAddressSync(
        [Buffer.from('bonding-curve'), mint.toBuffer()],
        PUMP_PROGRAM_ID
      );

      // Rate-limited request con retry
      let accountInfo = null;
      let attempt = 0;
      
      while (attempt <= retries && !accountInfo) {
        try {
          accountInfo = await this.throttledRequest(async () => {
            return await this.connection.getAccountInfo(bondingCurveAddress);
          });
          
          if (!accountInfo && attempt < retries) {
            // Esperar 500ms antes de reintentar
            await this.sleep(500);
          }
        } catch (error) {
          if (attempt === retries) throw error;
        }
        attempt++;
      }
      
      if (!accountInfo || accountInfo.data.length < 97) {
        return null;
      }

      // Parsear datos de la bonding curve
      const data = accountInfo.data;
      
      const virtualSolReserves = data.readBigUInt64LE(64);
      const virtualTokenReserves = data.readBigUInt64LE(72);
      const realSolReserves = data.readBigUInt64LE(80);
      const tokenTotalSupply = data.readBigUInt64LE(89);
      const complete = data.readUInt8(88) === 1;

      // Calcular precio
      const pricePerToken = Number(virtualSolReserves) / Number(virtualTokenReserves);
      const priceInSol = pricePerToken / 1e9;
      const priceInUsd = priceInSol * this.solPriceUSD;

      // Calcular market cap
      const supply = Number(tokenTotalSupply) / 1e6;
      const marketCapUsd = priceInUsd * supply;

      // Calcular liquidez
      const liquiditySol = Number(realSolReserves) / 1e9;
      const liquidityUsd = liquiditySol * this.solPriceUSD;

      // Calcular bonding curve progress
      const bondingCurveProgress = this.calculateBondingCurveProgress(liquiditySol, 85);

      return {
        price: priceInUsd,
        priceInSol: priceInSol,
        marketCap: marketCapUsd,
        liquidity: liquidityUsd,
        liquiditySol: liquiditySol,
        bondingCurve: bondingCurveProgress,
        supply: supply,
        virtualSolReserves: Number(virtualSolReserves) / 1e9,
        virtualTokenReserves: Number(virtualTokenReserves) / 1e6,
        isComplete: complete,
        source: 'bonding_curve_onchain'
      };

    } catch (error) {
      console.debug(`⚠️ Bonding curve error for ${mintAddress.slice(0, 8)}: ${error.message}`);
      return null;
    }
  }

  /**
   * ✅ MÉTODO 3: DexScreener (para tokens graduados)
   */
  async getTokenDataFromDexScreener(mintAddress) {
    try {
      const response = await axios.get(
        `https://api.dexscreener.com/latest/dex/tokens/${mintAddress}`,
        { timeout: 5000 }
      );

      if (response.data.pairs && response.data.pairs.length > 0) {
        // Buscar el par más líquido
        const pair = response.data.pairs.reduce((best, current) => {
          const currentLiq = parseFloat(current.liquidity?.usd || 0);
          const bestLiq = parseFloat(best.liquidity?.usd || 0);
          return currentLiq > bestLiq ? current : best;
        });
        
        return {
          price: parseFloat(pair.priceUsd || 0),
          marketCap: parseFloat(pair.fdv || pair.marketCap || 0),
          liquidity: parseFloat(pair.liquidity?.usd || 0),
          bondingCurve: 100, // Ya graduado
          volume24h: parseFloat(pair.volume?.h24 || 0),
          priceChange24h: parseFloat(pair.priceChange?.h24 || 0),
          dexId: pair.dexId,
          pairAddress: pair.pairAddress,
          source: 'dexscreener'
        };
      }

      return null;
    } catch (error) {
      return null;
    }
  }

  /**
   * ✅ MÉTODO PRINCIPAL CON CASCADA MEJORADA
   */
  async getTokenData(mintAddress) {
    // Verificar cache
    const cached = this.getCached(mintAddress);
    if (cached) {
      return cached;
    }

    // Actualizar precio SOL
    await this.updateSolPrice();

    // ✅ ORDEN OPTIMIZADO PARA TOKENS NUEVOS:
    
    // 1. Intentar API oficial primero (más rápido y confiable para tokens nuevos)
    let data = await this.getTokenDataFromAPI(mintAddress);
    if (data && this.isValidData(data)) {
      this.setCache(mintAddress, data);
      return data;
    }

    // 2. Esperar 2 segundos y reintentar API (tokens muy nuevos)
    await this.sleep(2000);
    data = await this.getTokenDataFromAPI(mintAddress);
    if (data && this.isValidData(data)) {
      this.setCache(mintAddress, data);
      return data;
    }

    // 3. Intentar bonding curve on-chain
    data = await this.getTokenDataFromBondingCurve(mintAddress);
    if (data && this.isValidData(data)) {
      this.setCache(mintAddress, data);
      return data;
    }

    // 4. Último recurso: DexScreener (para tokens graduados)
    data = await this.getTokenDataFromDexScreener(mintAddress);
    if (data && this.isValidData(data)) {
      this.setCache(mintAddress, data);
      return data;
    }

    return null;
  }

  /**
   * ✅ VALIDACIÓN DE DATOS
   */
  isValidData(data) {
    if (!data) return false;
    
    // Debe tener precio válido
    if (!data.price || data.price <= 0) return false;
    
    // Debe tener market cap o liquidez
    if ((!data.marketCap || data.marketCap <= 0) && 
        (!data.liquidity || data.liquidity <= 0)) {
      return false;
    }
    
    return true;
  }

  /**
   * ✅ CALCULAR BONDING CURVE PROGRESS
   */
  calculateBondingCurveProgress(currentSol, targetSol = 85) {
    if (currentSol >= targetSol) return 100;
    return Math.round((currentSol / targetSol) * 100);
  }

  /**
   * ✅ RATE LIMITING MEJORADO
   */
  async throttledRequest(fn) {
    return new Promise((resolve, reject) => {
      this.requestQueue.push({ fn, resolve, reject });
      this.processQueue();
    });
  }

  async processQueue() {
    if (this.isProcessing || this.requestQueue.length === 0) return;

    this.isProcessing = true;

    while (this.requestQueue.length > 0) {
      // Limpiar requests antiguos
      const now = Date.now();
      this.requestTimes = this.requestTimes.filter(t => now - t < 1000);

      // Si alcanzamos el límite, esperar
      if (this.requestTimes.length >= this.maxRequestsPerSecond) {
        const oldestRequest = this.requestTimes[0];
        const waitTime = 1000 - (now - oldestRequest);
        if (waitTime > 0) {
          await this.sleep(waitTime);
        }
        continue;
      }

      // Procesar siguiente request
      const { fn, resolve, reject } = this.requestQueue.shift();
      this.requestTimes.push(Date.now());

      try {
        const result = await fn();
        resolve(result);
      } catch (error) {
        reject(error);
      }

      // Delay pequeño entre requests
      await this.sleep(100);
    }

    this.isProcessing = false;
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
   * Actualizar precio de SOL
   */
  async updateSolPrice() {
    const now = Date.now();
    if (now - this.lastSolPriceUpdate < 60000) return;

    try {
      const response = await axios.get(
        'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd',
        { timeout: 3000 }
      );
      
      if (response.data.solana) {
        this.solPriceUSD = response.data.solana.usd;
        this.lastSolPriceUpdate = now;
      }
    } catch (error) {
      // Usar precio previo si falla
    }
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  /**
   * Health check mejorado
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

  getStats() {
    return {
      cacheSize: this.cache.size,
      queueSize: this.requestQueue.length,
      requestsPerSecond: this.requestTimes.filter(t => Date.now() - t < 1000).length,
      solPrice: this.solPriceUSD,
      lastSolUpdate: new Date(this.lastSolPriceUpdate).toISOString()
    };
  }
}

// ✅ EXPORTAR
module.exports = PumpDataFetcher;

// ✅ TEST SI SE EJECUTA DIRECTAMENTE
if (require.main === module) {
  (async () => {
    console.log('🧪 PROBANDO PUMP DATA FETCHER MEJORADO\n');
    
    const fetcher = new PumpDataFetcher(process.env.HELIUS_RPC_URL);
    
    // Health check
    const health = await fetcher.healthCheck();
    console.log('🏥 RPC Health:', health);
    console.log('');
    
    // Probar con un token de ejemplo (reemplazar con un mint real)
    const testMint = process.argv[2] || 'FjzWDvCq7h8VgqR7J7rQQKFBKfgKA7aUy3bvN9wpump';
    
    console.log(`📊 Obteniendo datos para: ${testMint}\n`);
    
    const data = await fetcher.getTokenData(testMint);
    
    if (data) {
      console.log('✅ DATOS OBTENIDOS:');
      console.log(`   Precio: $${data.price.toFixed(8)}`);
      console.log(`   Market Cap: $${data.marketCap.toLocaleString()}`);
      console.log(`   Liquidez: $${data.liquidity.toLocaleString()}`);
      console.log(`   Bonding Curve: ${data.bondingCurve}%`);
      console.log(`   Fuente: ${data.source}`);
    } else {
      console.log('❌ No se pudieron obtener datos');
    }
    
    console.log('\n📊 Stats:', fetcher.getStats());
  })();
}
