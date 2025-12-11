#!/usr/bin/env python3
"""
POLYNEKO v2 - Directional Trading with Trailing

Strategy:
- Monitor Polymarket token prices (YES/NO) via WebSocket
- Make initial prediction based on Binance momentum
- TRAILING: If token price moves against us, hedge by buying opposite side
- Multiple bets per slot allowed

"""

import os
import json
import asyncio
import aiohttp
import logging
import copy
import time
import sqlite3
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# Dashboard (optional)
try:
    from polyneko_dashboard import Dashboard
    DASHBOARD_AVAILABLE = True
except ImportError:
    DASHBOARD_AVAILABLE = False

# Polymarket CLOB client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    BUY = None
    SELL = None
    logger_temp = logging.getLogger(__name__)
    logger_temp.warning("py_clob_client not installed - real orders disabled")

# ========================== LOGGING ==========================

# Create logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Console handler (INFO level)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_format = logging.Formatter('%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
console_handler.setFormatter(console_format)

# File handler (DEBUG level - captures everything)
LOG_FILE = os.getenv("LOG_FILE", "polyneko.log")
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_format = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(file_format)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Log startup
logger.info(f"Logging to file: {LOG_FILE}")

# ========================== CONFIG ==========================

@dataclass
class Config:
    # API Keys
    polymarket_api_key: str = ""
    polymarket_secret: str = ""
    polymarket_passphrase: str = ""
    
    # Wallet
    wallet_address: str = ""
    private_key: str = ""
    funder_address: str = ""        # Proxy wallet (if different from main)
    signature_type: int = 1         # 0=EOA/MetaMask, 1=Magic/Email, 2=Browser proxy
    
    # Trading params
    bet_size: float = 25.0
    max_position: float = 200.0
    
    # Trailing params
    trail_trigger: float = 0.10
    trail_size: float = 0.5
    max_hedges: int = 3
    hedge_confirm_seconds: int = 5  # Drop must persist for X seconds before hedge
    
    # Order execution
    order_max_retries: int = 15          # FOK retries (more is safer)
    order_price_increment: float = 0.01  # Price increment per retry
    order_use_gtc_fallback: bool = False # GTC is risky - might not fill
    
    # Signal params
    signal_cooldown: int = 30
    
    # Symbols
    symbols: list = field(default_factory=lambda: ["BTC", "ETH", "SOL", "XRP"])
    
    # Mode
    simulation_mode: bool = True
    
    # Discord
    discord_webhook: str = ""
    
    # URLs
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    # Database
    db_path: str = "polyneko.db"
    
    # Dashboard
    dashboard_enabled: bool = True
    dashboard_port: int = 8080
    
    @classmethod
    def from_env(cls):
        load_dotenv()
        
        # Parse symbols
        symbols_str = os.getenv("SYMBOLS", "BTC,ETH,SOL,XRP")
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        
        return cls(
            # API Keys
            polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
            polymarket_secret=os.getenv("POLYMARKET_SECRET", ""),
            polymarket_passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
            
            # Wallet
            wallet_address=os.getenv("WALLET_ADDRESS", ""),
            private_key=os.getenv("PRIVATE_KEY", ""),
            funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS", ""),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "1")),
            
            # Trading
            bet_size=float(os.getenv("BET_SIZE", "25")),
            max_position=float(os.getenv("MAX_POSITION", "200")),
            
            # Trailing
            trail_trigger=float(os.getenv("TRAIL_TRIGGER", "0.10")),
            trail_size=float(os.getenv("TRAIL_SIZE", "0.5")),
            max_hedges=int(os.getenv("MAX_HEDGES", "3")),
            hedge_confirm_seconds=int(os.getenv("HEDGE_CONFIRM_SECONDS", "5")),
            
            # Signal
            signal_cooldown=int(os.getenv("SIGNAL_COOLDOWN", "30")),
            
            # Order execution
            order_max_retries=int(os.getenv("ORDER_MAX_RETRIES", "15")),
            order_price_increment=float(os.getenv("ORDER_PRICE_INCREMENT", "0.01")),
            order_use_gtc_fallback=os.getenv("ORDER_USE_GTC_FALLBACK", "false").lower() == "true",
            
            # Symbols
            symbols=symbols,
            
            # Mode
            simulation_mode=os.getenv("SIMULATION_MODE", "true").lower() == "true",
            
            # Discord
            discord_webhook=os.getenv("DISCORD_WEBHOOK", ""),
            
            # URLs
            gamma_url=os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com"),
            clob_url=os.getenv("CLOB_URL", "https://clob.polymarket.com"),
            ws_url=os.getenv("WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            
            # Database
            db_path=os.getenv("DB_PATH", "polyneko.db"),
            
            # Dashboard
            dashboard_enabled=os.getenv("DASHBOARD_ENABLED", "true").lower() == "true",
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
        )
    
    def print_config(self):
        """Print configuration summary"""
        logger.info("=" * 60)
        logger.info("CONFIGURATION")
        logger.info("=" * 60)
        logger.info(f"  Symbols: {', '.join(self.symbols)}")
        logger.info(f"  Bet Size: ${self.bet_size}")
        logger.info(f"  Max Position: ${self.max_position}")
        logger.info(f"  Trail Trigger: {self.trail_trigger:.0%}")
        logger.info(f"  Trail Size: {self.trail_size:.0%}")
        logger.info(f"  Max Hedges: {self.max_hedges}")
        logger.info(f"  Signal Cooldown: {self.signal_cooldown}s")
        logger.info(f"  FOK Retries: {self.order_max_retries} (increment ${self.order_price_increment}, GTC fallback: {self.order_use_gtc_fallback})")
        logger.info(f"  Simulation: {self.simulation_mode}")
        logger.info(f"  Discord: {'✓' if self.discord_webhook else '✗'}")
        logger.info(f"  Dashboard: {'http://localhost:' + str(self.dashboard_port) if self.dashboard_enabled else '✗'}")
        logger.info(f"  Database: {self.db_path}")
        logger.info("=" * 60)

# ========================== DATA CLASSES ==========================

@dataclass
class Trade:
    side: str           # YES or NO
    shares: float
    price: float
    cost: float
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class Position:
    symbol: str
    market_id: str
    
    # YES side
    yes_shares: float = 0.0
    yes_cost: float = 0.0
    yes_entry_price: float = 0.0    # First entry price for trailing
    
    # NO side
    no_shares: float = 0.0
    no_cost: float = 0.0
    no_entry_price: float = 0.0     # First entry price for trailing
    
    # Tracking
    trades: list = field(default_factory=list)
    hedge_count: int = 0
    last_trade_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Initial prediction
    initial_side: str = ""          # What we predicted first
    
    # Hedge confirmation tracking
    hedge_trigger_time: datetime = None  # When drop first detected
    hedge_trigger_drop: float = 0.0      # Drop % when first detected
    
    @property
    def total_cost(self) -> float:
        return self.yes_cost + self.no_cost
    
    @property
    def avg_yes_price(self) -> float:
        return self.yes_cost / self.yes_shares if self.yes_shares > 0 else 0
    
    @property
    def avg_no_price(self) -> float:
        return self.no_cost / self.no_shares if self.no_shares > 0 else 0
    
    def add_trade(self, side: str, shares: float, price: float, cost: float, reason: str):
        trade = Trade(side=side, shares=shares, price=price, cost=cost, reason=reason)
        self.trades.append(trade)
        
        if side == "YES":
            if self.yes_shares == 0:
                self.yes_entry_price = price  # First entry
            self.yes_shares += shares
            self.yes_cost += cost
        else:
            if self.no_shares == 0:
                self.no_entry_price = price   # First entry
            self.no_shares += shares
            self.no_cost += cost
        
        self.last_trade_time = datetime.now(timezone.utc)

# ========================== ORDERBOOK ==========================

class OrderBook:
    """Track best bid/ask for tokens"""
    
    def __init__(self):
        self.books: dict[str, dict] = {}  # token_id -> {bids, asks, best_bid, best_ask}
    
    def update(self, token_id: str, bids: list, asks: list):
        """Update orderbook with new data"""
        try:
            # Parse bids - handle both dict and list formats
            parsed_bids = []
            for b in bids:
                if not b:
                    continue
                if isinstance(b, dict):
                    price = float(b.get('price', 0) or 0)
                    size = float(b.get('size', 0) or 0)
                elif isinstance(b, (list, tuple)) and len(b) >= 2:
                    price = float(b[0])
                    size = float(b[1])
                else:
                    continue
                if price > 0:
                    parsed_bids.append({'price': price, 'size': size})
            
            # Parse asks
            parsed_asks = []
            for a in asks:
                if not a:
                    continue
                if isinstance(a, dict):
                    price = float(a.get('price', 0) or 0)
                    size = float(a.get('size', 0) or 0)
                elif isinstance(a, (list, tuple)) and len(a) >= 2:
                    price = float(a[0])
                    size = float(a[1])
                else:
                    continue
                if price > 0:
                    parsed_asks.append({'price': price, 'size': size})
            
            # Sort
            parsed_bids.sort(key=lambda x: x['price'], reverse=True)
            parsed_asks.sort(key=lambda x: x['price'])
            
            best_bid = parsed_bids[0]['price'] if parsed_bids else 0.0
            best_ask = parsed_asks[0]['price'] if parsed_asks else 1.0
            
            self.books[token_id] = {
                'bids': parsed_bids,
                'asks': parsed_asks,
                'best_bid': best_bid,
                'best_ask': best_ask,
                'mid': (best_bid + best_ask) / 2 if best_bid and best_ask else 0.5,
            }
        except Exception as e:
            logger.debug(f"OrderBook update error: {e}")
    
    def get_buy_price(self, token_id: str) -> float:
        """Price to buy (best ask)"""
        book = self.books.get(token_id, {})
        return book.get('best_ask', 1.0)
    
    def get_mid_price(self, token_id: str) -> float:
        """Mid price"""
        book = self.books.get(token_id, {})
        return book.get('mid', 0.5)

# ========================== BINANCE (for initial momentum & settlement) ==========================

class BinanceClient:
    """Get momentum signal from Binance"""
    
    SYMBOL_MAP = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT", 
        "SOL": "SOLUSDT",
        "XRP": "XRPUSDT",
    }
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def start(self):
        self.session = aiohttp.ClientSession()
        
    async def stop(self):
        if self.session:
            await self.session.close()
    
    async def get_momentum(self, symbol: str, window: int = 5) -> tuple[float, float]:
        """Get momentum % change and current price"""
        binance_symbol = self.SYMBOL_MAP.get(symbol)
        if not binance_symbol or not self.session:
            return 0.0, 0.0
        
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval=1m&limit={window + 1}"
            async with self.session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    klines = await resp.json()
                    if len(klines) >= 2:
                        old_close = float(klines[0][4])
                        new_close = float(klines[-1][4])
                        momentum = ((new_close - old_close) / old_close) * 100
                        return momentum, new_close
        except Exception as e:
            logger.error(f"[{symbol}] Binance error: {e}")
        
        return 0.0, 0.0
    
    async def get_price(self, symbol: str) -> float:
        """Get current price"""
        binance_symbol = self.SYMBOL_MAP.get(symbol)
        if not binance_symbol or not self.session:
            return 0.0
        
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}"
            async with self.session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['price'])
        except:
            pass
        return 0.0

# ========================== DATABASE ==========================

class Database:
    """SQLite database for trade history and analytics"""
    
    def __init__(self, db_path: str = "polyneko.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
    
    def _init_tables(self):
        """Create tables if they don't exist"""
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                slot TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                cost REAL NOT NULL,
                is_hedge INTEGER DEFAULT 0,
                reason TEXT,
                order_id TEXT,
                attempts INTEGER DEFAULT 1
            );
            
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                winner TEXT NOT NULL,
                yes_shares REAL DEFAULT 0,
                yes_cost REAL DEFAULT 0,
                no_shares REAL DEFAULT 0,
                no_cost REAL DEFAULT 0,
                payout REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                hedge_count INTEGER DEFAULT 0
            );
            
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                stat_name TEXT NOT NULL,
                stat_value REAL NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_settlements_timestamp ON settlements(timestamp);
            CREATE INDEX IF NOT EXISTS idx_settlements_slot ON settlements(slot);
        ''')
        self.conn.commit()
    
    def save_trade(self, slot: str, symbol: str, side: str, shares: float, price: float, 
                   cost: float, is_hedge: bool, reason: str, order_id: str = None, attempts: int = 1):
        """Save a trade to database"""
        self.conn.execute('''
            INSERT INTO trades (timestamp, slot, symbol, side, shares, price, cost, is_hedge, reason, order_id, attempts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            slot, symbol, side, shares, price, cost,
            1 if is_hedge else 0, reason, order_id, attempts
        ))
        self.conn.commit()
    
    def save_settlement(self, slot: str, symbol: str, winner: str, 
                        yes_shares: float, yes_cost: float, no_shares: float, no_cost: float,
                        payout: float, pnl: float, hedge_count: int):
        """Save settlement result"""
        self.conn.execute('''
            INSERT INTO settlements (timestamp, slot, symbol, winner, yes_shares, yes_cost, no_shares, no_cost, payout, pnl, hedge_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            slot, symbol, winner, yes_shares, yes_cost, no_shares, no_cost, payout, pnl, hedge_count
        ))
        self.conn.commit()
    
    def get_recent_trades(self, limit: int = 50) -> list:
        """Get recent trades"""
        cursor = self.conn.execute('''
            SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
        ''', (limit,))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_recent_settlements(self, limit: int = 50) -> list:
        """Get recent settlements"""
        cursor = self.conn.execute('''
            SELECT * FROM settlements ORDER BY timestamp DESC LIMIT ?
        ''', (limit,))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_stats(self) -> dict:
        """Get aggregate statistics"""
        stats = {}
        
        # Total trades
        cursor = self.conn.execute('SELECT COUNT(*) as cnt FROM trades')
        stats['total_trades'] = cursor.fetchone()['cnt']
        
        # Total settlements
        cursor = self.conn.execute('SELECT COUNT(*) as cnt FROM settlements')
        stats['total_settlements'] = cursor.fetchone()['cnt']
        
        # Total P&L
        cursor = self.conn.execute('SELECT COALESCE(SUM(pnl), 0) as total FROM settlements')
        stats['total_pnl'] = cursor.fetchone()['total']
        
        # Win rate
        cursor = self.conn.execute('SELECT COUNT(*) as cnt FROM settlements WHERE pnl > 0')
        wins = cursor.fetchone()['cnt']
        stats['win_rate'] = (wins / stats['total_settlements'] * 100) if stats['total_settlements'] > 0 else 0
        
        # Total cost
        cursor = self.conn.execute('SELECT COALESCE(SUM(cost), 0) as total FROM trades')
        stats['total_cost'] = cursor.fetchone()['total']
        
        # ROI
        stats['roi'] = (stats['total_pnl'] / stats['total_cost'] * 100) if stats['total_cost'] > 0 else 0
        
        # By symbol
        cursor = self.conn.execute('''
            SELECT symbol, COUNT(*) as trades, SUM(pnl) as pnl, 
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM settlements GROUP BY symbol
        ''')
        stats['by_symbol'] = [dict(row) for row in cursor.fetchall()]
        
        # Today's stats
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        cursor = self.conn.execute('''
            SELECT COUNT(*) as cnt, COALESCE(SUM(pnl), 0) as pnl
            FROM settlements WHERE timestamp LIKE ?
        ''', (f'{today}%',))
        row = cursor.fetchone()
        stats['today_settlements'] = row['cnt']
        stats['today_pnl'] = row['pnl']
        
        # Hourly P&L for chart (last 24h)
        cursor = self.conn.execute('''
            SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour, SUM(pnl) as pnl
            FROM settlements 
            WHERE timestamp > datetime('now', '-24 hours')
            GROUP BY hour ORDER BY hour
        ''')
        stats['hourly_pnl'] = [dict(row) for row in cursor.fetchall()]
        
        return stats
    
    def get_performance_by_hour(self) -> list:
        """Get performance grouped by hour of day"""
        cursor = self.conn.execute('''
            SELECT strftime('%H', timestamp) as hour, 
                   COUNT(*) as trades,
                   SUM(pnl) as pnl,
                   AVG(pnl) as avg_pnl,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM settlements 
            GROUP BY hour ORDER BY hour
        ''')
        return [dict(row) for row in cursor.fetchall()]
    
    def close(self):
        """Close database connection"""
        self.conn.close()

# ========================== DISCORD ==========================

class Discord:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def start(self):
        if self.webhook_url:
            self.session = aiohttp.ClientSession()
            logger.info("✓ Discord webhook configured")
        else:
            logger.warning("⚠ Discord webhook not configured")
            
    async def stop(self):
        if self.session:
            await self.session.close()
    
    async def send(self, content: str = "", embed: dict = None):
        if not self.webhook_url:
            logger.debug("Discord: No webhook URL configured")
            return
        if not self.session:
            logger.warning("Discord: Session not initialized!")
            return
        try:
            payload = {}
            if content:
                payload["content"] = content
            if embed:
                payload["embeds"] = [embed]
            async with self.session.post(self.webhook_url, json=payload, timeout=10) as resp:
                if resp.status != 204:
                    logger.warning(f"Discord: Unexpected status {resp.status}")
        except asyncio.TimeoutError:
            logger.error("Discord: Timeout sending message")
        except Exception as e:
            logger.error(f"Discord error: {e}")
    
    async def send_trade(self, symbol: str, side: str, shares: float, price: float, reason: str, is_hedge: bool = False):
        color = 0x00ff00 if side == "YES" else 0xff0000
        emoji = "🛡️" if is_hedge else ("📈" if side == "YES" else "📉")
        title = f"{emoji} {symbol}: {'HEDGE' if is_hedge else 'BET'} {side}"
        
        embed = {
            "title": title,
            "description": f"**Shares:** {shares:.0f}\n"
                          f"**Price:** ${price:.3f}\n"
                          f"**Cost:** ${shares * price:.2f}\n"
                          f"**Reason:** {reason}",
            "color": color,
        }
        await self.send(embed=embed)
    
    async def send_settlement(self, slot: str, positions: dict, total_pnl: float, results: dict):
        if not self.webhook_url or not self.session:
            logger.warning("Discord not configured, skipping settlement report")
            return
            
        try:
            color = 0x00ff00 if total_pnl > 0 else 0xff0000
            emoji = "🎉" if total_pnl > 0 else "📉"
            
            fields = []
            total_cost = 0
            for market_id, pos in positions.items():
                if pos.total_cost == 0:
                    continue
                total_cost += pos.total_cost
                r = results.get(market_id, {})
                winner = r.get('winner', '?')
                pnl = r.get('pnl', 0)
                status = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                
                fields.append({
                    "name": f"{status} {pos.symbol}",
                    "value": f"Winner: {winner}\nYES: {pos.yes_shares:.0f} NO: {pos.no_shares:.0f}\nHedges: {pos.hedge_count}\nP&L: ${pnl:+.2f}",
                    "inline": True
                })
            
            # Calculate ROI
            roi_text = ""
            if total_cost > 0:
                roi = (total_pnl / total_cost) * 100
                roi_text = f"\n**ROI:** {roi:+.1f}%"
            
            embed = {
                "title": f"{emoji} Settlement: {slot}",
                "description": f"**Mode:** SIM\n**Total Cost:** ${total_cost:.2f}\n**Total P&L:** ${total_pnl:+.2f}{roi_text}",
                "color": color,
                "fields": fields,
                "footer": {"text": f"PolyNeko v2 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}
            }
            
            logger.info(f"📤 Sending Discord settlement report...")
            await self.send(embed=embed)
            logger.info(f"✅ Discord report sent")
            
        except Exception as e:
            logger.error(f"Discord settlement error: {e}")

# ========================== MAIN BOT ==========================

class PolyNeko:
    def __init__(self, config: Config):
        self.config = config
        self.running = False
        
        # Components
        self.orderbook = OrderBook()
        self.binance = BinanceClient()
        self.discord = Discord(config.discord_webhook)
        self.db = Database(config.db_path)
        
        # CLOB Client for real orders
        self.clob = None
        self.executor = None
        if not config.simulation_mode and CLOB_AVAILABLE:
            self._init_clob_client()
        
        # WebSocket
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.ws_session: Optional[aiohttp.ClientSession] = None
        
        # State
        self.active_markets: dict = {}          # slug -> market data
        self.token_to_market: dict = {}         # token_id -> slug
        self.positions: dict[str, Position] = {} # slug -> Position
        self.start_prices: dict[str, float] = {} # symbol -> Binance price at slot start
        self.current_slot: str = ""             # Current slot name
        
        # Stats
        self.stats = {
            'trades': 0,
            'hedges': 0,
            'wss_messages': 0,
            'slots': 0,
            'total_pnl': 0.0,
            'orders_sent': 0,
            'orders_filled': 0,
            'orders_failed': 0,
            'start_time': time.time(),
        }
        
        # Dashboard
        self.dashboard = None
        if config.dashboard_enabled and DASHBOARD_AVAILABLE:
            self.dashboard = Dashboard(db_path=config.db_path, port=config.dashboard_port)
    
    def _init_clob_client(self):
        """Initialize CLOB client for real orders"""
        private_key = self.config.private_key
        if not private_key:
            logger.error("PRIVATE_KEY not set - cannot trade!")
            return
        
        # Ensure 0x prefix
        if not private_key.startswith('0x'):
            private_key = '0x' + private_key
        
        try:
            from eth_account import Account
            wallet = Account.from_key(private_key)
            
            # Use funder_address (proxy wallet) if provided, otherwise use main wallet
            funder = self.config.funder_address or self.config.wallet_address or wallet.address
            
            self.clob = ClobClient(
                host=self.config.clob_url,
                key=private_key,
                chain_id=137,
                signature_type=self.config.signature_type,
                funder=funder
            )
            
            # Setup API credentials
            self._setup_api_creds()
            
            # Thread pool for sync CLOB calls
            self.executor = ThreadPoolExecutor(max_workers=4)
            
            sig_type_names = {0: "EOA/MetaMask", 1: "Magic/Email", 2: "Browser proxy"}
            sig_name = sig_type_names.get(self.config.signature_type, "Unknown")
            
            logger.info(f"✓ CLOB client initialized")
            logger.info(f"  Signer: {wallet.address}")
            logger.info(f"  Signature type: {self.config.signature_type} ({sig_name})")
            if funder != wallet.address:
                logger.info(f"  Funder (proxy): {funder}")
            
        except Exception as e:
            logger.error(f"CLOB client init failed: {e}")
            self.clob = None
    
    def _setup_api_creds(self):
        """Load or generate API credentials"""
        api_key = self.config.polymarket_api_key
        api_secret = self.config.polymarket_secret
        api_passphrase = self.config.polymarket_passphrase
        
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
            self.clob.set_api_creds(creds)
            logger.info("✓ API credentials loaded from .env")
        else:
            try:
                creds = self.clob.create_or_derive_api_creds()
                self.clob.set_api_creds(creds)
                logger.info("✓ API credentials derived")
                logger.info(f"  Save these to .env:")
                logger.info(f"  POLYMARKET_API_KEY={creds.api_key}")
                logger.info(f"  POLYMARKET_SECRET={creds.api_secret}")
                logger.info(f"  POLYMARKET_PASSPHRASE={creds.api_passphrase}")
            except Exception as e:
                logger.error(f"API credentials error: {e}")
    
    # ==================== MARKET DISCOVERY ====================
    
    def get_slug(self, symbol: str) -> str:
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        slot = (now.minute // 15) * 15
        ts = int(now.replace(minute=slot, second=0, microsecond=0)
                 .astimezone(ZoneInfo("UTC")).timestamp())
        return f"{symbol.lower()}-updown-15m-{ts}"
    
    async def fetch_markets(self) -> bool:
        """Fetch markets, return True if changed"""
        async with aiohttp.ClientSession() as session:
            new_markets = {}
            
            for symbol in self.config.symbols:
                slug = self.get_slug(symbol)
                url = f"{self.config.gamma_url}/markets/slug/{slug}"
                
                try:
                    async with session.get(url, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            clob_ids = data.get("clobTokenIds") or data.get("clob_token_ids")
                            
                            if isinstance(clob_ids, str):
                                try:
                                    clob_ids = json.loads(clob_ids)
                                except:
                                    clob_ids = [x.strip().strip('"') for x in clob_ids.strip("[]").split(",")]
                            
                            if isinstance(clob_ids, list) and len(clob_ids) >= 2:
                                logger.debug(f"[{symbol}] Found market: {slug}")
                                new_markets[slug] = {
                                    'symbol': symbol,
                                    'slug': slug,
                                    'yes_token': clob_ids[0],
                                    'no_token': clob_ids[1],
                                }
                except Exception as e:
                    logger.debug(f"[{symbol}] fetch error: {e}")
            
            # Check if changed
            old_slugs = set(self.active_markets.keys())
            new_slugs = set(new_markets.keys())
            changed = old_slugs != new_slugs
            
            if changed:
                self.active_markets = new_markets
                self.token_to_market.clear()
                
                for slug, market in new_markets.items():
                    self.token_to_market[market['yes_token']] = slug
                    self.token_to_market[market['no_token']] = slug
            
            return changed
    
    # ==================== WEBSOCKET ====================
    
    async def connect_ws(self):
        """Connect to Polymarket WebSocket"""
        if self.ws_session:
            await self.ws_session.close()
        
        self.ws_session = aiohttp.ClientSession()
        
        try:
            self.ws = await self.ws_session.ws_connect(self.config.ws_url)
            logger.info("WSS connected")
            
            # Subscribe to all tokens
            token_ids = []
            for market in self.active_markets.values():
                token_ids.extend([market['yes_token'], market['no_token']])
            
            if token_ids:
                await self.ws.send_json({"assets_ids": token_ids, "type": "market"})
                logger.info(f"Subscribed to {len(token_ids)} tokens")
                
        except Exception as e:
            logger.error(f"WSS connect error: {e}")
            self.ws = None
    
    async def handle_message(self, data: dict):
        """Handle WSS message"""
        self.stats['wss_messages'] += 1
        
        try:
            event = data.get('event_type')
            
            if event == 'book':
                token_id = data.get('asset_id')
                bids = data.get('bids') or data.get('buys') or []
                asks = data.get('asks') or data.get('sells') or []
                
                if token_id and (bids or asks):
                    self.orderbook.update(token_id, bids, asks)
                    
                    # Log price updates periodically
                    slug = self.token_to_market.get(token_id)
                    if slug and self.stats['wss_messages'] % 100 == 0:
                        market = self.active_markets.get(slug, {})
                        symbol = market.get('symbol', '?')
                        yes_token = market.get('yes_token')
                        no_token = market.get('no_token')
                        yes_p = self.orderbook.get_buy_price(yes_token) if yes_token else 0
                        no_p = self.orderbook.get_buy_price(no_token) if no_token else 0
                        logger.debug(f"[{symbol}] YES=${yes_p:.3f} NO=${no_p:.3f} (sum={yes_p+no_p:.3f})")
                    
                    # Check for trading signals
                    if slug:
                        await self.check_signals(slug)
        except Exception as e:
            logger.debug(f"Message handling error: {e}")
    
    # ==================== TRADING LOGIC ====================
    
    async def check_signals(self, slug: str):
        """Check if we should trade based on current token prices"""
        market = self.active_markets.get(slug)
        if not market:
            return
        
        symbol = market['symbol']
        yes_token = market['yes_token']
        no_token = market['no_token']
        
        # Get current token prices from orderbook
        yes_price = self.orderbook.get_buy_price(yes_token)
        no_price = self.orderbook.get_buy_price(no_token)
        
        # Validate prices
        if yes_price <= 0 or yes_price >= 1 or no_price <= 0 or no_price >= 1:
            return
        
        # Get or create position
        if slug not in self.positions:
            self.positions[slug] = Position(symbol=symbol, market_id=slug)
        
        pos = self.positions[slug]
        
        # Check cooldown
        now = datetime.now(timezone.utc)
        seconds_since_last = (now - pos.last_trade_time).total_seconds()
        if seconds_since_last < self.config.signal_cooldown:
            logger.debug(f"[{symbol}] Cooldown: {seconds_since_last:.0f}s < {self.config.signal_cooldown}s")
            return
        
        # === INITIAL TRADE (no position yet) ===
        if len(pos.trades) == 0:
            # Check max position only for initial trades
            if pos.total_cost >= self.config.max_position:
                logger.debug(f"[{symbol}] Max position reached: ${pos.total_cost:.2f}")
                return
                
            # Get momentum from Binance for initial direction
            momentum, _ = await self.binance.get_momentum(symbol)
            
            logger.debug(f"[{symbol}] Momentum check: {momentum:.4f}%")
            
            if abs(momentum) < 0.05:  # Need at least 0.05% move
                return
            
            if momentum > 0:
                # Bullish - buy YES
                logger.info(f"[{symbol}] 📈 SIGNAL: BUY YES (momentum +{momentum:.3f}%)")
                await self.place_trade(pos, "YES", yes_price, f"Initial: momentum +{momentum:.3f}%")
            else:
                # Bearish - buy NO  
                logger.info(f"[{symbol}] 📉 SIGNAL: BUY NO (momentum {momentum:.3f}%)")
                await self.place_trade(pos, "NO", no_price, f"Initial: momentum {momentum:.3f}%")
            return
        
        # === TRAILING / HEDGE LOGIC ===
        if pos.hedge_count >= self.config.max_hedges:
            logger.debug(f"[{symbol}] Max hedges reached: {pos.hedge_count}/{self.config.max_hedges}")
            return  # Max hedges reached
        
        now = datetime.now(timezone.utc)
        
        # Check if token price moved against us
        if pos.initial_side == "YES" and pos.yes_entry_price > 0:
            # We bet YES, check if YES token price dropped
            price_drop = (pos.yes_entry_price - yes_price) / pos.yes_entry_price
            
            if price_drop >= self.config.trail_trigger:
                # Drop detected - check if it persists
                if pos.hedge_trigger_time is None:
                    # First time seeing drop - start timer
                    pos.hedge_trigger_time = now
                    pos.hedge_trigger_drop = price_drop
                    logger.info(f"[{symbol}] ⏱️ HEDGE PENDING: YES dropped {price_drop:.0%}, waiting {self.config.hedge_confirm_seconds}s...")
                else:
                    # Check if enough time passed
                    elapsed = (now - pos.hedge_trigger_time).total_seconds()
                    if elapsed >= self.config.hedge_confirm_seconds:
                        # Confirmed! Execute hedge
                        logger.info(f"[{symbol}] 🛡️ HEDGE CONFIRMED: YES dropped {price_drop:.0%} for {elapsed:.0f}s")
                        await self.place_trade(
                            pos, "NO", no_price, 
                            f"HEDGE: YES ${pos.yes_entry_price:.2f}→${yes_price:.2f} (-{price_drop:.0%})",
                            is_hedge=True
                        )
                        # Reset trigger after hedge
                        pos.hedge_trigger_time = None
                        pos.hedge_trigger_drop = 0
                    else:
                        logger.debug(f"[{symbol}] YES drop={price_drop:.0%}, waiting... ({elapsed:.0f}s/{self.config.hedge_confirm_seconds}s)")
            else:
                # Drop recovered - reset timer
                if pos.hedge_trigger_time is not None:
                    logger.info(f"[{symbol}] ✅ DROP RECOVERED: YES back to ${yes_price:.3f} (was -{pos.hedge_trigger_drop:.0%})")
                    pos.hedge_trigger_time = None
                    pos.hedge_trigger_drop = 0
        
        elif pos.initial_side == "NO" and pos.no_entry_price > 0:
            # We bet NO, check if NO token price dropped
            price_drop = (pos.no_entry_price - no_price) / pos.no_entry_price
            
            if price_drop >= self.config.trail_trigger:
                # Drop detected - check if it persists
                if pos.hedge_trigger_time is None:
                    # First time seeing drop - start timer
                    pos.hedge_trigger_time = now
                    pos.hedge_trigger_drop = price_drop
                    logger.info(f"[{symbol}] ⏱️ HEDGE PENDING: NO dropped {price_drop:.0%}, waiting {self.config.hedge_confirm_seconds}s...")
                else:
                    # Check if enough time passed
                    elapsed = (now - pos.hedge_trigger_time).total_seconds()
                    if elapsed >= self.config.hedge_confirm_seconds:
                        # Confirmed! Execute hedge
                        logger.info(f"[{symbol}] 🛡️ HEDGE CONFIRMED: NO dropped {price_drop:.0%} for {elapsed:.0f}s")
                        await self.place_trade(
                            pos, "YES", yes_price,
                            f"HEDGE: NO ${pos.no_entry_price:.2f}→${no_price:.2f} (-{price_drop:.0%})",
                            is_hedge=True
                        )
                        # Reset trigger after hedge
                        pos.hedge_trigger_time = None
                        pos.hedge_trigger_drop = 0
                    else:
                        logger.debug(f"[{symbol}] NO drop={price_drop:.0%}, waiting... ({elapsed:.0f}s/{self.config.hedge_confirm_seconds}s)")
            else:
                # Drop recovered - reset timer
                if pos.hedge_trigger_time is not None:
                    logger.info(f"[{symbol}] ✅ DROP RECOVERED: NO back to ${no_price:.3f} (was -{pos.hedge_trigger_drop:.0%})")
                    pos.hedge_trigger_time = None
                    pos.hedge_trigger_drop = 0
    
    async def place_trade(self, pos: Position, side: str, price: float, reason: str, is_hedge: bool = False):
        """Place a trade"""
        MIN_SHARES = 5  # Polymarket minimum
        
        if price <= 0 or price >= 1:
            return
        
        # Calculate size
        if is_hedge:
            bet_amount = self.config.bet_size * self.config.trail_size
            # Hedge is always allowed (up to max_hedges limit checked earlier)
            remaining = bet_amount  # No max_position limit for hedges
        else:
            bet_amount = self.config.bet_size
            remaining = self.config.max_position - pos.total_cost
            bet_amount = min(bet_amount, remaining)
        
        # Calculate shares
        shares = int(bet_amount / price)
        
        # Enforce minimum 5 shares
        if shares < MIN_SHARES:
            # Try to buy exactly 5 shares if we can afford it
            min_cost = MIN_SHARES * price
            if min_cost <= remaining:
                shares = MIN_SHARES
            else:
                logger.debug(f"[{pos.symbol}] Cannot afford min {MIN_SHARES} shares @ ${price:.3f} (need ${min_cost:.2f}, have ${remaining:.2f})")
                return
        
        cost = shares * price
        
        # Double check minimum
        if shares < MIN_SHARES:
            return
        
        # Get token_id for the side we want to buy
        market = self.active_markets.get(pos.market_id, {})
        token_id = market.get('yes_token') if side == "YES" else market.get('no_token')
        
        if not token_id:
            logger.error(f"[{pos.symbol}] No token_id for {side}")
            return
        
        # Execute
        emoji = "🛡️" if is_hedge else "🎲"
        success = False
        order_id = None
        attempts = 1
        
        if self.config.simulation_mode:
            logger.info(f"[{pos.symbol}] [SIM] {emoji} BUY {side} @ ${price:.3f} x {shares} = ${cost:.2f}")
            logger.info(f"[{pos.symbol}] [SIM] Reason: {reason}")
            success = True
        else:
            # Real order via CLOB
            if not self.clob or not self.executor:
                logger.error(f"[{pos.symbol}] CLOB client not available!")
                return
            
            try:
                self.stats['orders_sent'] += 1
                logger.info(f"[{pos.symbol}] {emoji} Sending order: BUY {side} @ ${price:.3f} x {shares}")
                
                # Execute order in thread pool (sync call)
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    self.executor,
                    self._send_order_sync,
                    token_id, price, shares
                )
                
                if result and 'error' not in result:
                    status = result.get('status', result.get('orderStatus', 'UNKNOWN')).upper()
                    order_id = result.get('orderID', result.get('id', 'unknown'))
                    attempts = result.get('attempts', 1)
                    success_flag = result.get('success', False)
                    
                    # Success if status=MATCHED/FILLED OR success=True
                    if status in ['MATCHED', 'FILLED'] or success_flag:
                        success = True
                        self.stats['orders_filled'] += 1
                        
                        actual_price = result.get('fill_price', price)
                        actual_shares = result.get('fill_shares', shares)
                        actual_cost = result.get('fill_cost', shares * actual_price)
                        
                        logger.info(f"[{pos.symbol}] ✓ {side} @ ${actual_price:.3f} x {actual_shares:.0f} = ${actual_cost:.2f} | {status}")
                        
                        price = actual_price
                        shares = actual_shares
                        cost = actual_cost
                    else:
                        self.stats['orders_failed'] += 1
                        logger.warning(f"[{pos.symbol}] Order not filled: {status}")
                else:
                    self.stats['orders_failed'] += 1
                    error = result.get('error', 'Unknown') if result else 'No response'
                    logger.error(f"[{pos.symbol}] Order failed: {error}")
                    logger.debug(f"[{pos.symbol}] Full failed result: {result}")
                    
            except Exception as e:
                self.stats['orders_failed'] += 1
                logger.error(f"[{pos.symbol}] Order execution error: {e}")
        
        if success:
            pos.add_trade(side, shares, price, cost, reason)
            
            if not pos.initial_side:
                pos.initial_side = side
            
            if is_hedge:
                pos.hedge_count += 1
                self.stats['hedges'] += 1
            
            self.stats['trades'] += 1
            
            # Log position status
            logger.info(f"[{pos.symbol}] Position: YES={pos.yes_shares:.0f}@${pos.avg_yes_price:.3f} "
                       f"NO={pos.no_shares:.0f}@${pos.avg_no_price:.3f} | Total: ${pos.total_cost:.2f}")
            
            # Save to database
            self.db.save_trade(
                slot=self.current_slot,
                symbol=pos.symbol,
                side=side,
                shares=shares,
                price=price,
                cost=cost,
                is_hedge=is_hedge,
                reason=reason,
                order_id=order_id,
                attempts=attempts
            )
            
            # Discord
            await self.discord.send_trade(pos.symbol, side, shares, price, reason, is_hedge)
    
    def _send_order_sync(self, token_id: str, price: float, shares: int) -> dict:
        """Synchronous order execution - GTC with high limit for best fill"""
        shares = int(shares)
        
        if shares < 5:
            return {'error': 'Shares below minimum (5)'}
        
        # Use limit price of $0.99 - will fill at best available price
        limit_price = 0.99
        
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=float(shares),
                side=BUY,
            )
            
            signed_order = self.clob.create_order(order_args)
            resp = self.clob.post_order(signed_order, OrderType.GTC)
            
            logger.debug(f"GTC order @ ${limit_price:.2f} x {shares} -> {resp}")
            
            if resp:
                status = resp.get('status', resp.get('orderStatus', '')).upper()
                success_flag = resp.get('success', False)
                
                if status in ['MATCHED', 'FILLED'] or success_flag:
                    # Log full response for debugging
                    logger.debug(f"GTC full response: {resp}")
                    
                    # For BUY orders:
                    # takingAmount = tokens received (what you take from market)
                    # makingAmount = USDC paid (what you give/make)
                    tokens_received = float(resp.get('takingAmount', 0) or 0)
                    usdc_paid = float(resp.get('makingAmount', 0) or 0)
                    
                    logger.debug(f"tokens={tokens_received}, usdc={usdc_paid}")
                    
                    if tokens_received > 0 and usdc_paid > 0:
                        actual_price = round(usdc_paid / tokens_received, 3)
                        actual_shares = tokens_received
                        actual_cost = usdc_paid
                    else:
                        actual_price = self.orderbook.get_buy_price(token_id)
                        actual_shares = shares
                        actual_cost = shares * actual_price
                    
                    resp['fill_price'] = actual_price
                    resp['fill_shares'] = actual_shares
                    resp['fill_cost'] = actual_cost
                    resp['attempts'] = 1
                    resp['order_type'] = 'GTC'
                    logger.info(f"GTC filled @ ${actual_price:.3f} x {actual_shares:.1f} (${actual_cost:.2f})")
                    return resp
                else:
                    logger.warning(f"GTC order status: {status}")
                    return {'error': f'GTC order not filled: {status}'}
            
            return {'error': 'No response from GTC order'}
                
        except Exception as e:
            logger.warning(f"GTC order error: {e}")
            return {'error': str(e)}
    
    # ==================== SETTLEMENT ====================
    
    async def settle_slot(self, old_positions: dict, old_start_prices: dict):
        """Settle previous slot"""
        logger.info(f"[SETTLE] Starting settlement with {len(old_positions)} positions")
        
        if not old_positions:
            logger.info("[SETTLE] No positions to settle")
            return
        
        # Check if any positions have trades
        active_positions = {k: v for k, v in old_positions.items() if v.total_cost > 0}
        if not active_positions:
            logger.info("[SETTLE] No active positions (all have 0 cost)")
            return
        
        logger.info(f"[SETTLE] Active positions: {len(active_positions)}")
        
        # Wait for prices to stabilize
        await asyncio.sleep(5)
        
        logger.info("")
        logger.info("=" * 60)
        logger.info("📊 SETTLEMENT")
        logger.info("=" * 60)
        
        results = {}
        total_cost = 0
        total_payout = 0
        total_pnl = 0
        
        for slug, pos in old_positions.items():
            if pos.total_cost == 0:
                continue
            
            symbol = pos.symbol
            start_price = old_start_prices.get(symbol, 0)
            end_price = await self.binance.get_price(symbol)
            
            if not start_price or not end_price:
                logger.warning(f"[{symbol}] Could not determine winner")
                continue
            
            # Determine winner based on Binance price
            if end_price >= start_price:
                winner = "YES"
                change = ((end_price - start_price) / start_price) * 100
                logger.info(f"[{symbol}] 📈 UP: ${start_price:.2f} → ${end_price:.2f} ({change:+.3f}%)")
            else:
                winner = "NO"
                change = ((end_price - start_price) / start_price) * 100
                logger.info(f"[{symbol}] 📉 DOWN: ${start_price:.2f} → ${end_price:.2f} ({change:+.3f}%)")
            
            # Calculate P&L
            if winner == "YES":
                payout = pos.yes_shares * 1.0
                profit = payout - pos.yes_cost
                loss = pos.no_cost
            else:
                payout = pos.no_shares * 1.0
                profit = payout - pos.no_cost
                loss = pos.yes_cost
            
            pnl = profit - loss
            
            results[slug] = {'winner': winner, 'pnl': pnl, 'payout': payout}
            total_cost += pos.total_cost
            total_payout += payout
            total_pnl += pnl
            
            # Log details
            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            logger.info(f"[{symbol}] {emoji} Winner: {winner} | Initial bet: {pos.initial_side}")
            logger.info(f"   YES: {pos.yes_shares:.0f} @ ${pos.avg_yes_price:.3f} = ${pos.yes_cost:.2f}")
            logger.info(f"   NO:  {pos.no_shares:.0f} @ ${pos.avg_no_price:.3f} = ${pos.no_cost:.2f}")
            logger.info(f"   Trades: {len(pos.trades)} | Hedges: {pos.hedge_count}")
            
            if winner == "YES":
                logger.info(f"   ✅ YES: {pos.yes_shares:.0f} × $1 - ${pos.yes_cost:.2f} = ${profit:+.2f}")
                logger.info(f"   ❌ NO: -${pos.no_cost:.2f}")
            else:
                logger.info(f"   ✅ NO: {pos.no_shares:.0f} × $1 - ${pos.no_cost:.2f} = ${profit:+.2f}")
                logger.info(f"   ❌ YES: -${pos.yes_cost:.2f}")
            
            logger.info(f"   → NET P&L: ${pnl:+.2f}")
            
            # Save to database
            self.db.save_settlement(
                slot=slug,
                symbol=symbol,
                winner=winner,
                yes_shares=pos.yes_shares,
                yes_cost=pos.yes_cost,
                no_shares=pos.no_shares,
                no_cost=pos.no_cost,
                payout=payout,
                pnl=pnl,
                hedge_count=pos.hedge_count
            )
        
        # Summary
        logger.info("")
        logger.info("=" * 60)
        emoji = "🎉" if total_pnl > 0 else "📉"
        logger.info(f"{emoji} SLOT SUMMARY")
        logger.info(f"   Cost:   ${total_cost:.2f}")
        logger.info(f"   Payout: ${total_payout:.2f}")
        logger.info(f"   P&L:    ${total_pnl:+.2f}")
        if total_cost > 0:
            logger.info(f"   ROI:    {(total_pnl/total_cost)*100:+.1f}%")
        logger.info("=" * 60)
        
        self.stats['total_pnl'] += total_pnl
        self.stats['slots'] += 1
        
        # Discord
        slot_name = list(old_positions.keys())[0] if old_positions else "unknown"
        await self.discord.send_settlement(slot_name, old_positions, total_pnl, results)
    
    # ==================== MAIN LOOP ====================
    
    async def start(self):
        logger.info("=" * 60)
        logger.info(f"POLYNEKO v2 [{'🔬 SIM' if self.config.simulation_mode else '💰 PROD'}]")
        logger.info("=" * 60)
        logger.info("  Strategy: MOMENTUM + TRAILING HEDGE")
        self.config.print_config()
        logger.info("")
        logger.info("  📊 Monitors Polymarket token prices via WebSocket")
        logger.info("  🛡️ Hedges when token price drops against position")
        
        # Check CLOB availability for production mode
        if not self.config.simulation_mode:
            if not CLOB_AVAILABLE:
                logger.error("❌ py_clob_client not installed! Run: pip install py-clob-client")
                logger.error("   Falling back to simulation mode")
                self.config.simulation_mode = True
            elif not self.clob:
                logger.error("❌ CLOB client failed to initialize!")
                logger.error("   Check PRIVATE_KEY in .env")
                logger.error("   Falling back to simulation mode")
                self.config.simulation_mode = True
            else:
                logger.info("  ✅ CLOB client ready for real orders")
        
        logger.info("=" * 60)
        
        await self.binance.start()
        await self.discord.start()
        
        # Start dashboard
        if self.dashboard:
            await self.dashboard.start()
        
        self.running = True
        
        try:
            await self._main_loop()
        finally:
            await self.stop()
    
    async def stop(self):
        self.running = False
        if self.ws:
            await self.ws.close()
        if self.ws_session:
            await self.ws_session.close()
        if self.executor:
            self.executor.shutdown(wait=False)
        if self.dashboard:
            await self.dashboard.stop()
        self.db.close()
        await self.binance.stop()
        await self.discord.stop()
    
    async def _main_loop(self):
        """Main loop"""
        last_slot = None
        last_status = datetime.now()
        
        while self.running:
            try:
                # Check for new slot
                markets_changed = await self.fetch_markets()
                current_slot = list(self.active_markets.keys())[0] if self.active_markets else None
                
                if markets_changed and current_slot != last_slot:
                    if last_slot is not None:
                        # Settle old slot - deep copy positions!
                        old_positions = copy.deepcopy(self.positions)
                        old_prices = dict(self.start_prices)
                        asyncio.create_task(self.settle_slot(old_positions, old_prices))
                    
                    # New slot
                    logger.info(f"🔄 New slot: {current_slot}")
                    self.positions = {}
                    self.current_slot = current_slot
                    last_slot = current_slot
                    
                    # Record start prices (Binance) for settlement
                    self.start_prices = {}
                    for symbol in self.config.symbols:
                        price = await self.binance.get_price(symbol)
                        if price:
                            self.start_prices[symbol] = price
                            logger.info(f"[{symbol}] 📍 Start: ${price:.2f}")
                    
                    # Connect WebSocket
                    await self.connect_ws()
                
                # Process WebSocket messages
                if self.ws and not self.ws.closed:
                    try:
                        msg = await asyncio.wait_for(self.ws.receive(), timeout=0.1)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            await self.handle_message(data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("WSS disconnected, reconnecting...")
                            await self.connect_ws()
                    except asyncio.TimeoutError:
                        pass
                    except Exception as e:
                        logger.error(f"WSS error: {e}")
                        await asyncio.sleep(1)
                else:
                    await asyncio.sleep(1)
                
                # Status every 60s
                if (datetime.now() - last_status).total_seconds() >= 60:
                    self._print_status()
                    last_status = datetime.now()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                await asyncio.sleep(5)
    
    def _print_status(self):
        logger.info("")
        logger.info("=" * 60)
        mode = "SIM" if self.config.simulation_mode else "PROD"
        logger.info(f"PREDICTION v2 [{mode}] | Trades: {self.stats['trades']} | Hedges: {self.stats['hedges']}")
        logger.info(f"WSS msgs: {self.stats['wss_messages']} | Slots: {self.stats['slots']}")
        
        if not self.config.simulation_mode:
            logger.info(f"Orders: {self.stats['orders_sent']} sent, {self.stats['orders_filled']} filled, {self.stats['orders_failed']} failed")
        
        logger.info(f"Total P&L: ${self.stats['total_pnl']:+.2f}")
        
        for slug, pos in self.positions.items():
            if pos.total_cost > 0:
                logger.info(f"  [{pos.symbol}] {pos.initial_side} | YES={pos.yes_shares:.0f}@${pos.avg_yes_price:.3f} "
                           f"NO={pos.no_shares:.0f}@${pos.avg_no_price:.3f} | ${pos.total_cost:.2f} | H:{pos.hedge_count}")
        logger.info("=" * 60)

# ========================== MAIN ==========================

def main():
    config = Config.from_env()
    bot = PolyNeko(config)
    
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Shutting down...")

if __name__ == "__main__":
    main()
