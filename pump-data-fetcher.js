// pump-data-fetcher.js - La forma CORRECTA de obtener market cap de Pump.fun
const { Connection, PublicKey } = require('@solana/web3.js');
const axios = require('axios');

// Constantes de Pump.fun
const PUMP_PROGRAM_ID = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');
const PUMP_GLOBAL_ACCOUNT = new PublicKey('4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf');
const PUMP_FEE_RECIPIENT = new PublicKey('CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM');

// Estructura de la Bonding Curve Account
const BONDING_CURVE_LAYOUT_SIZE = 97;
const VIRTUAL_SOL_OFFSET = 64;
const VIRTUAL_TOKEN_OFFSET = 72;
const REAL_SOL_OFFSET = 80;
const TOKEN_SUPPLY_OFFSET = 89;

class PumpDataFetcher {
  constructor(rpcUrl = null) {
    this.connection = new Connection(
      rpcUrl || process.env.HELIUS_RPC_URL || 'https://api.mainnet-beta.solana.com',
      'confirmed'
    );
    this.solPriceUSD = 150; // Actualizar dinámicamente
  }

  /**
   * MÉTODO 1: Obtener datos desde la Bonding Curve Account (MÁS CONFIABLE)
   * SIN usar @coral-xyz/borsh - parsing manual más rápido
   */
  async getTokenDataFromBondingCurve(mintAddress) {
    try {
      const mint = new PublicKey(mintAddress);
      
      // Derivar la bonding curve account address
      const [bondingCurveAddress] = PublicKey.findProgramAddressSync(
        [Buffer.from('bonding-curve'), mint.toBuffer()],
        PUMP_GLOBAL_ACCOUNT
      );

      // Obtener datos de la cuenta
      const accountInfo = await this.connection.getAccountInfo(bondingCurveAddress);
      
      if (!accountInfo || accountInfo.data.length < BONDING_CURVE_LAYOUT_SIZE) {
        console.log(`❌ No bonding curve found for ${mintAddress.slice(0, 8)}`);
        return null;
      }

      // Parsear datos MANUALMENTE (sin Borsh - más rápido y sin dependencias)
      const data = accountInfo.data;
      
      // Leer valores directamente del buffer
      const virtualSolReserves = data.readBigUInt64LE(VIRTUAL_SOL_OFFSET);
      const virtualTokenReserves = data.readBigUInt64LE(VIRTUAL_TOKEN_OFFSET);
      const realSolReserves = data.readBigUInt64LE(REAL_SOL_OFFSET);
      const tokenTotalSupply = data.readBigUInt64LE(TOKEN_SUPPLY_OFFSET);
      const complete = data.readUInt8(88) === 1;

      // CALCULAR PRECIO (en lamports por token)
      const pricePerToken = Number(virtualSolReserves) / Number(virtualTokenReserves);
      const priceInSol = pricePerToken / 1e9; // Convertir lamports a SOL
      const priceInUsd = priceInSol * this.solPriceUSD;

      // CALCULAR MARKET CAP
      const supply = Number(tokenTotalSupply) / 1e6; // Ajustar por decimales (típicamente 6)
      const marketCapUsd = priceInUsd * supply;

      // CALCULAR LIQUIDEZ (SOL real en el pool)
      const liquiditySol = Number(realSolReserves) / 1e9;
      const liquidityUsd = liquiditySol * this.solPriceUSD;

      // CALCULAR BONDING CURVE PROGRESS
      // Pump.fun migra a Raydium cuando alcanza ~85 SOL
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
      console.error(`Pump.fun API error: ${error.message}`);
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
      console.error(`DexScreener error: ${error.message}`);
      return null;
    }
  }

  /**
   * MÉTODO PRINCIPAL: Intenta todos los métodos en cascada
   */
  async getTokenData(mintAddress) {
    // Actualizar precio SOL
    await this.updateSolPrice();

    // 1. Intentar bonding curve (más confiable)
    let data = await this.getTokenDataFromBondingCurve(mintAddress);
    if (data && data.price > 0) {
      console.log(`✅ Datos obtenidos desde bonding curve on-chain`);
      return data;
    }

    // 2. Intentar API de Pump.fun
    data = await this.getTokenDataFromAPI(mintAddress);
    if (data && data.price > 0) {
      console.log(`✅ Datos obtenidos desde Pump.fun API`);
      return data;
    }

    // 3. Último recurso: DexScreener
    data = await this.getTokenDataFromDexScreener(mintAddress);
    if (data && data.price > 0) {
      console.log(`✅ Datos obtenidos desde DexScreener`);
      return data;
    }

    console.log(`❌ No se pudieron obtener datos para ${mintAddress.slice(0, 8)}`);
    return null;
  }

  /**
   * Actualizar precio de SOL desde CoinGecko
   */
  async updateSolPrice() {
    try {
      const response = await axios.get(
        'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd',
        { timeout: 3000 }
      );
      
      if (response.data.solana) {
        this.solPriceUSD = response.data.solana.usd;
        console.log(`💰 Precio SOL actualizado: $${this.solPriceUSD}`);
      }
    } catch (error) {
      console.log(`⚠️ No se pudo actualizar precio SOL, usando $${this.solPriceUSD}`);
    }
  }

  /**
   * Obtener múltiples tokens en batch (más eficiente)
   */
  async getMultipleTokensData(mintAddresses) {
    const promises = mintAddresses.map(mint => 
      this.getTokenData(mint).catch(err => {
        console.error(`Error fetching ${mint}: ${err.message}`);
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
}

// Ejemplo de uso
async function example() {
  const fetcher = new PumpDataFetcher(process.env.HELIUS_RPC_URL);

  // Obtener datos de un token
  const mint = 'MINT_ADDRESS_AQUI';
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

// Descomentar para probar
// example();
