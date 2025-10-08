# token_pattern_analyzer.py
import asyncio
import httpx
import time
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import statistics

async def deep_analyze_token(token_address: str):
    """Analiza en profundidad un token para extraer patrones."""
    print(f"🔍 ANALIZANDO PATRÓN DEL TOKEN: {token_address}")
    print("=" * 60)
    
    try:
        # 1. Obtener datos actuales del token
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                pairs = data.get('pairs', [])
                
                if not pairs:
                    print("❌ No se encontraron pairs para este token")
                    return
                
                # Tomar el pair principal (mayor liquidez)
                main_pair = max(pairs, key=lambda x: float(x.get('liquidity', {}).get('usd', 0)))
                base_token = main_pair.get('baseToken', {})
                
                print(f"📛 Símbolo: {base_token.get('symbol', 'N/A')}")
                print(f"📝 Nombre: {base_token.get('name', 'N/A')}")
                print(f"💰 Precio actual: ${float(main_pair.get('priceUsd', 0)):.8f}")
                print(f"📊 Liquidez: ${float(main_pair.get('liquidity', {}).get('usd', 0)):,.2f}")
                print(f"📈 Volumen 24h: ${float(main_pair.get('volume', {}).get('h24', 0)):,.2f}")
                
                # Información de creación
                created_at = main_pair.get('pairCreatedAt')
                if created_at:
                    created_date = datetime.fromtimestamp(created_at / 1000)
                    age_days = (datetime.now() - created_date).days
                    print(f"🕐 Edad del pair: {age_days} días")
                    print(f"📅 Creado: {created_date}")
                
                # Análisis de cambios de precio
                price_change_5m = main_pair.get('priceChange', {}).get('m5', 0)
                price_change_1h = main_pair.get('priceChange', {}).get('h1', 0)
                price_change_6h = main_pair.get('priceChange', {}).get('h6', 0)
                price_change_24h = main_pair.get('priceChange', {}).get('h24', 0)
                
                print(f"📊 Cambios de precio:")
                print(f"   • 5min: {price_change_5m}%")
                print(f"   • 1h: {price_change_1h}%") 
                print(f"   • 6h: {price_change_6h}%")
                print(f"   • 24h: {price_change_24h}%")
                
                # 2. Obtener datos históricos (simulados - en producción usar API histórica)
                await analyze_price_patterns(main_pair)
                
                # 3. Recomendaciones de parámetros basados en el análisis
                await generate_parameter_recommendations(main_pair)
                
            else:
                print(f"❌ Error API: {response.status_code}")
                
    except Exception as e:
        print(f"❌ Error analizando token: {e}")

async def analyze_price_patterns(pair_data: Dict):
    """Analiza patrones de precio basado en los datos disponibles."""
    print("\n📈 ANÁLISIS DE PATRONES:")
    print("-" * 40)
    
    # Simular análisis de consolidación basado en cambios recientes
    price_changes = {
        '5m': float(pair_data.get('priceChange', {}).get('m5', 0)),
        '1h': float(pair_data.get('priceChange', {}).get('h1', 0)),
        '6h': float(pair_data.get('priceChange', {}).get('h6', 0)),
        '24h': float(pair_data.get('priceChange', {}).get('h24', 0))
    }
    
    # Detectar posibles patrones de consolidación
    consolidation_signals = []
    
    # Patrón 1: Baja volatilidad reciente con movimiento histórico
    if abs(price_changes['5m']) < 2 and abs(price_changes['1h']) < 3:
        consolidation_signals.append("Posible consolidación reciente (baja volatilidad 1h)")
    
    # Patrón 2: Movimiento significativo después de periodo plano
    if abs(price_changes['24h']) > 10 and abs(price_changes['6h']) < 5:
        consolidation_signals.append("Posible breakout después de consolidación")
    
    # Patrón 3: Volumen bajo reciente con precio estable
    volume_24h = float(pair_data.get('volume', {}).get('h24', 0))
    if volume_24h > 10000:  # Token con volumen decente
        consolidation_signals.append("Token con volumen significativo - buen candidato")
    
    if consolidation_signals:
        print("✅ Señales de consolidación detectadas:")
        for signal in consolidation_signals:
            print(f"   • {signal}")
    else:
        print("❌ No se detectaron patrones claros de consolidación")

async def generate_parameter_recommendations(pair_data: Dict):
    """Genera recomendaciones de parámetros basadas en el análisis."""
    print("\n🎯 RECOMENDACIONES DE PARÁMETROS:")
    print("-" * 40)
    
    price_changes = {
        '5m': abs(float(pair_data.get('priceChange', {}).get('m5', 0))),
        '1h': abs(float(pair_data.get('priceChange', {}).get('h1', 0))),
        '6h': abs(float(pair_data.get('priceChange', {}).get('h6', 0))),
        '24h': abs(float(pair_data.get('priceChange', {}).get('h24', 0)))
    }
    
    liquidity = float(pair_data.get('liquidity', {}).get('usd', 0))
    volume_24h = float(pair_data.get('volume', {}).get('h24', 0))
    
    # Recomendaciones basadas en volatilidad
    avg_volatility = statistics.mean([price_changes['5m'], price_changes['1h'], price_changes['6h']])
    
    print("📊 Basado en el análisis de este token:")
    
    if avg_volatility < 3:
        print("   • Token de BAJA volatilidad")
        print("   → CONSOLIDATION_THRESHOLD: 1.5%")
        print("   → BREAKOUT_PERCENT: 4-5%")
    elif avg_volatility < 8:
        print("   • Token de VOLATILIDAD MODERADA") 
        print("   → CONSOLIDATION_THRESHOLD: 2.5%")
        print("   → BREAKOUT_PERCENT: 6-7%")
    else:
        print("   • Token de ALTA volatilidad")
        print("   → CONSOLIDATION_THRESHOLD: 4.0%")
        print("   → BREAKOUT_PERCENT: 8-10%")
    
    # Recomendaciones basadas en liquidez
    if liquidity < 50000:
        print("   • Liquidez BAJA - mayor riesgo")
        print("   → MIN_LIQUIDITY: $25,000")
    elif liquidity < 200000:
        print("   • Liquidez MODERADA")
        print("   → MIN_LIQUIDITY: $50,000")
    else:
        print("   • Liquidez ALTA - más confiable")
        print("   → MIN_LIQUIDITY: $100,000")
    
    # Recomendación general
    print(f"\n💡 PARÁMETROS SUGERIDOS PARA TOKENS SIMILARES:")
    print(f"   • PRICE_INTERVAL: 5-10 minutos")
    print(f"   • CONSOLIDATION_HOURS: 6-24 horas")
    print(f"   • BREAKOUT_PERCENT: {max(4, min(10, int(avg_volatility) + 2))}%")
    print(f"   • VOLUME_SPIKE_MULTIPLIER: 2.0-3.0x")

# Ejecutar análisis del token específico
asyncio.run(deep_analyze_token("H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump"))
