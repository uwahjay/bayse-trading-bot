"""
Bayse Telegram Trading Bot
User-friendly Telegram bot for Bayse Markets with crowd signal detection

Features:
- Real-time market updates via Telegram
- Crowd sentiment/voting system
- One-click trading
- Portfolio monitoring
- Performance tracking

Setup:
1. Create bot: https://t.me/BotFather
2. Get token
3. Run this script with your token and API keys
"""

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from bayse_api_official import BayseAPI
from prediction_market_bot import TradingBot
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from pathlib import Path
import websockets
from sportsbook_odds import SportsbookOddsClient, outcome_probability

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# These values are sent to Bayse's list-events category filter. Keep labels
# separate so Telegram callback data stays stable and human-readable.
MARKET_CATEGORIES = {
    'finance': ('Finance', 'finance', None),
    'sports': ('Sports', 'sports', None),
    'player_stats': ('Player Stats', 'sports', 'player_stats'),
    'crypto': ('Crypto', 'crypto', None),
}
AUTO_TRADE_CATEGORIES = ('sports',)
AUTO_TRADE_CATEGORY_LABELS = {
    'sports': 'Sports',
}
RISK_FRACTIONS = {'conservative': 0.20, 'moderate': 0.50, 'aggressive': 0.80}
MAX_OPEN_EXPOSURE = 1005.0
AUTO_TRADE_INTERVAL_SECONDS = 60
MAX_MARKET_CLOSE_DAYS = 3
SPORTS_CLOSE_WINDOW_CATEGORIES = {'sports', 'player_stats'}
SPORTS_MIN_EDGE = max(0.0, min(0.25, float(os.getenv('SPORTS_MIN_EDGE', '0.05'))))


def _bayse_closing_time(event, market=None):
    """Read Bayse's trading close timestamp, preferring a market-specific value."""
    for source in (market, event):
        if not isinstance(source, dict):
            continue
        for key in ('closingDate', 'closeDate', 'closesAt'):
            raw = source.get(key)
            if not raw:
                continue
            try:
                parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(
                    str(raw).replace('Z', '+00:00')
                )
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return None


def _closes_within_window(event, market=None, now=None):
    closing_at = _bayse_closing_time(event, market)
    now = now or datetime.now(timezone.utc)
    return bool(closing_at and now < closing_at <= now + timedelta(days=MAX_MARKET_CLOSE_DAYS))


def build_active_strategies():
    """Legacy simulator strategies; live signals now use sportsbook consensus."""
    return []


def _event_matches_category(event, category):
    """Check cached event category metadata before adding it to a filtered view."""
    label, api_value, subcategory = MARKET_CATEGORIES[category]
    candidates = [event.get('category'), event.get('categoryName'), event.get('section')]
    if subcategory:
        candidates.extend([event.get('subcategory'), event.get('subCategory')])
    tags = event.get('tags')
    if isinstance(tags, list):
        candidates.extend(tags)
    if category == 'player_stats':
        candidates.extend([event.get('sportMarketType'), event.get('title'), event.get('description')])
        for market in event.get('markets', []):
            if isinstance(market, dict):
                candidates.extend([market.get('title'), market.get('question')])
        searchable_text = ' '.join(str(value).lower() for value in candidates if value)
        if ('player_stats' in searchable_text or 'player stats' in searchable_text
                or ('player' in searchable_text and any(
                    term in searchable_text for term in (
                        'stat', 'points', 'rebounds', 'assists', 'shots', 'goals', 'passes'
                    )
                ))):
            return True
    normalized = {str(value).strip().lower().replace('-', '_').replace(' ', '_')
                  for value in candidates if value}
    aliases = {subcategory, label.lower().replace(' ', '_')} if subcategory else {
        api_value, label.lower().replace(' ', '_')
    }
    if category == 'player_stats':
        aliases.update({'player_stat', 'player_statistics', 'playerstats'})
    return bool(normalized & aliases)


def _list_auto_trade_events(api, category, currency, limit=50):
    """Fetch a category with fallbacks for Bayse's Player Stats taxonomy."""
    _, api_category, subcategory = MARKET_CATEGORIES[category]
    queries = [(api_category, subcategory)]
    if category == 'player_stats':
        # Bayse's API documents category and subcategory filters but does not
        # publish the allowed subcategory values. Support both common forms.
        queries.extend([('player_stats', None), ('player-stats', None), ('sports', None)])

    events_by_id = {}
    for query_category, query_subcategory in queries:
        try:
            events = api.list_events(
                limit=limit, category=query_category, subcategory=query_subcategory,
                currency=currency
            )
        except Exception:
            logger.exception('Could not fetch auto-trade %s events (%s/%s)',
                             category, query_category, query_subcategory)
            continue
        for event in events:
            if query_category == 'sports' and query_subcategory is None and category == 'player_stats':
                if not _event_matches_category(event, 'player_stats'):
                    continue
            event_id = event.get('id')
            if event_id:
                events_by_id[str(event_id)] = event
    return list(events_by_id.values())

# Store user sessions
user_sessions = {}


def positive_price(*values):
    """Return the first usable non-zero price from alternative Bayse fields."""
    for value in values:
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if 0 < price <= 10000:
            return price
    return None


async def setkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow a user to store their Bayse API keys for personal portfolio views"""
    user_id = update.effective_user.id
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /setkeys <public_key> <secret_key>")
        return

    public, secret = args
    # persist to DB
    save_user_keys(user_id, public, secret)
    user_sessions[user_id] = {'bayse_public': public, 'bayse_secret': secret}
    await update.message.reply_text("Saved your Bayse API keys for portfolio views.")


async def clearkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear stored Bayse API keys for the user"""
    user_id = update.effective_user.id
    clear_user_keys(user_id)
    user_sessions.pop(user_id, None)
    await update.message.reply_text("Cleared your stored Bayse API keys.")


def init_db(db_path: str = "data/users.db"):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_keys (
            user_id INTEGER PRIMARY KEY,
            public_key TEXT NOT NULL,
            secret_key TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_trade_preferences (
            user_id INTEGER PRIMARY KEY,
            risk_level TEXT NOT NULL DEFAULT 'moderate',
            auto_enabled INTEGER NOT NULL DEFAULT 0,
            auto_categories TEXT NOT NULL DEFAULT '["sports", "player_stats"]'
        )
        """
    )
    preference_columns = {row[1] for row in cur.execute(
        "PRAGMA table_info(user_trade_preferences)"
    ).fetchall()}
    if 'auto_categories' not in preference_columns:
        cur.execute(
            "ALTER TABLE user_trade_preferences ADD COLUMN auto_categories TEXT "
            "NOT NULL DEFAULT '[\"sports\", \"player_stats\"]'"
        )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS automated_trade_guards (
            user_id INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            outcome_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, market_id, outcome_id)
        )
        """
    )
    conn.commit()
    conn.close()


def get_trade_preferences(user_id: int, db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT risk_level, auto_enabled, auto_categories "
        "FROM user_trade_preferences WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    if row:
        try:
            categories = json.loads(row[2])
        except (TypeError, json.JSONDecodeError):
            categories = list(AUTO_TRADE_CATEGORIES)
        old_categories = categories
        categories = [category for category in categories if category in AUTO_TRADE_CATEGORIES]
        if old_categories and not categories:
            categories = list(AUTO_TRADE_CATEGORIES)
        return {'risk_level': row[0], 'auto_enabled': bool(row[1]),
                'auto_categories': categories}
    return {'risk_level': 'moderate', 'auto_enabled': False,
            'auto_categories': list(AUTO_TRADE_CATEGORIES)}


def save_trade_preferences(user_id: int, risk_level=None, auto_enabled=None,
                           auto_categories=None,
                           db_path: str = "data/users.db"):
    current = get_trade_preferences(user_id, db_path)
    risk_level = risk_level or current['risk_level']
    if risk_level not in RISK_FRACTIONS:
        risk_level = 'moderate'
    enabled = current['auto_enabled'] if auto_enabled is None else bool(auto_enabled)
    categories = current['auto_categories'] if auto_categories is None else auto_categories
    categories = [category for category in AUTO_TRADE_CATEGORIES if category in categories]
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO user_trade_preferences
           (user_id, risk_level, auto_enabled, auto_categories)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             risk_level=excluded.risk_level, auto_enabled=excluded.auto_enabled,
             auto_categories=excluded.auto_categories""",
        (user_id, risk_level, int(enabled), json.dumps(categories))
    )
    conn.commit()
    conn.close()


def list_auto_trade_users(db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT user_id, risk_level, auto_categories "
        "FROM user_trade_preferences WHERE auto_enabled = 1"
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        try:
            categories = json.loads(row[2])
        except (TypeError, json.JSONDecodeError):
            categories = list(AUTO_TRADE_CATEGORIES)
        previous_categories = categories
        categories = [category for category in categories if category in AUTO_TRADE_CATEGORIES]
        if previous_categories and not categories:
            categories = list(AUTO_TRADE_CATEGORIES)
        results.append((int(row[0]), row[1], categories))
    return results


def has_auto_trade_guard(user_id: int, market_id: str, outcome_id: str,
                         db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM automated_trade_guards WHERE user_id = ? AND market_id = ? AND outcome_id = ?",
        (user_id, market_id, outcome_id)
    ).fetchone()
    conn.close()
    return row is not None


def record_auto_trade(user_id: int, market_id: str, outcome_id: str,
                      db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO automated_trade_guards VALUES (?, ?, ?, ?)",
        (user_id, market_id, outcome_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def save_user_keys(user_id: int, public: str, secret: str, db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "REPLACE INTO user_keys (user_id, public_key, secret_key) VALUES (?, ?, ?)",
        (user_id, public, secret)
    )
    conn.commit()
    conn.close()


def get_user_keys(user_id: int, db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT public_key, secret_key FROM user_keys WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {'bayse_public': row[0], 'bayse_secret': row[1]}
    return None


def load_all_user_keys(db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT user_id, public_key, secret_key FROM user_keys")
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        uid = int(r[0])
        user_sessions[uid] = {'bayse_public': r[1], 'bayse_secret': r[2]}


def clear_user_keys(user_id: int, db_path: str = "data/users.db"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM user_keys WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

class BayseTelegramBot:
    def __init__(self, telegram_token: str, bayse_public_key: str, bayse_secret_key: str):
        self.telegram_token = telegram_token
        self.api = BayseAPI(bayse_public_key, bayse_secret_key)
        self.bot = TradingBot(1000, build_active_strategies())
        self.odds = SportsbookOddsClient()
        self.market_currency = os.environ.get('BAYSE_CURRENCY', 'NGN').upper()
        if self.market_currency not in {'USD', 'NGN'}:
            logger.warning('Unsupported BAYSE_CURRENCY %r; using NGN', self.market_currency)
            self.market_currency = 'NGN'

    async def start_market_data_listener(self, application: Application):
        """Start a reconnecting Bayse price feed and cache the latest event snapshots."""
        application.bot_data['market_data_task'] = asyncio.create_task(
            self._market_data_listener(application), name='bayse-market-data'
        )
        application.bot_data['auto_trade_task'] = asyncio.create_task(
            self._auto_trade_loop(application), name='bayse-auto-trading'
        )

    async def stop_market_data_listener(self, application: Application):
        task = application.bot_data.get('market_data_task')
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        auto_task = application.bot_data.get('auto_trade_task')
        if auto_task:
            auto_task.cancel()
            try:
                await auto_task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _response_rows(response, key):
        if isinstance(response, dict):
            value = response.get(key)
            if isinstance(value, list):
                return value
            nested = response.get('data')
            if isinstance(nested, dict) and isinstance(nested.get(key), list):
                return nested[key]
        return response if isinstance(response, list) else []

    @staticmethod
    def _numeric(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _outcome_id_for_label(raw_market, label):
        target = str(label).strip().upper()
        nested = raw_market.get('outcomes') or raw_market.get('options') or []
        if isinstance(nested, list):
            for outcome in nested:
                if not isinstance(outcome, dict):
                    continue
                outcome_label = outcome.get('label') or outcome.get('name') or outcome.get('outcome')
                if str(outcome_label).strip().upper() == target:
                    return outcome.get('id')
        for index in (1, 2):
            outcome_label = raw_market.get(f'outcome{index}Label')
            outcome_id = raw_market.get(f'outcome{index}Id')
            if str(outcome_label or ('YES' if index == 1 else 'NO')).strip().upper() == target:
                return outcome_id
        return None

    def _find_auto_trade_candidates(self, api, categories, diagnostics=None):
        """Compare sportsbook no-vig probabilities with Bayse market prices."""
        candidates = []
        stats = diagnostics if diagnostics is not None else {}
        stats.update(events=0, markets=0, markets_in_close_window=0,
                     markets_outside_close_window=0, markets_missing_close_time=0,
                     markets_with_odds=0, missing_outcome_ids=0,
                     odds_provider_configured=self.odds.configured)
        if not self.odds.configured:
            stats['odds_configuration_error'] = (
                'Set ODDS_API_KEY and ODDS_API_SPORT_KEYS to enable sportsbook comparisons.'
            )
            return candidates
        if not any(category in AUTO_TRADE_CATEGORIES for category in categories):
            return candidates
        now = datetime.now(timezone.utc)
        latest_close = now + timedelta(days=MAX_MARKET_CLOSE_DAYS)
        for category in categories:
            if category not in AUTO_TRADE_CATEGORIES:
                continue
            events = _list_auto_trade_events(
                api, category, self.market_currency, limit=50
            )
            stats['events'] += len(events)
            for event in events:
                event_id = event.get('id')
                if not event_id:
                    continue
                # The standard sportsbook feed returns head-to-head markets. Player
                # props need provider-specific event market keys and are deliberately
                # skipped until an explicit prop mapping/feed is configured.
                if category == 'player_stats':
                    stats['player_props_unsupported'] = stats.get('player_props_unsupported', 0) + len(
                        event.get('markets', [])
                    )
                    continue
                try:
                    odds_matches = self.odds.get_event_odds(event)
                except Exception:
                    logger.exception('Sportsbook odds lookup failed for Bayse event %s', event_id)
                    stats['odds_lookup_errors'] = stats.get('odds_lookup_errors', 0) + 1
                    continue
                if not odds_matches:
                    stats['events_without_odds_match'] = stats.get('events_without_odds_match', 0) + 1
                    continue
                consensus = self.odds.consensus_probabilities(event)
                probabilities = consensus['probabilities']
                if not probabilities:
                    if consensus.get('stale'):
                        stats['events_with_stale_odds'] = stats.get('events_with_stale_odds', 0) + 1
                    continue
                for raw_market in event.get('markets', []):
                    stats['markets'] += 1
                    market_id = raw_market.get('id')
                    closing_at = _bayse_closing_time(event, raw_market)
                    if closing_at is None:
                        stats['markets_missing_close_time'] += 1
                        continue
                    if closing_at <= now or closing_at > latest_close:
                        stats['markets_outside_close_window'] += 1
                        continue
                    stats['markets_in_close_window'] += 1
                    outcomes, displayed_prices = api.extract_market_outcomes(raw_market)
                    if len(outcomes) < 2 or len(outcomes) != len(displayed_prices):
                        continue
                    event_title = event.get('title', 'Untitled event')
                    market_title = raw_market.get('question') or raw_market.get('title')
                    question = (f'{event_title}: {market_title}'
                                if market_title and market_title != event_title else event_title)
                    for odds_event in odds_matches:
                        stats['markets_with_odds'] += 1
                        for label, displayed_price in zip(outcomes, displayed_prices):
                            fair_probability = outcome_probability(
                                label, odds_event, probabilities, self.odds.team_aliases
                            )
                            if fair_probability is None:
                                continue
                            edge = float(fair_probability) - float(displayed_price)
                            if edge <= SPORTS_MIN_EDGE:
                                continue
                            outcome_id = self._outcome_id_for_label(raw_market, label)
                            if not outcome_id:
                                stats['missing_outcome_ids'] += 1
                                continue
                            candidates.append({
                                'category': category,
                                'event': event,
                                'market': raw_market,
                                'question': question,
                                'outcome': label,
                                'outcome_id': outcome_id,
                                'market_id': market_id,
                                'event_id': event_id,
                                'reason': (
                                    f'Sportsbook no-vig consensus {fair_probability:.1%} vs Bayse display '
                                    f'{float(displayed_price):.1%}; estimated edge {edge:.1%} '
                                    f'from {consensus["bookmakers"]} fresh bookmaker(s).'
                                ),
                                'strategy': 'Sportsbook edge',
                                'fair_probability': float(fair_probability),
                                'minimum_edge': SPORTS_MIN_EDGE,
                                'displayed_price': float(displayed_price),
                                'bookmakers': consensus['bookmakers'],
                                'closing_at': closing_at,
                            })
        return candidates

    async def _auto_trade_loop(self, application: Application):
        """Periodically scan enabled users and place capped, quoted BUY orders."""
        while True:
            for user_id, risk_level, categories in list_auto_trade_users():
                try:
                    await self._auto_trade_once(application, user_id, risk_level, categories)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception('Automated trade cycle failed for Telegram user %s', user_id)
                    self._set_auto_trade_status(
                        application, user_id,
                        f"Cycle failed: {type(exc).__name__}: {str(exc)[:160]}"
                    )
            await asyncio.sleep(AUTO_TRADE_INTERVAL_SECONDS)

    @staticmethod
    def _set_auto_trade_status(application, user_id, message):
        statuses = application.bot_data.setdefault('auto_trade_status', {})
        statuses[user_id] = f"{datetime.now().astimezone():%H:%M:%S} - {message}"

    async def _auto_trade_once(self, application: Application, user_id: int,
                              risk_level: str, categories):
        if not categories:
            self._set_auto_trade_status(application, user_id, 'No auto-trade categories selected.')
            return
        self._set_auto_trade_status(application, user_id, 'Checking wallet and scanning selected categories...')
        api = self._user_api(user_id)
        assets_response = await asyncio.to_thread(api.get_balance)
        assets = self._response_rows(assets_response, 'assets')
        asset = next((item for item in assets
                      if str(item.get('symbol') or item.get('currency', '')).upper()
                      == self.market_currency), None)
        if not asset:
            logger.warning('Auto-trading skipped for %s: no %s wallet asset', user_id, self.market_currency)
            self._set_auto_trade_status(
                application, user_id,
                f"Wallet response has no {self.market_currency} asset; check BAYSE_CURRENCY and API access."
            )
            return
        available = self._numeric(asset.get('availableBalance', asset.get('available')))
        if available <= 0:
            self._set_auto_trade_status(application, user_id, f'Available {self.market_currency} balance is zero.')
            return

        portfolio = await asyncio.to_thread(api.get_portfolio, 1, 100)
        positions = self._response_rows(portfolio, 'outcomeBalances')
        if not positions:
            positions = self._response_rows(portfolio, 'positions')
        portfolio_pages = self._numeric((portfolio.get('pagination') or {}).get('lastPage'), 1)
        if portfolio_pages > 20:
            logger.error('Auto-trading stopped: portfolio pagination is too large to verify exposure')
            self._set_auto_trade_status(application, user_id, 'Stopped: portfolio has too many pages to verify exposure safely.')
            return
        for page in range(2, min(int(portfolio_pages), 20) + 1):
            next_page = await asyncio.to_thread(api.get_portfolio, page, 100)
            positions.extend(self._response_rows(next_page, 'outcomeBalances')
                             or self._response_rows(next_page, 'positions'))
        position_exposure = 0.0
        held_market_ids = set()
        for position in positions:
            currency = str(position.get('currency') or self.market_currency).upper()
            if currency != self.market_currency:
                continue
            position_exposure += max(0.0, self._numeric(position.get('cost')))
            market = position.get('market') or {}
            if market.get('id'):
                held_market_ids.add(str(market['id']))

        orders = []
        for order_status in ('open', 'partial_filled'):
            orders_response = await asyncio.to_thread(
                api.list_orders, status=order_status, limit=100
            )
            orders.extend(self._response_rows(orders_response, 'orders'))
            order_pages = self._numeric((orders_response.get('pagination') or {}).get('lastPage'), 1)
            if order_pages > 20:
                logger.error('Auto-trading stopped: order pagination is too large to verify exposure')
                self._set_auto_trade_status(application, user_id, 'Stopped: open orders have too many pages to verify exposure safely.')
                return
            for page in range(2, int(order_pages) + 1):
                next_page = await asyncio.to_thread(api.list_orders, order_status, 100, page)
                orders.extend(self._response_rows(next_page, 'orders'))
        reserved_exposure = 0.0
        pending_market_ids = set()
        for order in orders:
            if str(order.get('side', 'BUY')).upper() != 'BUY':
                continue
            if str(order.get('currency') or self.market_currency).upper() != self.market_currency:
                continue
            amount = max(0.0, self._numeric(order.get('amount')))
            size = self._numeric(order.get('size'))
            remaining = self._numeric(order.get('remainingSize'))
            if size > 0 and remaining >= 0:
                amount *= min(1.0, remaining / size)
            reserved_exposure += amount
            if order.get('marketId'):
                pending_market_ids.add(str(order['marketId']))

        current_exposure = position_exposure + reserved_exposure
        exposure_room = MAX_OPEN_EXPOSURE - current_exposure
        risk_fraction = RISK_FRACTIONS.get(risk_level, RISK_FRACTIONS['moderate'])
        per_trade_cap = min(available * risk_fraction, exposure_room)
        if per_trade_cap <= 0:
            self._set_auto_trade_status(
                application, user_id,
                f'No spending room: available balance {self._money(available, self.market_currency)}, '
                f'open exposure {self._money(current_exposure, self.market_currency)} / '
                f'{self._money(MAX_OPEN_EXPOSURE, self.market_currency)}.'
            )
            return
        minimum_order = 100.0 if self.market_currency == 'NGN' else 1.0
        if per_trade_cap < minimum_order:
            self._set_auto_trade_status(
                application, user_id,
                f'Risk limit allows only {self._money(per_trade_cap, self.market_currency)} per trade, '
                f'below Bayse minimum {self._money(minimum_order, self.market_currency)}.'
            )
            return

        scan_stats = {}
        candidates = await asyncio.to_thread(
            self._find_auto_trade_candidates, api, categories, scan_stats
        )
        if not candidates:
            self._set_auto_trade_status(
                application, user_id,
                scan_stats.get('odds_configuration_error') or
                (f'Scanned {scan_stats.get("events", 0)} sports events / '
                 f'{scan_stats.get("markets", 0)} markets; '
                 f'{scan_stats.get("markets_in_close_window", 0)} close within '
                 f'{MAX_MARKET_CLOSE_DAYS} days. '
                 f'{scan_stats.get("events_without_odds_match", 0)} had no exact sportsbook event match; '
                 f'{scan_stats.get("events_with_stale_odds", 0)} had stale odds; '
                 f'{scan_stats.get("player_props_unsupported", 0)} player-stat markets were skipped '
                 'because configured feed currently uses head-to-head odds. '
                 f'No quote-confirmed sportsbook edge above {SPORTS_MIN_EDGE:.0%} was found.')
            )
            return
        skipped = 0
        for candidate in candidates:
            preferences = get_trade_preferences(user_id)
            if not preferences['auto_enabled']:
                self._set_auto_trade_status(application, user_id, 'Auto-trading was turned off during the scan.')
                return
            if candidate['category'] not in preferences['auto_categories']:
                skipped += 1
                continue
            market_id = str(candidate['market_id'])
            outcome_id = str(candidate['outcome_id'])
            if market_id in held_market_ids or market_id in pending_market_ids:
                skipped += 1
                continue
            if has_auto_trade_guard(user_id, market_id, outcome_id):
                skipped += 1
                continue

            quote = await asyncio.to_thread(
                api.get_quote, candidate['event_id'], market_id, 'BUY', outcome_id,
                per_trade_cap, self.market_currency
            )
            if isinstance(quote.get('data'), dict):
                quote = quote['data']
            quoted_amount = self._numeric(quote.get('amount'), default=float('inf'))
            quoted_quantity = self._numeric(quote.get('quantity'), default=0.0)
            if (quoted_amount <= 0 or quoted_amount > per_trade_cap + 1e-8
                    or quote.get('completeFill') is False
                    or quote.get('tradeGoesOverMaxLiability') is True):
                logger.info('Skipped quote for %s because it exceeds the limits or cannot fill', market_id)
                self._set_auto_trade_status(
                    application, user_id,
                    f'Found a signal for {candidate["question"]}, but Bayse quote is unavailable, '
                    'cannot fill, or exceeds the per-trade cap.'
                )
                continue

            # amount/quantity is treated as the all-in executable share cost.
            # If the response omits either field, fail closed rather than compare
            # sportsbook probability with a stale/display-only Bayse price.
            if quoted_quantity <= 0:
                self._set_auto_trade_status(
                    application, user_id,
                    f'Found a sportsbook edge for {candidate["question"]}, but Bayse quote '
                    'did not include a usable share quantity; order skipped.'
                )
                continue
            all_in_price = quoted_amount / quoted_quantity
            executable_edge = candidate['fair_probability'] - all_in_price
            if not 0 < all_in_price < 1 or executable_edge <= candidate['minimum_edge']:
                logger.info(
                    'Skipped %s after quote: fair=%.4f all-in=%.4f edge=%.4f',
                    market_id, candidate['fair_probability'], all_in_price, executable_edge
                )
                self._set_auto_trade_status(
                    application, user_id,
                    f'Sportsbook edge for {candidate["question"]} fell to '
                    f'{executable_edge:.1%} at Bayse quote; minimum is '
                    f'{candidate["minimum_edge"]:.1%}. No order sent.'
                )
                continue

            order_response = await asyncio.to_thread(
                api.place_order, candidate['event_id'], market_id, 'BUY', outcome_id,
                quoted_amount, self.market_currency, 'MARKET'
            )
            order = order_response.get('order', order_response)
            if isinstance(order, dict) and str(order.get('status', '')).lower() in {
                    'rejected', 'cancelled', 'failed', 'expired'}:
                logger.warning('Bayse rejected auto-order for market %s: %s', market_id, order)
                self._set_auto_trade_status(
                    application, user_id,
                    f'Bayse rejected the order for {candidate["question"]}: '
                    f'{str(order.get("message") or order.get("status"))[:120]}'
                )
                continue
            record_auto_trade(user_id, market_id, outcome_id)
            self._set_auto_trade_status(
                application, user_id,
                f'{candidate["strategy"]} order submitted for {candidate["question"]} '
                f'({self._money(quoted_amount, self.market_currency)}).'
            )
            await application.bot.send_message(
                chat_id=user_id,
                text=(f"Auto trade placed ({candidate['category'].title()} / {candidate['strategy']})\n"
                      f"{candidate['question']}\n"
                      f"Bayse closes: {candidate['closing_at']:%Y-%m-%d %H:%M} UTC\n"
                      f"Buy {candidate['outcome']} for {self._money(quoted_amount, self.market_currency)}\n"
                      f"Sportsbook estimate {candidate['fair_probability']:.1%}; Bayse all-in "
                      f"{all_in_price:.1%}; edge {executable_edge:.1%}\n"
                      f"Reason: {candidate['reason']}\n"
                      f"Open exposure cap: {self._money(MAX_OPEN_EXPOSURE, self.market_currency)}")
            )
            return
        self._set_auto_trade_status(
            application, user_id,
            f'Found {len(candidates)} signal(s), but none could be submitted; '
            f'{skipped} were already held, pending, guarded, or deselected. Check API logs for quote/order errors.'
        )

    async def _market_data_listener(self, application: Application):
        """Subscribe to event price updates; split batched frames before JSON parsing."""
        uri = 'wss://socket.bayse.markets/ws/v1/markets'
        reconnect_delay = 1

        while True:
            try:
                # Subscribe to the categories exposed in the Telegram UI, plus
                # the unfiltered feed. Dedup IDs before subscribing.
                events = await asyncio.to_thread(self.api.list_events, limit=50)
                for _, api_category, api_subcategory in MARKET_CATEGORIES.values():
                    try:
                        events.extend(await asyncio.to_thread(
                            self.api.list_events, limit=50, category=api_category,
                            subcategory=api_subcategory
                        ))
                    except Exception:
                        logger.exception('Could not load %s events for live subscriptions', api_category)
                event_ids = list(dict.fromkeys(
                    str(event['id']) for event in events if event.get('id')
                ))
                if not event_ids:
                    logger.warning('Bayse returned no event IDs for WebSocket subscriptions')
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 30)
                    continue

                async with websockets.connect(uri, ping_interval=20, ping_timeout=60) as ws:
                    reconnect_delay = 1
                    for event_id in event_ids:
                        await ws.send(json.dumps({
                            'type': 'subscribe',
                            'channel': 'prices',
                            'eventId': event_id,
                        }))
                    logger.info('Subscribed to Bayse prices for %d events', len(event_ids))

                    async for frame in ws:
                        if isinstance(frame, bytes):
                            frame = frame.decode('utf-8')
                        for line in frame.splitlines():
                            if not line.strip():
                                continue
                            try:
                                message = json.loads(line)
                            except json.JSONDecodeError:
                                logger.warning('Ignoring malformed Bayse WebSocket line: %r', line[:200])
                                continue

                            if not isinstance(message, dict):
                                logger.warning('Ignoring non-object Bayse WebSocket message')
                                continue
                            if message.get('type') != 'price_update':
                                continue
                            event = message.get('data')
                            if isinstance(event, dict) and event.get('id'):
                                application.bot_data.setdefault('live_market_events', {})[
                                    str(event['id'])
                                ] = event
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Bayse market WebSocket disconnected; reconnecting in %s seconds', reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

    async def _ticker_price(self, market_id: str, outcome: str):
        """Read the live ticker as a fallback when an event snapshot has no price."""
        try:
            ticker = await asyncio.to_thread(self.api.get_ticker, market_id, outcome)
            return positive_price(
                ticker.get('bestAsk'), ticker.get('lastPrice'), ticker.get('midPrice')
            )
        except Exception as exc:
            logger.debug('Ticker unavailable for %s/%s: %s', market_id, outcome, exc)
            return None
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command"""
        # Support both command messages and callback queries
        user_id = update.effective_user.id
        context.user_data['screen'] = 'home'

        welcome_text = """
🤖 *Welcome to Bayse Trading Bot!*

I'm your AI trading companion on Bayse Markets. I help you:
✅ Monitor live markets
✅ Get crowd voting signals
✅ Place trades easily
✅ Track your portfolio

*Choose an option:*
"""
        
        keyboard = [
            [InlineKeyboardButton("📊 View Markets", callback_data='markets')],
            [InlineKeyboardButton("🎯 View Signals", callback_data='signals')],
            [InlineKeyboardButton("💼 My Portfolio", callback_data='portfolio')],
            [InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
            [InlineKeyboardButton("🔄 Refresh", callback_data='refresh')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # If started via a callback (button press), edit the message; otherwise send a new message
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')
    
    def _category_menu(self, screen: str):
        rows = [[InlineKeyboardButton(label, callback_data=f'{screen}:{key}')]
                for key, (label, _, _) in MARKET_CATEGORIES.items()]
        rows.append([InlineKeyboardButton('Back', callback_data='back')])
        return InlineKeyboardMarkup(rows)

    async def show_markets(self, update: Update, context: ContextTypes.DEFAULT_TYPE, category=None):
        """Show active Bayse markets, outcomes, and their current prices."""
        query = update.callback_query
        context.user_data['screen'] = 'markets'
        context.user_data['market_category'] = category
        if category is None:
            text = 'Choose a market section to browse live markets:'
            if query:
                await query.edit_message_text(text, reply_markup=self._category_menu('markets'))
            else:
                await update.message.reply_text(text, reply_markup=self._category_menu('markets'))
            return
        category_info = MARKET_CATEGORIES.get(category)
        if not category_info:
            category = None
            context.user_data['market_category'] = None
            await self.show_markets(update, context)
            return
        try:
            live_events = context.application.bot_data.get('live_market_events', {})
            try:
                events = self.api.list_events(limit=50, category=category_info[1],
                                              currency=self.market_currency,
                                              subcategory=category_info[2])
            except Exception:
                if not live_events:
                    raise
                logger.exception('Bayse REST market request failed; using cached WebSocket snapshots')
                events = []
            events = [live_events.get(str(event.get('id')), event) for event in events]
            included_ids = {str(event.get('id')) for event in events if event.get('id')}
            events.extend(event for event_id, event in live_events.items()
                          if event_id not in included_ids and _event_matches_category(event, category))
            sports_window = category in SPORTS_CLOSE_WINDOW_CATEGORIES
            lines = [f'{category_info[0]} Markets on Bayse ({self.market_currency})']
            if sports_window:
                lines.append(f'Only markets closing within the next {MAX_MARKET_CLOSE_DAYS} days')
            lines.append('')
            shown_markets = 0
            excluded_markets = 0
            missing_close_time = 0

            for event_index, event in enumerate(events, 1):
                event_title = event.get('title') or f'Event {event_index}'
                event_markets = event.get('markets', [])
                if not event_markets:
                    continue
                event_header_added = False

                for market in event_markets:
                    closing_at = _bayse_closing_time(event, market)
                    if sports_window and not _closes_within_window(event, market):
                        if closing_at is None:
                            missing_close_time += 1
                        else:
                            excluded_markets += 1
                        continue
                    if not event_header_added:
                        lines.extend([event_title, ''])
                        event_header_added = True
                    shown_markets += 1
                    range_name = (market.get('question') or market.get('title')
                                  or market.get('name') or f'Market {shown_markets}')
                    lines.append(f'  {range_name}')
                    if closing_at:
                        lines.append(f'    Closes: {closing_at:%Y-%m-%d %H:%M} UTC')

                    yes_price = positive_price(market.get('yesBuyPrice'))
                    no_price = positive_price(market.get('noBuyPrice'))
                    for outcome_index in (1, 2):
                        label = str(market.get(f'outcome{outcome_index}Label') or '').upper()
                        outcome_price = market.get(f'outcome{outcome_index}Price')
                        if outcome_price is None:
                            outcome_price = market.get('prices', {}).get(label) if isinstance(market.get('prices'), dict) else None
                        if label.startswith('YES') and yes_price is None:
                            yes_price = positive_price(outcome_price)
                        elif label.startswith('NO') and no_price is None:
                            no_price = positive_price(outcome_price)

                    live_prices = market.get('prices')
                    if isinstance(live_prices, dict):
                        yes_price = yes_price or positive_price(live_prices.get('YES'))
                        no_price = no_price or positive_price(live_prices.get('NO'))

                    market_id = market.get('id')
                    if market_id and (yes_price is None or no_price is None):
                        ticker_yes, ticker_no = await asyncio.gather(
                            self._ticker_price(market_id, 'YES'),
                            self._ticker_price(market_id, 'NO'),
                        )
                        yes_price = yes_price or ticker_yes
                        no_price = no_price or ticker_no

                    def format_price(raw_price):
                        price = positive_price(raw_price)
                        if price is None:
                            return 'unavailable'
                        # Bayse probability prices are in 0..1; NGN displays the
                        # same share price on a 100-naira payout scale.
                        if self.market_currency == 'NGN' and 0 <= price <= 1:
                            price *= 100
                        elif self.market_currency == 'USD' and 1 < price <= 100:
                            price /= 100
                        symbol = '\u20a6' if self.market_currency == 'NGN' else '$'
                        decimals = 0 if self.market_currency == 'NGN' else 2
                        return f'{symbol}{price:,.{decimals}f}'

                    lines.append(
                        f"    Yes {format_price(yes_price)}  |  No {format_price(no_price)}"
                    )
                lines.append('')

            if shown_markets == 0:
                if sports_window:
                    lines.append(
                        f'No sports markets close within the next {MAX_MARKET_CLOSE_DAYS} days '
                        f'({excluded_markets} close later or have already closed; '
                        f'{missing_close_time} had no close time).'
                    )
                else:
                    lines.append('No active markets were returned by Bayse.')

            text = '\n'.join(lines).rstrip()
            # Telegram limits a message to 4096 characters. Keep room for the buttons.
            chunks = []
            current = ''
            for line in text.splitlines():
                addition = line + '\n'
                if len(current) + len(addition) > 3900 and current:
                    chunks.append(current.rstrip())
                    current = ''
                current += addition
            if current.strip():
                chunks.append(current.rstrip())

            keyboard = [[InlineKeyboardButton('Sections', callback_data='markets'),
                         InlineKeyboardButton('Refresh', callback_data='refresh')],
                        [InlineKeyboardButton('Back', callback_data='back')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            if query:
                await query.edit_message_text(text=chunks[0], reply_markup=reply_markup)
                for chunk in chunks[1:]:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk)
            else:
                await update.message.reply_text(chunks[0], reply_markup=reply_markup)
                for chunk in chunks[1:]:
                    await update.message.reply_text(chunk)

        except Exception as e:
            if query:
                await query.edit_message_text(text=f'Could not load Bayse markets: {e}')
            else:
                await update.message.reply_text(f'Could not load Bayse markets: {e}')

    async def show_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE, category=None):
        """Analyze Bayse markets; use external sportsbook probabilities for sports."""
        query = update.callback_query
        context.user_data['screen'] = 'signals'
        context.user_data['signal_category'] = category
        if category is None:
            text = 'Choose a section to analyze. Sports signals compare sportsbook odds with Bayse prices:'
            if query:
                await query.edit_message_text(text, reply_markup=self._category_menu('signals'))
            else:
                await update.message.reply_text(text, reply_markup=self._category_menu('signals'))
            return
        category_info = MARKET_CATEGORIES.get(category)
        if not category_info:
            context.user_data['signal_category'] = None
            await self.show_signals(update, context)
            return
        if category in SPORTS_CLOSE_WINDOW_CATEGORIES:
            stats = {}
            candidates = await asyncio.to_thread(
                self._find_auto_trade_candidates, self.api, [category], stats
            )
            if category == 'player_stats':
                text = ('Player-stat signals are paused: the configured sportsbook feed retrieves '
                        'head-to-head odds only. No player-stat signal or auto-trade is generated.')
            elif not self.odds.configured:
                text = ('Sportsbook comparison is not configured. Set ODDS_API_KEY and '
                        'ODDS_API_SPORT_KEYS in the bot environment, then refresh.')
            elif not candidates:
                text = (
                    f'No estimated sportsbook edge above {SPORTS_MIN_EDGE:.0%} found in '
                    f'{stats.get("events", 0)} sports events. '
                    f'{stats.get("events_without_odds_match", 0)} had no exact sportsbook event match; '
                    f'{stats.get("events_with_stale_odds", 0)} had stale sportsbook prices. '
                    'Only exact team/outcome matches with fresh odds are considered.'
                )
            else:
                lines = [f'Sportsbook edges (minimum {SPORTS_MIN_EDGE:.0%}; before quote check):']
                for candidate in candidates[:8]:
                    gap = candidate['fair_probability'] - candidate['displayed_price']
                    lines.append(
                        f"\n{candidate['question']}\n"
                        f"Buy {candidate['outcome']} | sportsbook estimate "
                        f"{candidate['fair_probability']:.1%} | Bayse displayed "
                        f"{candidate['displayed_price']:.1%} | gap {gap:.1%}\n"
                        f"Closes {candidate['closing_at']:%Y-%m-%d %H:%M} UTC | "
                        f"{candidate['bookmakers']} bookmakers"
                    )
                text = '\n'.join(lines)
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton('Sections', callback_data='signals'),
                 InlineKeyboardButton('Refresh', callback_data='refresh')],
                [InlineKeyboardButton('Back', callback_data='back')],
            ])
            if query:
                await query.edit_message_text(text=text, reply_markup=markup)
            else:
                await update.message.reply_text(text=text, reply_markup=markup)
            return
        try:
            events = self.api.list_events(limit=50, category=category_info[1],
                                          currency=self.market_currency,
                                          subcategory=category_info[2])
            live_events = context.application.bot_data.get('live_market_events', {})
            events = [live_events.get(str(event.get('id')), event) for event in events]
            included_ids = {str(event.get('id')) for event in events if event.get('id')}
            events.extend(event for event_id, event in live_events.items()
                          if event_id not in included_ids and _event_matches_category(event, category))
            sections = []
            analyzed = 0
            sports_window = category in SPORTS_CLOSE_WINDOW_CATEGORIES
            excluded_markets = 0
            missing_close_time = 0

            for event in events:
                event_title = event.get('title', 'Untitled event')
                for market in event.get('markets', []):
                    closing_at = _bayse_closing_time(event, market)
                    if sports_window and not _closes_within_window(event, market):
                        if closing_at is None:
                            missing_close_time += 1
                        else:
                            excluded_markets += 1
                        continue
                    names, prices = self.api.extract_market_outcomes(market)
                    if len(names) < 2 and market.get('id'):
                        ticker_yes, ticker_no = await asyncio.gather(
                            self._ticker_price(market['id'], 'YES'),
                            self._ticker_price(market['id'], 'NO'),
                        )
                        names, prices = self.api.extract_market_outcomes(
                            market, fallback_prices=[ticker_yes, ticker_no]
                        )

                    if len(names) < 2:
                        continue

                    choices = list(zip(names, prices.tolist()))

                    choices.sort(key=lambda item: item[1], reverse=True)
                    best_name, best_price = choices[0]
                    second_price = choices[1][1] if len(choices) > 1 else 0.0
                    spread = best_price - second_price
                    if best_price >= 0.70 and spread >= 0.15:
                        signal, confidence = 'STRONG CROWD LEAN', 'High'
                    elif best_price >= 0.55 and spread >= 0.05:
                        signal, confidence = 'CROWD LEAN', 'Moderate'
                    else:
                        signal, confidence = 'No clear lean', 'Low'

                    market_title = market.get('question') or market.get('title')
                    question = (f'{event_title}: {market_title}'
                                if market_title and market_title != event_title
                                else event_title)
                    votes = ' | '.join(f'{name}: {price:.0%}' for name, price in choices[:4])
                    closing_line = (
                        f'Closes: {closing_at:%Y-%m-%d %H:%M} UTC\n'
                        if closing_at else ''
                    )
                    sections.append(
                        f'{question}\n{closing_line}{signal}: {best_name} ({best_price:.0%})\n'
                        f'Confidence: {confidence}\nPrices: {votes}'
                    )
                    analyzed += 1

            if not sections:
                if sports_window:
                    sections = [
                        f'No markets closing within the next {MAX_MARKET_CLOSE_DAYS} days had usable prices. '
                        f'{excluded_markets} close later or have closed; '
                        f'{missing_close_time} had no close time.'
                    ]
                else:
                    sections = ['Bayse returned no markets with two usable Yes/No prices.']
            header = f'{category_info[0]} Crowd Signals'
            if sports_window:
                header += f' (next {MAX_MARKET_CLOSE_DAYS} days)'
            header += f': analyzed {analyzed} live market(s)\n\n'
            chunks = []
            current = header
            for section in sections:
                addition = section + '\n\n'
                if len(current) + len(addition) > 3500 and current != header:
                    chunks.append(current.rstrip())
                    current = ''
                current += addition
            if current.strip():
                chunks.append(current.rstrip())

            keyboard = [[InlineKeyboardButton('Sections', callback_data='signals'),
                         InlineKeyboardButton('Refresh', callback_data='refresh')],
                        [InlineKeyboardButton('Back', callback_data='back')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            if query:
                await query.edit_message_text(text=chunks[0], reply_markup=reply_markup)
                for chunk in chunks[1:]:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk)
            else:
                await update.message.reply_text(chunks[0], reply_markup=reply_markup)
                for chunk in chunks[1:]:
                    await update.message.reply_text(chunk)

        except Exception as e:
            if query:
                await query.edit_message_text(text=f'Could not load live signals: {e}')
            else:
                await update.message.reply_text(f'Could not load live signals: {e}')

    def _user_api(self, user_id: int):
        user_data = user_sessions.get(user_id, {})
        if user_data.get('bayse_public') and user_data.get('bayse_secret'):
            return BayseAPI(user_data['bayse_public'], user_data['bayse_secret'])
        return self.api

    @staticmethod
    def _money(amount, currency):
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            amount = 0.0
        symbol = chr(0x20a6) if currency == 'NGN' else '$' if currency == 'USD' else f'{currency} '
        decimals = 0 if currency == 'NGN' else 2
        return f'{symbol}{amount:,.{decimals}f}'

    @staticmethod
    def _portfolio_buttons(active_view=False):
        rows = [
            [InlineKeyboardButton('Balance', callback_data='portfolio_balance'),
             InlineKeyboardButton('Current Positions', callback_data='portfolio_positions')],
        ]
        if active_view:
            rows.extend([
                [InlineKeyboardButton('Portfolio Menu', callback_data='portfolio'),
                 InlineKeyboardButton('Refresh', callback_data='refresh')],
                [InlineKeyboardButton('Home', callback_data='back')],
            ])
        else:
            rows.append([InlineKeyboardButton('Back', callback_data='back')])
        return InlineKeyboardMarkup(rows)

    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show navigation for wallet balance and open positions."""
        context.user_data['screen'] = 'portfolio'
        context.user_data.pop('portfolio_view', None)
        text = 'Your Bayse Portfolio\n\nChoose what you want to view:'
        query = update.callback_query
        markup = self._portfolio_buttons()
        if query:
            await query.edit_message_text(text=text, reply_markup=markup)
        else:
            await update.message.reply_text(text=text, reply_markup=markup)

    async def show_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['screen'] = 'portfolio'
        context.user_data['portfolio_view'] = 'balance'
        query = update.callback_query
        try:
            user_api = self._user_api(update.effective_user.id)
            balance = await asyncio.to_thread(user_api.get_balance)
            assets = balance.get('assets', [])
            lines = ['Wallet Balance', '']
            for asset in assets:
                currency = asset.get('symbol') or asset.get('currency') or 'USD'
                available = asset.get('availableBalance', asset.get('available', 0))
                pending = asset.get('pendingBalance', 0)
                lines.append(f"{currency}: {self._money(available, currency)} available")
                if pending:
                    lines.append(f"  {self._money(pending, currency)} pending")
            if not assets:
                lines.append('Bayse returned no wallet assets.')
            text = '\n'.join(lines)
            markup = self._portfolio_buttons(active_view=True)
            if query:
                await query.edit_message_text(text=text, reply_markup=markup)
            else:
                await update.message.reply_text(text=text, reply_markup=markup)
        except Exception as exc:
            text = f'Could not load wallet balance: {exc}'
            if query:
                await query.edit_message_text(text=text, reply_markup=self._portfolio_buttons(active_view=True))
            else:
                await update.message.reply_text(text, reply_markup=self._portfolio_buttons(active_view=True))

    async def show_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['screen'] = 'portfolio'
        context.user_data['portfolio_view'] = 'positions'
        query = update.callback_query
        try:
            user_api = self._user_api(update.effective_user.id)
            portfolio = await asyncio.to_thread(user_api.get_portfolio)
            positions = portfolio.get('outcomeBalances', portfolio.get('positions', []))
            lines = [f'Current Positions ({len(positions)})', '']
            for position in positions:
                market_data = position.get('market') or {}
                event_data = market_data.get('event') or {}
                event_title = event_data.get('title')
                market_title = market_data.get('title') or 'Unknown market'
                market_display = (f'{event_title}\n  {market_title}'
                                  if event_title and event_title != market_title
                                  else market_title)
                outcome = position.get('outcomeLabel') or position.get('outcome') or position.get('outcomeName', 'Unknown')
                quantity = position.get('balance', position.get('availableBalance', position.get('quantity', 0)))
                cost = position.get('cost', position.get('costBasis', 0))
                value = position.get('currentValue', 0)
                currency = position.get('currency', self.market_currency)
                try:
                    pnl = float(value) - float(cost)
                except (TypeError, ValueError):
                    pnl = 0.0
                pnl_percent = position.get('percentageChange')
                if pnl_percent is None:
                    try:
                        pnl_percent = pnl / float(cost) * 100 if float(cost) else 0
                    except (TypeError, ValueError):
                        pnl_percent = 0
                try:
                    quantity = float(quantity)
                    pnl_percent = float(pnl_percent)
                except (TypeError, ValueError):
                    quantity, pnl_percent = 0.0, 0.0
                lines.extend([
                    market_display,
                    f'{outcome}: {quantity:,.2f} shares',
                    f'Cost: {self._money(cost, currency)}  |  Value: {self._money(value, currency)}',
                    f'P&L: {self._money(pnl, currency)} ({pnl_percent:+.2f}%)',
                    '',
                ])
            if positions:
                total_cost = portfolio.get('portfolioCost')
                total_value = portfolio.get('portfolioCurrentValue')
                currency = positions[0].get('currency', self.market_currency)
                if total_cost is not None:
                    lines.append(f'Total cost: {self._money(total_cost, currency)}')
                if total_value is not None:
                    lines.append(f'Total value: {self._money(total_value, currency)}')
                total_change = portfolio.get('portfolioPercentageChange')
                if total_change is not None:
                    lines.append(f'Total change: {float(total_change):+.2f}%')
            else:
                lines.append('No current positions were returned by Bayse.')
            text = '\n'.join(lines).rstrip()
            markup = self._portfolio_buttons(active_view=True)
            if query:
                await query.edit_message_text(text=text, reply_markup=markup)
            else:
                await update.message.reply_text(text=text, reply_markup=markup)
        except Exception as exc:
            text = f'Could not load current positions: {exc}'
            if query:
                await query.edit_message_text(text=text, reply_markup=self._portfolio_buttons(active_view=True))
            else:
                await update.message.reply_text(text, reply_markup=self._portfolio_buttons(active_view=True))

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button presses"""
        query = update.callback_query
        # Acknowledge immediately so Telegram stops showing the button spinner while
        # the Bayse API request is running.
        await query.answer()
        
        if query.data == 'markets':
            await self.show_markets(update, context)
        elif query.data == 'signals':
            await self.show_signals(update, context)
        elif query.data.startswith('markets:'):
            await self.show_markets(update, context, query.data.split(':', 1)[1])
        elif query.data.startswith('signals:'):
            await self.show_signals(update, context, query.data.split(':', 1)[1])
        elif query.data == 'portfolio':
            await self.show_portfolio(update, context)
        elif query.data == 'portfolio_balance':
            await self.show_balance(update, context)
        elif query.data == 'portfolio_positions':
            await self.show_positions(update, context)
        elif query.data == 'settings':
            await self.show_settings(update, context)
        elif query.data.startswith('risk:'):
            risk = query.data.split(':', 1)[1]
            if risk in RISK_FRACTIONS:
                preferences = get_trade_preferences(update.effective_user.id)
                save_trade_preferences(update.effective_user.id, risk_level=risk,
                                       auto_enabled=preferences['auto_enabled'],
                                       auto_categories=preferences['auto_categories'])
            await self.show_settings(update, context)
        elif query.data.startswith('auto_category:'):
            category = query.data.split(':', 1)[1]
            if category in AUTO_TRADE_CATEGORIES:
                preferences = get_trade_preferences(update.effective_user.id)
                selected = set(preferences['auto_categories'])
                if category in selected:
                    selected.remove(category)
                else:
                    selected.add(category)
                save_trade_preferences(
                    update.effective_user.id,
                    risk_level=preferences['risk_level'],
                    auto_enabled=preferences['auto_enabled'],
                    auto_categories=selected,
                )
            await self.show_settings(update, context)
        elif query.data == 'auto_toggle':
            preferences = get_trade_preferences(update.effective_user.id)
            enabled = not preferences['auto_enabled']
            save_trade_preferences(update.effective_user.id,
                                   risk_level=preferences['risk_level'],
                                   auto_enabled=enabled,
                                   auto_categories=preferences['auto_categories'])
            await self.show_settings(update, context)
        elif query.data == 'refresh':
            screen = context.user_data.get('screen', 'home')
            if screen == 'markets':
                await self.show_markets(update, context, context.user_data.get('market_category'))
            elif screen == 'signals':
                await self.show_signals(update, context, context.user_data.get('signal_category'))
            elif screen == 'portfolio':
                portfolio_view = context.user_data.get('portfolio_view')
                if portfolio_view == 'balance':
                    await self.show_balance(update, context)
                elif portfolio_view == 'positions':
                    await self.show_positions(update, context)
                else:
                    await self.show_portfolio(update, context)
            elif screen == 'settings':
                await self.show_settings(update, context)
            else:
                await self.start(update, context)
        elif query.data == 'back':
            await self.start(update, context)
    
    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show persistent strategy, risk, and live automation settings."""
        query = update.callback_query
        context.user_data['screen'] = 'settings'
        preferences = get_trade_preferences(update.effective_user.id)
        risk = preferences['risk_level']
        auto_status = 'ON' if preferences['auto_enabled'] else 'OFF'
        cycle_status = context.application.bot_data.get('auto_trade_status', {}).get(update.effective_user.id)
        selected_categories = set(preferences['auto_categories'])
        category_buttons = [
            InlineKeyboardButton(
                f"{'✅' if category in selected_categories else '⬜'} {label}",
                callback_data=f'auto_category:{category}'
            )
            for category, label in AUTO_TRADE_CATEGORY_LABELS.items()
        ]
        risk_labels = {'conservative': 'Conservative (20%)',
                       'moderate': 'Moderate (50%)',
                       'aggressive': 'Aggressive (80%)'}
        text = (
            'Trading configuration\n\n'
            'Signal: No-vig sportsbook consensus compared with Bayse executable quotes\n'
            f'Minimum edge after quote: {SPORTS_MIN_EDGE:.0%}\n'
            'Sportsbook feed: The Odds API head-to-head markets; exact team match and fresh '
            'quotes required. Player props are skipped by this feed configuration.\n'
            f'Close window: Markets closing within {MAX_MARKET_CLOSE_DAYS} days\n'
            'Auto-trade markets: ' + (', '.join(
                label for category, label in AUTO_TRADE_CATEGORY_LABELS.items()
                if category in selected_categories
            ) or 'None selected') + '\n'
            'Risk level: ' + risk_labels.get(risk, risk_labels['moderate']) + '\n'
            'Automated live orders: ' + auto_status + '\n'
            'Last auto-trade check: ' + (cycle_status or 'Waiting for the first scan after startup.') + '\n'
            'Maximum open exposure: ' + self._money(MAX_OPEN_EXPOSURE, self.market_currency) + '\n'
            'Each order is capped at the selected risk percentage of available wallet balance '
            'and the remaining exposure limit. Set ODDS_API_KEY and ODDS_API_SPORT_KEYS '
            'in the bot environment to enable sportsbook signals.'
        )
        keyboard = [
            category_buttons,
            [InlineKeyboardButton('Conservative 20%', callback_data='risk:conservative'),
             InlineKeyboardButton('Moderate 50%', callback_data='risk:moderate')],
            [InlineKeyboardButton('Aggressive 80%', callback_data='risk:aggressive')],
            [InlineKeyboardButton('Turn auto-trading ' + ('OFF' if preferences['auto_enabled'] else 'ON'),
                                  callback_data='auto_toggle')],
            [InlineKeyboardButton('Refresh', callback_data='refresh')],
            [InlineKeyboardButton('Back', callback_data='back')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if query:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text=text, reply_markup=reply_markup)

    def run(self):
        """Run the Telegram bot"""
        init_db()
        load_all_user_keys()
        
        # Create the Application
        application = (Application.builder().token(self.telegram_token)
                       .post_init(self.start_market_data_listener)
                       .post_shutdown(self.stop_market_data_listener)
                       .build())
        application.bot_data['bayse_bot'] = self
        
        # Add handlers
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("predict", predict_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("setkeys", setkeys_command))
        application.add_handler(CommandHandler("clearkeys", clearkeys_command))
        application.add_handler(CallbackQueryHandler(self.button_callback))
        
        # Start the bot
        print("🤖 Bayse Telegram Bot started!")
        print("Send /start to activate")
        
        application.run_polling()


# ===== SIMPLE TEXT COMMANDS =====

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    help_text = """
📚 *Available Commands:*

/start - Show main menu
/markets - List active markets
/signals - Show crowd voting signals
/portfolio - View your positions
/help - Show this message

Use the buttons below each message to navigate!
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def predict_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show sportsbook-vs-Bayse sports signals."""
    bot_obj: BayseTelegramBot = context.application.bot_data.get('bayse_bot')
    if not bot_obj:
        await update.message.reply_text("Bot not initialized.")
        return

    try:
        scan_stats = {}
        preferences = get_trade_preferences(update.effective_user.id)
        categories = preferences['auto_categories'] or tuple(AUTO_TRADE_CATEGORIES)
        recommendations = await asyncio.to_thread(
            bot_obj._find_auto_trade_candidates,
            bot_obj.api,
            categories,
            scan_stats,
        )
        if not recommendations:
            await update.message.reply_text(
                scan_stats.get('odds_configuration_error') or
                (f'No sportsbook edge above {SPORTS_MIN_EDGE:.0%} found across '
                 f'{scan_stats.get("events", 0)} sports events and '
                 f'{scan_stats.get("markets", 0)} Bayse markets. '
                 f'{scan_stats.get("events_without_odds_match", 0)} had no exact odds match; '
                 f'{scan_stats.get("events_with_stale_odds", 0)} had stale odds. '
                 'Player-stat props are not enabled in this feed configuration.')
            )
            return

        lines = [f'Sportsbook vs Bayse signals (minimum edge {SPORTS_MIN_EDGE:.0%})']
        for candidate in recommendations[:8]:
            lines.append(
                f"\n{candidate['category'].replace('_', ' ').title()} / "
                f"{candidate['strategy']}\n{candidate['question']}\n"
                f"Bayse closes: {candidate['closing_at']:%Y-%m-%d %H:%M} UTC\n"
                f"Buy {candidate['outcome']}: {candidate['reason']}"
            )
        lines.append('\nSignals only; /predict does not submit live orders.')
        await update.message.reply_text('\n'.join(lines))

    except Exception as e:
        await update.message.reply_text(f"Error during prediction: {e}")


# ===== MAIN =====

if __name__ == '__main__':
    # Read tokens and keys from environment variables for safety
    TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
    BAYSE_PUBLIC_KEY = os.environ.get('BAYSE_PUBLIC_KEY')
    BAYSE_SECRET_KEY = os.environ.get('BAYSE_SECRET_KEY')

    if not TELEGRAM_TOKEN:
        print('Please set TELEGRAM_TOKEN environment variable and re-run the script')
    elif not BAYSE_PUBLIC_KEY or not BAYSE_SECRET_KEY:
        print('Please set BAYSE_PUBLIC_KEY and BAYSE_SECRET_KEY environment variables')
    else:
        bot = BayseTelegramBot(
            TELEGRAM_TOKEN,
            BAYSE_PUBLIC_KEY,
            BAYSE_SECRET_KEY
        )
        # store reference for handlers
        application = None
        try:
            # Initialize DB and preload user keys
            init_db()
            load_all_user_keys()

            # Build application and store bot object for handlers
            application = (Application.builder().token(TELEGRAM_TOKEN)
                           .post_init(bot.start_market_data_listener)
                           .post_shutdown(bot.stop_market_data_listener)
                           .build())
            application.bot_data['bayse_bot'] = bot
            application.add_handler(CommandHandler("start", bot.start))
            application.add_handler(CommandHandler("predict", predict_command))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("setkeys", setkeys_command))
            application.add_handler(CommandHandler("clearkeys", clearkeys_command))
            application.add_handler(CallbackQueryHandler(bot.button_callback))

            print('Bayse Telegram Bot started — send /start to interact')
            application.run_polling()
        except Exception as e:
            print('Failed to start bot:', e)
