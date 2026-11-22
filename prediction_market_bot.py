"""
Prediction market strategy simulator.
Implements LMSR pricing, Bayesian updating, and example trading strategies.
Bayse market data can be adapted into these in-memory models, but this module
does not connect to Bayse or execute live orders.
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import json


@dataclass
class Market:
    """Represents a prediction market"""
    id: str
    question: str
    outcomes: List[str]
    liquidity_param: float  # 'b' parameter in LMSR
    quantities: np.ndarray  # Outstanding shares for each outcome
    created_at: datetime
    resolves_at: datetime
    resolved: bool = False
    winning_outcome: Optional[int] = None


@dataclass
class Position:
    """Represents a trader's position in a market"""
    market_id: str
    shares: np.ndarray  # Shares held for each outcome
    cost_basis: float  # Total amount paid


class LMSRPricer:
    """
    Logarithmic Market Scoring Rule (LMSR) implementation
    Based on the formula: C(q) = b · ln(Σ e^(qi/b))
    """
    
    @staticmethod
    def cost_function(quantities: np.ndarray, b: float) -> float:
        """
        Calculate the cost function C(q) for given quantities
        
        Args:
            quantities: Array of outstanding shares for each outcome
            b: Liquidity parameter (higher = more liquid, tighter spreads)
        
        Returns:
            Total cost
        """
        return b * np.log(np.sum(np.exp(quantities / b)))
    
    @staticmethod
    def get_prices(quantities: np.ndarray, b: float) -> np.ndarray:
        """
        Calculate instantaneous prices using softmax
        
        Args:
            quantities: Array of outstanding shares
            b: Liquidity parameter
            
        Returns:
            Array of prices for each outcome (sum = 1)
        """
        exp_q = np.exp(quantities / b)
        return exp_q / np.sum(exp_q)
    
    @staticmethod
    def calculate_trade_cost(current_q: np.ndarray, trade_delta: np.ndarray, b: float) -> float:
        """
        Calculate cost of moving from current quantities to new quantities
        
        Args:
            current_q: Current outstanding shares
            trade_delta: Change in shares (positive = buy, negative = sell)
            b: Liquidity parameter
            
        Returns:
            Cost of the trade (positive = cost, negative = payout)
        """
        new_q = current_q + trade_delta
        return LMSRPricer.cost_function(new_q, b) - LMSRPricer.cost_function(current_q, b)
    
    @staticmethod
    def max_loss(b: float, n_outcomes: int) -> float:
        """Calculate maximum market maker loss"""
        return b * np.log(n_outcomes)


class BayesianUpdater:
    """
    Bayesian belief updating based on new information
    P(H|D) = P(D|H) · P(H) / P(D)
    """
    
    @staticmethod
    def update_belief(prior: np.ndarray, likelihood: np.ndarray) -> np.ndarray:
        """
        Update beliefs using Bayes' theorem
        
        Args:
            prior: Prior probability distribution
            likelihood: Likelihood of data given each outcome
            
        Returns:
            Posterior probability distribution (normalized)
        """
        posterior = prior * likelihood
        return posterior / np.sum(posterior)
    
    @staticmethod
    def sequential_update(prior: np.ndarray, likelihoods: List[np.ndarray]) -> np.ndarray:
        """
        Apply multiple sequential Bayesian updates
        
        Args:
            prior: Initial belief
            likelihoods: List of likelihood vectors
            
        Returns:
            Final posterior after all updates
        """
        belief = prior.copy()
        for likelihood in likelihoods:
            belief = BayesianUpdater.update_belief(belief, likelihood)
        return belief


class TradingStrategy:
    """Base class for trading strategies"""
    
    def should_trade(self, market: Market, belief: np.ndarray, 
                    current_position: Optional[Position]) -> Tuple[bool, np.ndarray, str]:
        """
        Determine if a trade should be made
        
        Returns:
            (should_trade, trade_vector, reason)
        """
        raise NotImplementedError


class KellyStrategy(TradingStrategy):
    """
    Kelly Criterion strategy - optimal betting based on edge
    Fraction to bet = (p·b - (1-p)) / b
    where p = true probability, b = odds
    """
    
    def __init__(self, kelly_fraction: float = 0.5):
        """
        Args:
            kelly_fraction: Fraction of full Kelly to use (0.5 = half Kelly for safety)
        """
        self.kelly_fraction = kelly_fraction
    
    def should_trade(self, market: Market, belief: np.ndarray, 
                    current_position: Optional[Position]) -> Tuple[bool, np.ndarray, str]:
        """Trade when there's significant edge based on Kelly criterion"""
        
        market_prices = LMSRPricer.get_prices(market.quantities, market.liquidity_param)
        
        # Calculate edge for each outcome
        edges = belief - market_prices
        
        # Find best opportunity
        best_outcome = np.argmax(edges)
        max_edge = edges[best_outcome]
        
        # Only trade if edge exceeds threshold
        if max_edge > 0.05:  # 5% edge threshold
            # Calculate Kelly position size
            true_prob = belief[best_outcome]
            market_prob = market_prices[best_outcome]
            
            # Simplified Kelly for binary-like betting
            if market_prob > 0:
                odds = (1 - market_prob) / market_prob
                kelly_bet = (true_prob * odds - (1 - true_prob)) / odds
                kelly_bet = max(0, min(kelly_bet, 1)) * self.kelly_fraction
                
                # Convert to share purchase
                trade_vector = np.zeros(len(market.outcomes))
                trade_vector[best_outcome] = kelly_bet * 100  # Buy 100 shares scaled by Kelly
                
                return True, trade_vector, f"Kelly edge on {market.outcomes[best_outcome]}: {max_edge:.2%}"
        
        return False, np.zeros(len(market.outcomes)), "No significant edge"


class ArbitrageStrategy(TradingStrategy):
    """
    Look for arbitrage opportunities where sum of probabilities != 1
    or mispricing across related markets
    """
    
    def should_trade(self, market: Market, belief: np.ndarray,
                    current_position: Optional[Position]) -> Tuple[bool, np.ndarray, str]:
        """Find and exploit arbitrage opportunities"""
        
        market_prices = LMSRPricer.get_prices(market.quantities, market.liquidity_param)
        
        # Check if we can buy all outcomes for less than $1
        if np.sum(market_prices) < 0.98:  # Allow for small rounding
            trade_vector = np.ones(len(market.outcomes)) * 10  # Buy 10 of each
            return True, trade_vector, "Arbitrage: Sum of probabilities < 1"
        
        return False, np.zeros(len(market.outcomes)), "No arbitrage found"


class MeanReversionStrategy(TradingStrategy):
    """
    Trade against extreme market movements
    Assumes markets overreact to news
    """
    
    def __init__(self, reversion_threshold: float = 0.15, trade_size: float = 50.0):
        self.reversion_threshold = reversion_threshold
        self.trade_size = max(0.0, float(trade_size))
    
    def should_trade(self, market: Market, belief: np.ndarray,
                    current_position: Optional[Position]) -> Tuple[bool, np.ndarray, str]:
        """Trade when market deviates significantly from belief"""
        
        market_prices = LMSRPricer.get_prices(market.quantities, market.liquidity_param)
        
        # Find outcomes with large deviations
        deviations = belief - market_prices
        
        for i, dev in enumerate(deviations):
            if dev > self.reversion_threshold:
                trade_vector = np.zeros(len(market.outcomes))
                trade_vector[i] = self.trade_size
                return True, trade_vector, f"Buy {market.outcomes[i]} - below recent mean by {dev:.2%}"

            # A sell is only valid when this simulated bot already holds shares;
            # never create an unintended short position.
            if dev < -self.reversion_threshold and current_position is not None:
                held = max(0.0, float(current_position.shares[i]))
                sell_size = min(self.trade_size, held)
                if sell_size > 0:
                    trade_vector = np.zeros(len(market.outcomes))
                    trade_vector[i] = -sell_size
                    return True, trade_vector, f"Sell {market.outcomes[i]} - above recent mean by {abs(dev):.2%}"
        
        return False, np.zeros(len(market.outcomes)), "No mean reversion opportunity"


class TradingBot:
    """
    Main trading bot that manages positions and executes strategies
    """
    
    def __init__(self, initial_capital: float, strategies: List[TradingStrategy]):
        self.capital = initial_capital
        self.initial_capital = initial_capital
        self.strategies = strategies
        self.positions: Dict[str, Position] = {}
        self.trade_history: List[Dict] = []
        self.beliefs: Dict[str, np.ndarray] = {}
    
    def set_belief(self, market_id: str, belief: np.ndarray):
        """Set or update belief about a market"""
        self.beliefs[market_id] = belief / np.sum(belief)  # Normalize
    
    def update_belief_with_data(self, market_id: str, likelihood: np.ndarray):
        """Update existing belief with new data"""
        if market_id in self.beliefs:
            prior = self.beliefs[market_id]
            self.beliefs[market_id] = BayesianUpdater.update_belief(prior, likelihood)
        else:
            # If no prior, use uniform
            n = len(likelihood)
            prior = np.ones(n) / n
            self.beliefs[market_id] = BayesianUpdater.update_belief(prior, likelihood)
    
    def evaluate_market_all(self, market: Market) -> List[Dict]:
        """Return a recommendation from every strategy that signals this market."""
        if market.id not in self.beliefs:
            return []

        belief = self.beliefs[market.id]
        current_position = self.positions.get(market.id)
        recommendations = []
        for strategy in self.strategies:
            should_trade, trade_vector, reason = strategy.should_trade(
                market, belief, current_position
            )
            if not should_trade:
                continue

            cost = LMSRPricer.calculate_trade_cost(
                market.quantities, trade_vector, market.liquidity_param
            )
            if cost > 0 and cost > self.capital:
                continue

            recommendations.append({
                'market_id': market.id,
                'trade_vector': trade_vector,
                'cost': cost,
                'reason': reason,
                'strategy': strategy.__class__.__name__
            })
        return recommendations

    def evaluate_market(self, market: Market) -> Optional[Dict]:
        """
        Evaluate a market and decide whether to trade
        
        Returns:
            Trade recommendation dict or None
        """
        recommendations = self.evaluate_market_all(market)
        return recommendations[0] if recommendations else None
    
    def execute_trade(self, market: Market, trade_vector: np.ndarray) -> bool:
        """
        Execute a trade in the market
        
        Args:
            market: Market to trade in
            trade_vector: Shares to buy/sell for each outcome
            
        Returns:
            Success boolean
        """
        cost = LMSRPricer.calculate_trade_cost(
            market.quantities, trade_vector, market.liquidity_param
        )
        
        # Check capital
        if cost > 0 and cost > self.capital:
            return False
        
        # Update market quantities
        market.quantities += trade_vector
        
        # Update capital
        self.capital -= cost
        
        # Update position
        if market.id in self.positions:
            position = self.positions[market.id]
            position.shares += trade_vector
            position.cost_basis += cost
        else:
            self.positions[market.id] = Position(
                market_id=market.id,
                shares=trade_vector.copy(),
                cost_basis=cost
            )
        
        # Record trade
        self.trade_history.append({
            'timestamp': datetime.now(),
            'market_id': market.id,
            'trade_vector': trade_vector.tolist(),
            'cost': cost,
            'capital_after': self.capital
        })
        
        return True
    
    def settle_market(self, market: Market, winning_outcome: int) -> float:
        """
        Settle a resolved market and calculate P&L
        
        Args:
            market: The resolved market
            winning_outcome: Index of the winning outcome
            
        Returns:
            Profit/loss from this market
        """
        if market.id not in self.positions:
            return 0.0
        
        position = self.positions[market.id]
        
        # Payout is ₦1 per share of winning outcome
        payout = position.shares[winning_outcome]
        
        # Calculate P&L
        pnl = payout - position.cost_basis
        
        # Update capital
        self.capital += payout
        
        # Remove position
        del self.positions[market.id]
        
        return pnl
    
    def get_portfolio_value(self, markets: List[Market]) -> float:
        """Calculate total portfolio value (capital + positions at current prices)"""
        total_value = self.capital
        
        for market in markets:
            if market.id in self.positions:
                position = self.positions[market.id]
                current_prices = LMSRPricer.get_prices(market.quantities, market.liquidity_param)
                
                # Value positions at current market prices
                position_value = np.sum(position.shares * current_prices)
                total_value += position_value
        
        return total_value
    
    def get_performance_summary(self) -> Dict:
        """Get performance metrics"""
        return {
            'initial_capital': self.initial_capital,
            'current_capital': self.capital,
            'total_trades': len(self.trade_history),
            'open_positions': len(self.positions),
            'roi': (self.capital - self.initial_capital) / self.initial_capital
        }


# Example usage and simulation
def simulate_trading():
    """Simulate bot trading on sample markets"""
    
    print("=" * 60)
    print("PREDICTION MARKET TRADING BOT SIMULATION")
    print("=" * 60)
    
    # Create a binary market: "Will it rain tomorrow?"
    market = Market(
        id="rain_tomorrow",
        question="Will it rain tomorrow?",
        outcomes=["Yes", "No"],
        liquidity_param=100.0,  # High liquidity
        quantities=np.array([0.0, 0.0]),  # Start at 50/50
        created_at=datetime.now(),
        resolves_at=datetime.now(),
        resolved=False
    )
    
    # Initialize bot with strategies
    bot = TradingBot(
        initial_capital=1000.0,
        strategies=[MeanReversionStrategy()]
    )
    
    print(f"\nInitial capital: ₦{bot.capital:.2f}")
    print(f"Market: {market.question}")
    print(f"Outcomes: {market.outcomes}")
    
    # Bot's initial belief (60% chance of rain based on weather data)
    bot.set_belief(market.id, np.array([0.60, 0.40]))
    
    print(f"\nBot's belief: Yes={bot.beliefs[market.id][0]:.1%}, No={bot.beliefs[market.id][1]:.1%}")
    
    # Market starts at 50/50
    initial_prices = LMSRPricer.get_prices(market.quantities, market.liquidity_param)
    print(f"Market prices: Yes={initial_prices[0]:.1%}, No={initial_prices[1]:.1%}")
    
    # Evaluate market
    print("\n" + "-" * 60)
    print("EVALUATING MARKET...")
    recommendation = bot.evaluate_market(market)
    
    if recommendation:
        print(f"\nTrade recommended by {recommendation['strategy']}:")
        print(f"  Reason: {recommendation['reason']}")
        print(f"  Shares to buy: Yes={recommendation['trade_vector'][0]:.1f}, No={recommendation['trade_vector'][1]:.1f}")
        print(f"  Cost: ₦{recommendation['cost']:.2f}")
        
        # Execute trade
        if bot.execute_trade(market, recommendation['trade_vector']):
            print(f"  ✓ Trade executed successfully")
            print(f"  Capital remaining: ₦{bot.capital:.2f}")
            
            # Show updated market prices
            new_prices = LMSRPricer.get_prices(market.quantities, market.liquidity_param)
            print(f"  New market prices: Yes={new_prices[0]:.1%}, No={new_prices[1]:.1%}")
    
    # Simulate new information arriving
    print("\n" + "-" * 60)
    print("NEW WEATHER DATA ARRIVES...")
    
    # Likelihood based on new satellite data strongly suggesting rain
    likelihood = np.array([0.85, 0.15])
    bot.update_belief_with_data(market.id, likelihood)
    
    updated_belief = bot.beliefs[market.id]
    print(f"Updated belief: Yes={updated_belief[0]:.1%}, No={updated_belief[1]:.1%}")
    
    # Check for another trade
    recommendation2 = bot.evaluate_market(market)
    if recommendation2:
        print(f"\nAnother trade opportunity:")
        print(f"  Reason: {recommendation2['reason']}")
        bot.execute_trade(market, recommendation2['trade_vector'])
    
    # Market resolves
    print("\n" + "-" * 60)
    print("MARKET RESOLVES: It rained!")
    winning_outcome = 0  # Yes won
    
    pnl = bot.settle_market(market, winning_outcome)
    print(f"P&L from this market: ₦{pnl:.2f}")
    print(f"Final capital: ₦{bot.capital:.2f}")
    
    # Performance summary
    print("\n" + "=" * 60)
    print("PERFORMANCE SUMMARY")
    print("=" * 60)
    summary = bot.get_performance_summary()
    for key, value in summary.items():
        if isinstance(value, float):
            if 'roi' in key:
                print(f"{key}: {value:.2%}")
            else:
                print(f"{key}: ₦{value:.2f}")
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    simulate_trading()
