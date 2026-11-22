"""
Crowd Signal Detection Engine
Analyzes market prices to identify trading signals
Based on crowd consensus and sentiment
"""

import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from dataclasses import dataclass


@dataclass
class Signal:
    """Trading signal data"""
    market_id: str
    market_title: str
    outcome: str
    signal_type: str  # STRONG_BUY, BUY, NEUTRAL, SELL, STRONG_SELL
    confidence: float  # 0-1
    crowd_price: float  # Current market price
    timestamp: datetime
    reasoning: str


class CrowdSignalDetector:
    """
    Detects trading signals from crowd behavior
    Uses market prices as proxy for crowd consensus
    """
    
    def __init__(self):
        self.signal_history = []
        self.price_history = {}
    
    def analyze_market(self, market: Dict) -> List[Signal]:
        """
        Analyze a market and return signals
        
        Args:
            market: Market dict with outcomes and prices
            
        Returns:
            List of Signal objects
        """
        signals = []
        outcomes = market.get('outcomes', [])
        
        if len(outcomes) < 2:
            return signals
        
        market_id = market['id']
        market_title = market.get('question', 'Unknown')
        
        # Analyze each outcome
        for i, outcome in enumerate(outcomes):
            outcome_id = outcome['id']
            outcome_name = outcome['name']
            price = outcome.get('price', 0.5)
            
            # Generate signal based on price
            signal = self._price_to_signal(
                market_id, market_title, outcome_name, 
                price, outcome_id
            )
            
            if signal:
                signals.append(signal)
        
        return signals
    
    def _price_to_signal(self, market_id: str, market_title: str, 
                        outcome_name: str, price: float, 
                        outcome_id: str) -> Signal:
        """Convert market price to trading signal"""
        
        # Get price trend (if available)
        trend = self._get_price_trend(market_id, outcome_id, price)
        
        # Determine signal type and confidence
        if price >= 0.75:
            signal_type = "STRONG_BUY"
            confidence = min(price, 1.0)  # Higher price = higher confidence
            reasoning = f"Strong crowd consensus ({price:.0%}) for {outcome_name}"
        
        elif price >= 0.65:
            signal_type = "BUY"
            confidence = (price - 0.5) / 0.15
            reasoning = f"Moderate crowd belief ({price:.0%}) for {outcome_name}"
        
        elif price >= 0.55:
            signal_type = "NEUTRAL"
            confidence = 0.5
            reasoning = f"Slight consensus ({price:.0%}) for {outcome_name}"
        
        elif price >= 0.45:
            signal_type = "NEUTRAL"
            confidence = 0.5
            reasoning = f"No clear consensus ({price:.0%})"
        
        elif price >= 0.35:
            signal_type = "SELL"
            confidence = (0.5 - price) / 0.15
            reasoning = f"Crowd doubts {outcome_name} ({price:.0%})"
        
        else:
            signal_type = "STRONG_SELL"
            confidence = 1.0 - price
            reasoning = f"Strong crowd consensus against {outcome_name} ({price:.0%})"
        
        # Boost confidence if trend is strong
        if trend == "RISING":
            confidence = min(confidence * 1.2, 1.0)
            reasoning += " [RISING TREND 📈]"
        elif trend == "FALLING":
            confidence = min(confidence * 1.2, 1.0)
            reasoning += " [FALLING TREND 📉]"
        
        return Signal(
            market_id=market_id,
            market_title=market_title,
            outcome=outcome_name,
            signal_type=signal_type,
            confidence=confidence,
            crowd_price=price,
            timestamp=datetime.now(),
            reasoning=reasoning
        )
    
    def _get_price_trend(self, market_id: str, outcome_id: str, 
                        current_price: float) -> str:
        """Determine if price is rising or falling"""
        
        key = f"{market_id}_{outcome_id}"
        
        if key not in self.price_history:
            self.price_history[key] = [current_price]
            return None
        
        prices = self.price_history[key]
        
        if len(prices) >= 2:
            prev_price = prices[-1]
            
            if current_price > prev_price + 0.05:
                return "RISING"
            elif current_price < prev_price - 0.05:
                return "FALLING"
        
        # Keep recent price history
        prices.append(current_price)
        if len(prices) > 10:
            prices.pop(0)
        
        return None
    
    def get_top_signals(self, signals: List[Signal], 
                       min_confidence: float = 0.6,
                       signal_types: List[str] = None) -> List[Signal]:
        """
        Filter and rank signals
        
        Args:
            signals: All signals
            min_confidence: Minimum confidence threshold (0-1)
            signal_types: Filter by signal types (e.g., ['STRONG_BUY', 'BUY'])
            
        Returns:
            Filtered and sorted signals
        """
        
        if signal_types is None:
            signal_types = ['STRONG_BUY', 'BUY']
        
        # Filter
        filtered = [
            s for s in signals 
            if s.signal_type in signal_types 
            and s.confidence >= min_confidence
        ]
        
        # Sort by confidence (highest first)
        filtered.sort(key=lambda s: s.confidence, reverse=True)
        
        return filtered
    
    def format_signal_for_display(self, signal: Signal) -> str:
        """Format signal for display"""
        
        emoji_map = {
            'STRONG_BUY': '🟢🚀',
            'BUY': '🟢',
            'NEUTRAL': '🔵',
            'SELL': '🔴',
            'STRONG_SELL': '🔴🔻'
        }
        
        emoji = emoji_map.get(signal.signal_type, '⚪')
        
        return f"""
{emoji} *{signal.signal_type.replace('_', ' ')}*
Market: {signal.market_title}
Outcome: {signal.outcome}
Confidence: {signal.confidence:.0%}
Crowd Price: {signal.crowd_price:.0%}

💭 {signal.reasoning}
"""
    
    def detect_reversals(self, signals: List[Signal]) -> List[Tuple[str, str]]:
        """
        Detect potential market reversals
        (when price differs significantly from recent trend)
        """
        reversals = []
        
        for signal in signals:
            # If we have strong trend but weak price movement
            if signal.signal_type in ['STRONG_BUY', 'STRONG_SELL']:
                if 0.40 <= signal.crowd_price <= 0.60:
                    # Market is reversing - price doesn't match signal strength
                    reversals.append((
                        signal.market_title,
                        f"Potential reversal detected in {signal.outcome}"
                    ))
        
        return reversals
    
    def detect_arbitrage(self, market: Dict) -> List[Dict]:
        """
        Detect arbitrage opportunities
        (sum of probabilities != 1)
        """
        outcomes = market.get('outcomes', [])
        prices = [o.get('price', 0) for o in outcomes]
        total = sum(prices)
        
        if total < 0.95:  # Sum is less than 100%
            return [{
                'market': market['question'],
                'opportunity': 'Mispriced',
                'profit_margin': (1.0 - total) * 100,
                'action': 'Buy all outcomes below parity'
            }]
        elif total > 1.05:  # Sum is more than 100%
            return [{
                'market': market['question'],
                'opportunity': 'Overpriced',
                'profit_margin': (total - 1.0) * 100,
                'action': 'Short all outcomes above parity'
            }]
        
        return []


class VolumeBasedSignal:
    """
    Detect signals based on trading volume
    High volume = higher confidence in outcome
    """
    
    def __init__(self):
        self.volume_history = {}
    
    def analyze_volume(self, market: Dict) -> Dict:
        """Analyze trading volume for signals"""
        
        market_id = market['id']
        volume = market.get('volume', 0)
        trades = market.get('trades', 0)
        
        # High volume with consensus = strong signal
        outcomes = market.get('outcomes', [])
        prices = [o.get('price', 0) for o in outcomes]
        
        # Check for price divergence
        max_price = max(prices)
        min_price = min(prices)
        divergence = max_price - min_price
        
        if volume > 1000000 and divergence > 0.30:
            return {
                'type': 'HIGH_VOLUME_CONSENSUS',
                'strength': min(divergence * volume / 1000000, 1.0),
                'reasoning': 'High volume + high consensus = strong signal'
            }
        
        return None


# ===== EXAMPLE USAGE =====

def demo_signal_detection():
    """Demo the signal detection system"""
    
    detector = CrowdSignalDetector()
    
    # Simulate market data
    demo_market = {
        'id': 'mkt_demo',
        'question': 'Will Bitcoin hit $100k by end of 2024?',
        'volume': 5000000,
        'trades': 2500,
        'outcomes': [
            {
                'id': 'out_1',
                'name': 'Yes',
                'price': 0.72,  # 72% crowd thinks YES
            },
            {
                'id': 'out_2',
                'name': 'No',
                'price': 0.28,
            }
        ]
    }
    
    print("=" * 70)
    print("CROWD SIGNAL DETECTION DEMO")
    print("=" * 70)
    
    # Analyze market
    signals = detector.analyze_market(demo_market)
    
    print(f"\nMarket: {demo_market['question']}\n")
    
    for signal in signals:
        print(detector.format_signal_for_display(signal))
    
    # Get top signals
    print("\n" + "=" * 70)
    print("TOP SIGNALS (Confidence >= 70%)")
    print("=" * 70)
    
    top_signals = detector.get_top_signals(
        signals, 
        min_confidence=0.7, 
        signal_types=['STRONG_BUY', 'BUY']
    )
    
    if top_signals:
        for signal in top_signals:
            print(f"\n✅ {signal.signal_type}")
            print(f"   {signal.outcome}: {signal.confidence:.0%} confidence")
            print(f"   {signal.reasoning}")
    else:
        print("\nNo strong signals at this confidence level")
    
    # Check for reversals
    print("\n" + "=" * 70)
    print("REVERSAL DETECTION")
    print("=" * 70)
    
    reversals = detector.detect_reversals(signals)
    if reversals:
        for market_title, reversal in reversals:
            print(f"⚠️  {reversal}")
    else:
        print("No reversals detected")
    
    # Check for arbitrage
    print("\n" + "=" * 70)
    print("ARBITRAGE CHECK")
    print("=" * 70)
    
    arbitrage = detector.detect_arbitrage(demo_market)
    if arbitrage:
        for opp in arbitrage:
            print(f"💰 {opp['opportunity']}")
            print(f"   Profit margin: {opp['profit_margin']:.2f}%")
            print(f"   Action: {opp['action']}")
    else:
        print("No arbitrage opportunities")


if __name__ == "__main__":
    demo_signal_detection()
