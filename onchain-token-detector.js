// onchain-token-detector.js - Detectar nuevos tokens desde eventos on-chain
const { Connection, PublicKey } = require('@solana/web3.js');

const PUMP_PROGRAM_ID = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');

class OnChainTokenDetector {
  constructor(rpcUrl, onNewToken) {
    this.connection = new Connection(rpcUrl, {
      commitment: 'confirmed',
      wsEndpoint: rpcUrl.replace('https://', 'wss://').replace('http://', 'ws://')
    });
    this.onNewToken = onNewToken;
    this.subscriptionId = null;
    this.processedSignatures = new Set();
    this.maxCacheSize = 1000;
  }

  /**
   * Iniciar monitoreo de logs del programa Pump
   */
  async start() {
    try {
      console.log('🔍 Iniciando monitoreo on-chain de Pump.fun...');
      
      this.subscriptionId = this.connection.onLogs(
        PUMP_PROGRAM_ID,
        (logs, context) => {
          this.handleLogs(logs, context);
        },
        'confirmed'
      );
      
      console.log(`✅ Suscrito a logs de ${PUMP_PROGRAM_ID.toString()}`);
      console.log(`   Subscription ID: ${this.subscriptionId}`);
      
      return true;
    } catch (error) {
      console.error('❌ Error iniciando monitoreo on-chain:', error.message);
      return false;
    }
  }

  /**
   * Detener monitoreo
   */
  async stop() {
    if (this.subscriptionId) {
      try {
        await this.connection.removeOnLogsListener(this.subscriptionId);
        console.log('🛑 Monitoreo on-chain detenido');
      } catch (error) {
        console.error('Error deteniendo monitoreo:', error.message);
      }
    }
  }

  /**
   * Procesar logs de transacciones
   */
  async handleLogs(logs, context) {
    try {
      const signature = logs.signature;
      
      // Evitar procesar la misma tx dos veces
      if (this.processedSignatures.has(signature)) {
        return;
      }
      
      this.processedSignatures.add(signature);
      
      // Limpiar cache si crece mucho
      if (this.processedSignatures.size > this.maxCacheSize) {
        const toDelete = Array.from(this.processedSignatures).slice(0, 100);
        toDelete.forEach(sig => this.processedSignatures.delete(sig));
      }
      
      // Verificar si hay error
      if (logs.err) {
        return;
      }
      
      // Buscar event de creación de token
      const createEvent = logs.logs.find(log => 
        log.includes('CreateEvent') || 
        log.includes('Program log: create') ||
        log.includes('initialized')
      );
      
      if (!createEvent) {
        return;
      }
      
      // Obtener detalles de la transacción
      const txDetails = await this.fetchTransactionDetails(signature);
      
      if (txDetails) {
        await this.onNewToken(txDetails);
      }
      
    } catch (error) {
      console.error(`Error procesando logs:`, error.message);
    }
  }

  /**
   * Obtener detalles completos de una transacción
   */
  async fetchTransactionDetails(signature) {
    try {
      const tx = await this.connection.getParsedTransaction(signature, {
        maxSupportedTransactionVersion: 0,
        commitment: 'confirmed'
      });
      
      if (!tx || !tx.meta || !tx.transaction) {
        return null;
      }
      
      // Buscar el mint del token creado
      const postTokenBalances = tx.meta.postTokenBalances || [];
      const preTokenBalances = tx.meta.preTokenBalances || [];
      
      // Token nuevo = existe en post pero no en pre
      const newToken = postTokenBalances.find(post => 
        !preTokenBalances.some(pre => pre.mint === post.mint)
      );
      
      if (!newToken) {
        return null;
      }
      
      const mint = newToken.mint;
      
      // Buscar metadata en los logs
      let name = null;
      let symbol = null;
      let uri = null;
      
      const logs = tx.meta.logMessages || [];
      for (const log of logs) {
        // Parsear logs buscando metadata (esto depende del formato de logs de Pump)
        if (log.includes('name:')) {
          name = this.extractFromLog(log, 'name:');
        }
        if (log.includes('symbol:')) {
          symbol = this.extractFromLog(log, 'symbol:');
        }
        if (log.includes('uri:')) {
          uri = this.extractFromLog(log, 'uri:');
        }
      }
      
      // Obtener creator
      const creator = tx.transaction.message.accountKeys[0].pubkey.toString();
      
      return {
        mint,
        name: name || 'Unknown',
        symbol: symbol || 'UNKNOWN',
        uri: uri || '',
        creator,
        signature,
        slot: tx.slot,
        blockTime: tx.blockTime,
        source: 'onchain_logs'
      };
      
    } catch (error) {
      console.error(`Error obteniendo detalles de tx ${signature}:`, error.message);
      return null;
    }
  }

  /**
   * Extraer valor de un log
   */
  extractFromLog(log, key) {
    const start = log.indexOf(key);
    if (start === -1) return null;
    
    const value = log.substring(start + key.length).trim();
    // Remover comillas y espacios
    return value.replace(/['"]/g, '').split(/[\s,]/)[0];
  }

  /**
   * Método alternativo: Polling de signatures recientes
   */
  async pollRecentSignatures(intervalMs = 5000) {
    let lastSignature = null;
    
    const poll = async () => {
      try {
        const signatures = await this.connection.getSignaturesForAddress(
          PUMP_PROGRAM_ID,
          {
            limit: 10,
            before: lastSignature
          }
        );
        
        if (signatures.length > 0) {
          lastSignature = signatures[0].signature;
          
          // Procesar en orden inverso (más antiguos primero)
          for (const sig of signatures.reverse()) {
            if (!this.processedSignatures.has(sig.signature)) {
              const txDetails = await this.fetchTransactionDetails(sig.signature);
              if (txDetails) {
                await this.onNewToken(txDetails);
              }
              this.processedSignatures.add(sig.signature);
            }
          }
        }
        
      } catch (error) {
        console.error('Error en polling:', error.message);
      }
      
      setTimeout(poll, intervalMs);
    };
    
    poll();
  }

  /**
   * Health check
   */
  async healthCheck() {
    try {
      const slot = await this.connection.getSlot();
      return {
        healthy: true,
        slot,
        subscribed: this.subscriptionId !== null
      };
    } catch (error) {
      return {
        healthy: false,
        error: error.message
      };
    }
  }
}

module.exports = OnChainTokenDetector;
