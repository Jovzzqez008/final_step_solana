// wallet-loader.js - Cargar wallet desde variable de entorno o archivo
const { Keypair } = require('@solana/web3.js');
const bs58 = require('bs58');
const fs = require('fs');

/**
 * Carga el keypair de Solana desde múltiples fuentes:
 * 1. Variable de entorno SOLANA_PRIVATE_KEY (base58 o JSON array)
 * 2. Archivo JSON en SOLANA_WALLET_PATH
 * 
 * Esto es perfecto para Railway/Heroku/Vercel donde usas variables de entorno
 */
function loadWalletKeypair() {
  // PRIORIDAD 1: Variable de entorno SOLANA_PRIVATE_KEY (Railway/Heroku)
  if (process.env.SOLANA_PRIVATE_KEY) {
    console.log('🔑 Cargando wallet desde SOLANA_PRIVATE_KEY...');
    
    try {
      const privateKeyStr = process.env.SOLANA_PRIVATE_KEY.trim();
      
      // Caso 1: Array JSON como string "[1,2,3,...]"
      if (privateKeyStr.startsWith('[')) {
        const keypairArray = JSON.parse(privateKeyStr);
        if (!Array.isArray(keypairArray) || keypairArray.length !== 64) {
          throw new Error('El array debe tener 64 elementos');
        }
        const keypair = Keypair.fromSecretKey(Uint8Array.from(keypairArray));
        console.log('✅ Wallet cargada desde SOLANA_PRIVATE_KEY (JSON array)');
        console.log(`📍 Dirección: ${keypair.publicKey.toString()}`);
        return keypair;
      }
      
      // Caso 2: Clave privada en base58 (de Phantom/Solflare)
      else {
        const secretKey = bs58.decode(privateKeyStr);
        if (secretKey.length !== 64) {
          throw new Error('La clave base58 debe decodificar a 64 bytes');
        }
        const keypair = Keypair.fromSecretKey(secretKey);
        console.log('✅ Wallet cargada desde SOLANA_PRIVATE_KEY (base58)');
        console.log(`📍 Dirección: ${keypair.publicKey.toString()}`);
        return keypair;
      }
      
    } catch (error) {
      console.error('❌ Error cargando SOLANA_PRIVATE_KEY:', error.message);
      throw error;
    }
  }

  // PRIORIDAD 2: Archivo JSON (desarrollo local)
  if (process.env.SOLANA_WALLET_PATH) {
    console.log('🔑 Cargando wallet desde archivo...');
    
    try {
      if (!fs.existsSync(process.env.SOLANA_WALLET_PATH)) {
        throw new Error(`Archivo no encontrado: ${process.env.SOLANA_WALLET_PATH}`);
      }

      const keypairData = fs.readFileSync(process.env.SOLANA_WALLET_PATH, 'utf8');
      const keypairArray = JSON.parse(keypairData);
      
      if (!Array.isArray(keypairArray) || keypairArray.length !== 64) {
        throw new Error('El archivo debe contener un array de 64 elementos');
      }

      const keypair = Keypair.fromSecretKey(Uint8Array.from(keypairArray));
      console.log('✅ Wallet cargada desde archivo');
      console.log(`📍 Dirección: ${keypair.publicKey.toString()}`);
      return keypair;
      
    } catch (error) {
      console.error('❌ Error cargando archivo wallet:', error.message);
      throw error;
    }
  }

  // Ninguna fuente disponible
  throw new Error(
    'No se encontró wallet. Configura SOLANA_PRIVATE_KEY o SOLANA_WALLET_PATH'
  );
}

/**
 * Genera el JSON array desde una clave privada base58
 * Útil para convertir tu clave de Phantom a formato para Railway
 */
function convertBase58ToJSON(base58PrivateKey) {
  try {
    const secretKey = bs58.decode(base58PrivateKey);
    if (secretKey.length !== 64) {
      throw new Error('Clave inválida: debe decodificar a 64 bytes');
    }
    
    const keypairArray = Array.from(secretKey);
    const jsonString = JSON.stringify(keypairArray);
    
    console.log('\n✅ Conversión exitosa!');
    console.log('\n📋 Copia este JSON y úsalo como SOLANA_PRIVATE_KEY en Railway:');
    console.log('─'.repeat(70));
    console.log(jsonString);
    console.log('─'.repeat(70));
    
    // Verificar que funciona
    const keypair = Keypair.fromSecretKey(secretKey);
    console.log(`\n📍 Dirección de la wallet: ${keypair.publicKey.toString()}`);
    
    return jsonString;
    
  } catch (error) {
    console.error('❌ Error en conversión:', error.message);
    throw error;
  }
}

/**
 * Exportar la clave privada de un archivo JSON a base58
 * (Por si necesitas importarla a Phantom)
 */
function exportToBase58(walletPath) {
  try {
    const keypairData = fs.readFileSync(walletPath, 'utf8');
    const keypairArray = JSON.parse(keypairData);
    const keypair = Keypair.fromSecretKey(Uint8Array.from(keypairArray));
    const base58Key = bs58.encode(keypair.secretKey);
    
    console.log('\n✅ Exportación exitosa!');
    console.log('\n🔑 Clave privada en base58 (para importar a Phantom):');
    console.log('─'.repeat(70));
    console.log(base58Key);
    console.log('─'.repeat(70));
    console.log(`\n📍 Dirección: ${keypair.publicKey.toString()}`);
    console.log('\n⚠️ NUNCA compartas esta clave!');
    
    return base58Key;
    
  } catch (error) {
    console.error('❌ Error exportando:', error.message);
    throw error;
  }
}

module.exports = {
  loadWalletKeypair,
  convertBase58ToJSON,
  exportToBase58
};

// CLI para conversiones
if (require.main === module) {
  const args = process.argv.slice(2);
  
  if (args[0] === 'convert' && args[1]) {
    // Convertir base58 a JSON
    // node wallet-loader.js convert TU_CLAVE_BASE58_AQUI
    convertBase58ToJSON(args[1]);
    
  } else if (args[0] === 'export' && args[1]) {
    // Exportar archivo JSON a base58
    // node wallet-loader.js export ./my-wallet.json
    exportToBase58(args[1]);
    
  } else if (args[0] === 'test') {
    // Probar carga
    // node wallet-loader.js test
    try {
      const keypair = loadWalletKeypair();
      console.log('\n✅ Test exitoso - Wallet cargada correctamente!');
    } catch (error) {
      console.error('\n❌ Test fallido:', error.message);
      console.log('\n💡 Configura SOLANA_PRIVATE_KEY o SOLANA_WALLET_PATH');
    }
    
  } else {
    console.log(`
🔑 Wallet Loader - Utilidad para manejar claves de Solana

COMANDOS:

  test
    Probar carga de wallet desde variables de entorno
    
  convert <CLAVE_BASE58>
    Convertir clave base58 a JSON array (para Railway)
    
  export <ARCHIVO.json>
    Exportar archivo JSON a base58 (para Phantom)

EJEMPLOS:

  # Probar carga
  node wallet-loader.js test
  
  # Convertir de Phantom a Railway
  node wallet-loader.js convert 3KSeWGZ...ABC123
  
  # Exportar para Phantom
  node wallet-loader.js export ./my-wallet.json

VARIABLES DE ENTORNO:

  SOLANA_PRIVATE_KEY   → Clave privada (JSON array o base58)
  SOLANA_WALLET_PATH   → Ruta a archivo JSON (fallback)
    `);
  }
}
