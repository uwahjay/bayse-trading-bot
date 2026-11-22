"""
Bayse Markets API Adapter - Official API Implementation
Documentation: https://docs.bayse.markets/

Base URL: https://relay.bayse.markets
"""

import requests
import os
import hmac
import hashlib
import base64
import time
import json
import numpy as np
from typing import List, Dict, Optional
from datetime import datetime
from prediction_market_bot import Market, Position


class BayseAPI:
    """
    Official Bayse Markets API client
    Implements HMAC-SHA256 authenticated requests
    """
    
    def __init__(self, public_key: str, secret_key: str):
        """
        Initialize Bayse API client
        
        Args:
            public_key: Your Bayse API public key
            secret_key: Your Bayse API secret key
        """
        self.base_url = "https://relay.bayse.markets"
        self.public_key = public_key
        self.secret_key = secret_key
        
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'BayseTradingBot/1.0'
        })
    
    def _compute_signature(self, timestamp: int, method: str, 
                          path: str, body: str = "") -> str:
        """
        Compute HMAC-SHA256 signature for authenticated requests
        
        Format: {timestamp}.{METHOD}.{path}.{bodyHash}
        """
        # Hash the body
        body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
        
        # Create payload
        payload = f"{timestamp}.{method}.{path}.{body_hash}"
        
        # Compute HMAC signature
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        # Base64 encode
        return base64.b64encode(signature).decode('utf-8')
    
    def _make_request(self, method: str, path: str, 
                     data: Optional[Dict] = None,
                     params: Optional[Dict] = None,
                     authenticated: bool = False) -> Dict:
        """
        Make HTTP request to Bayse API
        
        Args:
            method: HTTP method (GET, POST, DELETE)
            path: API path (e.g., /v1/pm/events)
            data: Request body for POST/PUT
            params: Query parameters
            authenticated: Whether to sign the request
            
        Returns:
            Response JSON
        """
        url = f"{self.base_url}{path}"
        
        # Prepare headers
        headers = {
            'X-Public-Key': self.public_key
        }
        
        # For authenticated (write) requests
        if authenticated:
            timestamp = int(time.time())
            body = json.dumps(data) if data else ""
            signature = self._compute_signature(timestamp, method, path, body)
            
            headers['X-Timestamp'] = str(timestamp)
            headers['X-Signature'] = signature
        
        # Make request
        try:
            if method == 'GET':
                response = self.session.get(url, params=params, headers=headers)
            elif method == 'POST':
                response = self.session.post(url, json=data, params=params, headers=headers)
            elif method == 'DELETE':
                response = self.session.delete(url, params=params, headers=headers)
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.HTTPError as e:
            print(f"API Error: {e}")
            if e.response is not None:
                print(f"Response: {e.response.text}")
            raise
        except Exception as e:
            print(f"Request failed: {e}")
            raise
    
    # ===== MARKET DATA (READ - No signing required) =====
    
    def list_events(self, status: str = None, limit: int = 20, 
                   page: int = 1, category: str = None, 
                   keyword: str = None, currency: str = None,
                   subcategory: str = None) -> List[Dict]:
        """
        List prediction market events
        
        Args:
            status: Filter by status (open, paused, closed, resolved, cancelled)
                    Default: open (includes paused) when omitted
            limit: Number of events per page (default 20)
            page: Page number (default 1)
            category: Filter by category (sports, crypto, politics, etc.)
            subcategory: Optional narrower category filter (e.g. player stats)
            keyword: Search events by title keyword
            currency: Optional display currency (USD or NGN)
            
        Returns:
            List of events with markets
        """
        params = {
            'page': page,
            'size': limit
        }
        
        # Only add optional parameters if provided
        if status:
            params['status'] = status
        if category:
            params['category'] = category
        if subcategory:
            params['subcategory'] = subcategory
        if keyword:
            params['keyword'] = keyword
        if currency:
            params['currency'] = currency.upper()
        
        response = self._make_request('GET', '/v1/pm/events', params=params)
        return response.get('events', [])
    
    def get_event(self, event_id: str) -> Dict:
        """
        Get detailed information about a specific event
        
        Args:
            event_id: Event ID (e.g., evt_123)
            
        Returns:
            Event details with markets
        """
        return self._make_request('GET', f'/v1/pm/events/{event_id}')
    
    def get_quote(self, event_id: str, market_id: str, side: str,
                 outcome_id: str, amount: float, currency: str = "USD") -> Dict:
        """
        Get a quote for a potential trade
        
        Args:
            event_id: Event ID (UUID)
            market_id: Market ID (UUID)
            side: "BUY" or "SELL"
            outcome_id: Outcome ID (UUID string, not index!)
                       Get this from market['outcomes'][i]['id']
            amount: Amount in currency
            currency: Currency code (USD, NGN, etc.)
            
        Returns:
            Quote with price and fees
        """
        data = {
            "side": side.upper(),
            "outcomeId": outcome_id,  # UUID string
            "amount": amount,
            "currency": currency
        }
        
        path = f'/v1/pm/events/{event_id}/markets/{market_id}/quote'
        return self._make_request('POST', path, data=data)
    
    def get_ticker(self, market_id: str, outcome: Optional[str] = None) -> Dict:
        """
        Get current ticker data for a market
        
        Args:
            market_id: Market ID
            
        Returns:
            Ticker with prices, volume, etc.
        """
        params = {'outcome': outcome.upper()} if outcome else None
        return self._make_request('GET', f'/v1/pm/markets/{market_id}/ticker', params=params)
    
    def get_order_book(self, market_id: str, outcome_index: int = 0) -> Dict:
        """
        Get order book depth
        
        Args:
            market_id: Market ID
            outcome_index: Outcome index (0, 1, etc.)
            
        Returns:
            Order book with bids and asks
        """
        params = {
            'marketId': market_id,
            'outcomeIndex': outcome_index
        }
        return self._make_request('GET', '/v1/pm/books', params=params)
    
    def get_price_history(self, event_id: str, time_period: str = "24H",
                         market_ids: Optional[List[str]] = None,
                         outcome: Optional[str] = None) -> Dict:
        """
        Get historical price data
        
        Args:
            event_id: Event ID
            time_period: History window (12H, 24H, 1W, 1M, 1Y)
            market_ids: Optional market UUIDs to filter
            outcome: Optional outcome label (YES or NO)
            
        Returns:
            Historical prices
        """
        params = {'timePeriod': time_period.upper()}
        if market_ids:
            params['marketId[]'] = ','.join(market_ids)
        if outcome:
            params['outcome'] = outcome.upper()
        return self._make_request('GET', f'/v1/pm/events/{event_id}/price-history', 
                                 params=params)
    
    # ===== TRADING (WRITE - Requires signing) =====
    
    def place_order(self, event_id: str, market_id: str, side: str,
                   outcome_id: str, amount: float, currency: str = "USD",
                   order_type: str = "MARKET") -> Dict:
        """
        Place an order in a market
        
        Args:
            event_id: Event ID (UUID)
            market_id: Market ID (UUID)
            side: "BUY" or "SELL"
            outcome_id: Outcome ID (UUID string, not index!)
                       Get this from market['outcomes'][i]['id']
            amount: Amount to trade
            currency: Currency code
            order_type: "MARKET" or "LIMIT"
            
        Returns:
            Order confirmation
        """
        data = {
            "side": side.upper(),
            "outcomeId": outcome_id,  # UUID string
            "amount": amount,
            "currency": currency,
            "type": order_type
        }
        
        path = f'/v1/pm/events/{event_id}/markets/{market_id}/orders'
        return self._make_request('POST', path, data=data, authenticated=True)
    
    def batch_place_orders(self, orders: List[Dict]) -> Dict:
        """
        Place multiple orders at once
        
        Args:
            orders: List of order dicts with keys:
                    eventId, marketId, side, outcomeId (NOT outcome name), amount, currency
                    
        Returns:
            Batch order results
        """
        data = {"orders": orders}
        return self._make_request('POST', '/v1/pm/orders/batch', 
                                 data=data, authenticated=True)
    
    def cancel_order(self, order_id: str) -> Dict:
        """
        Cancel an open order
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            Cancellation confirmation
        """
        return self._make_request('DELETE', f'/v1/pm/orders/{order_id}',
                                 authenticated=True)
    
    def list_orders(self, status: str = "open", limit: int = 50,
                    page: int = 1) -> Dict:
        """
        List your orders
        
        Args:
            status: Filter by status (open, filled, cancelled, all)
            limit: Number of orders
            
        Returns:
            List of orders
        """
        params = {
            'status': status,
            'size': limit,
            'page': page
        }
        return self._make_request('GET', '/v1/pm/orders', params=params)
    
    # ===== PORTFOLIO & WALLET =====
    
    def get_portfolio(self, page: int = 1, limit: int = 100) -> Dict:
        """
        Get your current positions across all markets
        
        Returns:
            Portfolio with positions
        """
        return self._make_request('GET', '/v1/pm/portfolio',
                                  params={'page': page, 'size': limit})
    
    def get_pnl(self, currency: str = "USD") -> Dict:
        """
        Get profit & loss summary
        
        Args:
            currency: Currency to report in
            
        Returns:
            P&L data
        """
        params = {'currency': currency}
        return self._make_request('GET', '/v1/pm/pnl', params=params)
    
    def get_balance(self) -> Dict:
        """
        Get wallet balance
        
        Returns:
            Balance across currencies
        """
        return self._make_request('GET', '/v1/wallet/assets')
    
    def get_activities(self, limit: int = 50) -> Dict:
        """
        Get trading activity history
        
        Args:
            limit: Number of activities
            
        Returns:
            Activity history
        """
        params = {'size': limit}
        return self._make_request('GET', '/v1/pm/activities', params=params)
    
    # ===== HELPER METHODS =====
    
    def get_outcome_id(self, event_id: str, market_id: str, outcome_name: str) -> str:
        """
        Convert outcome name to outcome ID (UUID)
        
        Args:
            event_id: Event ID
            market_id: Market ID
            outcome_name: Outcome name (e.g., "Yes", "No")
            
        Returns:
            Outcome UUID string
        """
        event = self.get_event(event_id)
        
        for market in event.get('markets', []):
            if market['id'] == market_id:
                outcomes = market.get('outcomes', [])
                for outcome in outcomes:
                    if outcome.get('name', '').lower() == outcome_name.lower():
                        return outcome['id']  # Return UUID
        
        raise ValueError(f"Outcome '{outcome_name}' not found in market {market_id}")
    
    @staticmethod
    def extract_market_outcomes(market: Dict, fallback_prices: Optional[List[float]] = None):
        """Read both current flat Bayse outcome fields and older nested responses."""
        raw_outcomes = market.get('outcomes') or market.get('options') or []
        raw_prices = (market.get('prices') or market.get('outcomePrices')
                      or market.get('probabilities') or [])
        labels = []
        values = []

        def first_price(*candidates):
            for candidate in candidates:
                try:
                    value = float(candidate)
                except (TypeError, ValueError):
                    continue
                if 0 < value <= 100:
                    return value / 100 if value > 1 else value
            return None

        if raw_outcomes:
            for index, outcome in enumerate(raw_outcomes):
                if isinstance(outcome, dict):
                    label = (outcome.get('name') or outcome.get('label')
                             or outcome.get('title') or f'Outcome {index + 1}')
                    outcome_price = next(
                        (outcome.get(key) for key in ('price', 'probability', 'currentPrice')
                         if outcome.get(key) is not None), None
                    )
                else:
                    label = str(outcome)
                    outcome_price = None
                mapped_price = (raw_prices.get(str(label).upper())
                                if isinstance(raw_prices, dict) else
                                raw_prices[index] if index < len(raw_prices) else None)
                fallback = fallback_prices[index] if fallback_prices and index < len(fallback_prices) else None
                price = first_price(outcome_price, mapped_price, fallback)
                if price is not None:
                    labels.append(label)
                    values.append(price)
        else:
            for index, default_label in ((1, 'YES'), (2, 'NO')):
                label = market.get(f'outcome{index}Label') or default_label
                mapped_price = (raw_prices.get(str(label).upper())
                                if isinstance(raw_prices, dict) else
                                raw_prices[index - 1] if index - 1 < len(raw_prices) else None)
                fallback = fallback_prices[index - 1] if fallback_prices and index - 1 < len(fallback_prices) else None
                buy_price = market.get('yesBuyPrice' if str(label).upper().startswith('YES')
                                       else 'noBuyPrice' if str(label).upper().startswith('NO')
                                       else '')
                price = first_price(mapped_price, market.get(f'outcome{index}Price'),
                                    buy_price, fallback)
                if price is not None:
                    labels.append(str(label))
                    values.append(price)

        if not values:
            return [], np.array([], dtype=float)
        probabilities = np.asarray(values, dtype=float)
        total = probabilities.sum()
        if total <= 0:
            return [], np.array([], dtype=float)
        return labels, probabilities / total

    def convert_to_bot_markets(self, events: List[Dict]) -> List[Market]:
        """
        Convert Bayse API events to bot Market objects
        
        Args:
            events: List of events from list_events()
            
        Returns:
            List of Market objects for the trading bot
        """
        markets = []
        
        for event in events:
            for market in event.get('markets', []):
                outcomes, prices = self.extract_market_outcomes(market)
                if len(outcomes) < 2:
                    continue

                b = 100.0  # Default liquidity parameter
                # Inverse softmax: softmax(b * log(p)) reproduces p exactly.
                probabilities = np.clip(prices, 1e-9, 1.0)
                quantities = b * np.log(probabilities)

                event_title = event.get('title', 'Unknown Event')
                market_title = market.get('title') or market.get('question')
                question = (f'{event_title}: {market_title}'
                            if market_title and market_title != event_title
                            else event_title)
                resolution_date = event.get('resolutionDate') or event.get('endDate')
                if not resolution_date:
                    resolution_date = datetime.now().isoformat()
                
                bot_market = Market(
                    id=market['id'],
                    question=question,
                    outcomes=outcomes,
                    liquidity_param=b,
                    quantities=quantities,
                    created_at=datetime.fromisoformat(
                        event.get('createdAt', datetime.now().isoformat())
                        .replace('Z', '+00:00')
                    ),
                    resolves_at=datetime.fromisoformat(
                        resolution_date.replace('Z', '+00:00')
                    ),
                    resolved=event.get('status') == 'resolved'
                )
                
                markets.append(bot_market)
        
        return markets


# ===== EXAMPLE USAGE =====

def example_fetch_markets():
    """Example: Fetch and display Bayse markets"""
    
    # Initialize API client
    api = BayseAPI(
        public_key=os.environ.get('BAYSE_PUBLIC_KEY', ''),
        secret_key=os.environ.get('BAYSE_SECRET_KEY', '')
    )
    
    print("=" * 70)
    print("FETCHING BAYSE MARKETS")
    print("=" * 70)
    
    try:
        # Fetch open events (default behavior)
        events = api.list_events(limit=10)  # No status needed - defaults to 'open'
        
        print(f"\nFound {len(events)} events:\n")
        
        for i, event in enumerate(events, 1):
            print(f"{i}. {event.get('title', 'Unknown')}")
            print(f"   Category: {event.get('category', 'N/A')}")
            print(f"   Status: {event.get('status', 'N/A')}")
            
            # Show markets in this event
            markets = event.get('markets', [])
            for market in markets:
                print(f"\n   Market: {market.get('question', 'N/A')}")
                print(f"   Engine: {market.get('engine', 'N/A')}")
                
                # Show outcomes and prices
                outcomes = market.get('outcomes', [])
                prices = market.get('prices', [])
                
                if outcomes and prices:
                    print(f"   Outcomes:")
                    for j, (outcome, price) in enumerate(zip(outcomes, prices)):
                        outcome_name = outcome.get('name', f'Outcome {j}')
                        print(f"     - {outcome_name}: {price:.1%}")
            
            print()
    
    except Exception as e:
        print(f"Error: {e}")


def example_place_trade():
    """Example: Get quote and place a trade"""
    
    api = BayseAPI(
        public_key=os.environ.get('BAYSE_PUBLIC_KEY', ''),
        secret_key=os.environ.get('BAYSE_SECRET_KEY', '')
    )
    
    print("=" * 70)
    print("PLACING A TRADE")
    print("=" * 70)
    
    try:
        # First, fetch a real market
        print("\n1. Fetching markets...")
        events = api.list_events(limit=1)  # Defaults to open markets
        
        if not events:
            print("No active markets found!")
            return
        
        event = events[0]
        if not event.get('markets'):
            print("Event has no markets!")
            return
        
        market = event['markets'][0]
        
        print(f"   Event: {event['title']}")
        print(f"   Market: {market.get('question', 'N/A')}")
        
        # Show outcomes
        outcomes = market.get('outcomes', [])
        if not outcomes:
            print("   No outcomes found!")
            return
        
        print(f"   Outcomes:")
        for i, outcome in enumerate(outcomes):
            print(f"     [{i}] {outcome['name']} (ID: {outcome['id']})")
        
        # Use the first outcome
        outcome_id = outcomes[0]['id']
        
        print(f"\n2. Getting quote for outcome '{outcomes[0]['name']}'...")
        quote = api.get_quote(
            event_id=event['id'],
            market_id=market['id'],
            side="BUY",
            outcome_id=outcome_id,  # UUID string
            amount=10.0,
            currency="USD"
        )
        
        print(f"   Quote received:")
        print(f"   Amount: ${quote.get('amount', 0):.2f}")
        print(f"   Shares: {quote.get('quantity', 0):.2f}")
        print(f"   Price: ${quote.get('price', 0):.4f}")
        print(f"   Fees: ${quote.get('fee', 0):.2f}")
        
        # Uncomment to actually place the order:
        # print("\n3. Placing order...")
        # order = api.place_order(
        #     event_id=event['id'],
        #     market_id=market['id'],
        #     side="BUY",
        #     outcome_id=outcome_id,
        #     amount=10.0,
        #     currency="USD"
        # )
        # print(f"   Order placed!")
        # print(f"   Order ID: {order.get('id')}")
        # print(f"   Status: {order.get('status')}")
        
    except Exception as e:
        print(f"Error: {e}")


def example_with_trading_bot():
    """Example: Use Bayse API with the trading bot"""
    from prediction_market_bot import TradingBot, MeanReversionStrategy
    
    print("=" * 70)
    print("BAYSE API + TRADING BOT")
    print("=" * 70)
    
    # Initialize API
    api = BayseAPI(
        public_key="pk_live_your_key_here",
        secret_key="sk_live_your_secret_here"
    )
    
    # Initialize bot
    bot = TradingBot(
        initial_capital=1000.0,
        strategies=[MeanReversionStrategy()]
    )
    
    try:
        # Fetch markets from Bayse
        print("\n1. Fetching markets...")
        events = []
        for category in ('crypto', 'finance'):
            events.extend(api.list_events(limit=50, category=category))
        markets = api.convert_to_bot_markets(events)
        
        print(f"   Loaded {len(markets)} markets")
        
        # Analyze each market
        print("\n2. Analyzing markets...")
        
        for market in markets[:5]:  # First 5
            print(f"\n   Market: {market.question}")
            
            # Set belief (you'd use real analysis here)
            from prediction_market_bot import LMSRPricer
            
            current_prices = LMSRPricer.get_prices(
                market.quantities, 
                market.liquidity_param
            )
            
            # For demo: assume you think market underprices first outcome by 10%
            belief = current_prices.copy()
            belief[0] += 0.10
            belief[1] -= 0.10
            belief = belief / belief.sum()
            
            bot.set_belief(market.id, belief)
            
            # Get recommendation
            rec = bot.evaluate_market(market)
            
            if rec:
                print(f"   ✓ Opportunity: {rec['reason']}")
                print(f"   Trade: {rec['trade_vector']}")
                print(f"   Cost: ${rec['cost']:.2f}")
                
                # You could place the order here:
                # api.place_order(...)
            else:
                print(f"   ○ No trade opportunity")
        
        print("\n3. Summary:")
        print(f"   Bot capital: ${bot.capital:.2f}")
        
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    print("""
Bayse Markets API Adapter
=========================

This adapter connects your trading bot to the official Bayse Markets API.

Before running:
1. Sign up at https://bayse.markets
2. Create API key at https://app.bayse.markets/settings/api-keys
3. Update public_key and secret_key in the examples above

Documentation: https://docs.bayse.markets/

Examples:
- example_fetch_markets() - Fetch and display markets
- example_place_trade() - Get quote and place order
- example_with_trading_bot() - Full integration with trading bot

""")
    
   
    example_fetch_markets()
    example_place_trade()
    example_with_trading_bot()
