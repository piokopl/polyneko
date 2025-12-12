#!/usr/bin/env python3
"""
POLYNEKO v3 - Advanced Directional Trading with Smart Hedging

Strategy:
- Monitor Polymarket token prices (YES/NO) via WebSocket
- Make initial prediction based on Binance momentum + RSI + volume
- Entry filters: spread check, volume check, RSI overbought/oversold, hour performance
- HEDGE when ALL conditions are met:
  1. Token price dropped X% from peak (dynamic trigger based on time left)
  2. Binance price crossed start price (losing position)
  3. Enough time left in slot (min_minutes_for_hedge)
  4. Volume confirmation (optional)
- Progressive hedge triggers: [10%, 20%, 30%] for hedge #1, #2, #3
- Scaled hedge size based on drop magnitude

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
    
    # Trailing/Hedge params
    hedge_triggers: list = field(default_factory=lambda: [0.10, 0.20, 0.30])  # Multiple hedge levels
    trail_size: float = 0.5
    max_hedges: int = 3
    hedge_confirm_seconds: int = 5  # Drop must persist for X seconds before hedge
    hedge_scale_with_drop: bool = True  # Scale hedge size with drop magnitude
    min_minutes_for_hedge: int = 2  # Don't hedge if less than X minutes left
    
    # Dynamic trail trigger (based on time left in slot)
    trail_trigger_early: float = 0.10   # >7 min left
    trail_trigger_mid: float = 0.08     # 3-7 min left  
    trail_trigger_late: float = 0.05    # <3 min left
    
    # Add to winner params
    add_winner_enabled: bool = True
    add_winner_min_minutes: float = 2.0    # Not earlier than X min before end
    add_winner_max_minutes: float = 5.0    # Not later than X min before end  
    add_winner_min_distance: float = 0.003 # Min 0.3% Binance distance from start
    add_winner_min_token_price: float = 0.60  # Token must be "safe" (>60%)
    add_winner_size: float = 0.5           # 50% of bet_size
    add_winner_max_adds: int = 2           # Max additions per position
    add_winner_confirm_seconds: int = 3    # Stability check duration
    
    # Order execution
    order_max_retries: int = 15          # FOK retries (more is safer)
    order_price_increment: float = 0.01  # Price increment per retry
    order_use_gtc_fallback: bool = False # GTC is risky - might not fill
    
    # Signal params
    signal_cooldown: int = 30
    min_momentum: float = 0.05  # Minimum momentum % for entry
    
    # Entry filters
    rsi_period: int = 14
    rsi_overbought: float = 70.0   # Don't buy YES if RSI > this
    rsi_oversold: float = 30.0     # Don't buy NO if RSI < this
    max_spread: float = 0.10       # Max spread (ask-bid) to trade
    min_volume_ratio: float = 0.5  # Min volume vs average to trade
    require_volume_for_hedge: bool = True  # Require volume spike for hedge
    
    # === NEW v4 INDICATORS ===
    
    # MACD (12, 26, 9)
    use_macd: bool = True          # Use MACD confirmation
    
    # Stochastic (14, 3)
    use_stochastic: bool = True
    stoch_overbought: float = 80.0
    stoch_oversold: float = 20.0
    
    # ADX - Average Directional Index
    use_adx: bool = True
    adx_min_strength: float = 20.0  # Skip if ADX < this (weak trend)
    adx_strong: float = 25.0        # Strong trend threshold
    
    # Bollinger Bands
    use_bb_squeeze: bool = True
    bb_squeeze_threshold: float = 0.02  # Width < 2% = squeeze
    
    # ATR - dynamic parameters
    use_atr_dynamic: bool = True
    atr_base_multiplier: float = 1.5
    
    # Multi-timeframe analysis
    use_mtf: bool = True  # Check 5m timeframe for trend confirmation
    
    # Divergence detection
    use_divergence: bool = True
    divergence_bonus: float = 0.15  # Confidence bonus for divergence
    
    # Session filter (UTC hours)
    use_session_filter: bool = False  # Disabled by default - crypto 24/7
    active_sessions: list = field(default_factory=lambda: [(8, 11), (13, 16), (19, 22)])
    
    # === CONFIDENCE SCORING ===
    use_confidence_scoring: bool = True
    min_confidence: float = 0.4     # Skip if confidence < 40%
    high_confidence: float = 0.6    # Increase bet if confidence > 60%
    high_confidence_multiplier: float = 1.5
    
    # === POSITION SIZING ===
    use_dynamic_sizing: bool = True
    win_streak_bonus: float = 0.1   # +10% per win (max +50%)
    max_streak_bonus: float = 0.5   # Cap at +50%
    
    # === MARTINGALE PREVENTION ===
    max_consecutive_losses: int = 3
    loss_cooldown_seconds: int = 300  # 5 min cooldown after max losses
    loss_size_reduction: float = 0.5  # Reduce bet to 50% after losses
    
    # Historical analysis
    use_hour_filter: bool = True
    min_hour_winrate: float = 40.0  # Skip hours with win rate below this
    
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
        
        # Parse hedge triggers (comma-separated)
        hedge_triggers_str = os.getenv("HEDGE_TRIGGERS", "0.10,0.20,0.30")
        hedge_triggers = [float(x.strip()) for x in hedge_triggers_str.split(",")]
        
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
            
            # Hedge params
            hedge_triggers=hedge_triggers,
            trail_size=float(os.getenv("TRAIL_SIZE", "0.5")),
            max_hedges=int(os.getenv("MAX_HEDGES", "3")),
            hedge_confirm_seconds=int(os.getenv("HEDGE_CONFIRM_SECONDS", "5")),
            hedge_scale_with_drop=os.getenv("HEDGE_SCALE_WITH_DROP", "true").lower() == "true",
            min_minutes_for_hedge=int(os.getenv("MIN_MINUTES_FOR_HEDGE", "2")),
            
            # Dynamic trail trigger
            trail_trigger_early=float(os.getenv("TRAIL_TRIGGER_EARLY", "0.10")),
            trail_trigger_mid=float(os.getenv("TRAIL_TRIGGER_MID", "0.08")),
            trail_trigger_late=float(os.getenv("TRAIL_TRIGGER_LATE", "0.05")),
            
            # Add to winner
            add_winner_enabled=os.getenv("ADD_WINNER_ENABLED", "true").lower() == "true",
            add_winner_min_minutes=float(os.getenv("ADD_WINNER_MIN_MINUTES", "2.0")),
            add_winner_max_minutes=float(os.getenv("ADD_WINNER_MAX_MINUTES", "5.0")),
            add_winner_min_distance=float(os.getenv("ADD_WINNER_MIN_DISTANCE", "0.003")),
            add_winner_min_token_price=float(os.getenv("ADD_WINNER_MIN_TOKEN_PRICE", "0.60")),
            add_winner_size=float(os.getenv("ADD_WINNER_SIZE", "0.5")),
            add_winner_max_adds=int(os.getenv("ADD_WINNER_MAX_ADDS", "2")),
            add_winner_confirm_seconds=int(os.getenv("ADD_WINNER_CONFIRM_SECONDS", "3")),
            
            # Signal
            signal_cooldown=int(os.getenv("SIGNAL_COOLDOWN", "30")),
            min_momentum=float(os.getenv("MIN_MOMENTUM", "0.05")),
            
            # Entry filters
            rsi_period=int(os.getenv("RSI_PERIOD", "14")),
            rsi_overbought=float(os.getenv("RSI_OVERBOUGHT", "70")),
            rsi_oversold=float(os.getenv("RSI_OVERSOLD", "30")),
            max_spread=float(os.getenv("MAX_SPREAD", "0.10")),
            min_volume_ratio=float(os.getenv("MIN_VOLUME_RATIO", "0.5")),
            require_volume_for_hedge=os.getenv("REQUIRE_VOLUME_FOR_HEDGE", "true").lower() == "true",
            
            # NEW v4 indicators
            use_macd=os.getenv("USE_MACD", "true").lower() == "true",
            use_stochastic=os.getenv("USE_STOCHASTIC", "true").lower() == "true",
            stoch_overbought=float(os.getenv("STOCH_OVERBOUGHT", "80")),
            stoch_oversold=float(os.getenv("STOCH_OVERSOLD", "20")),
            use_adx=os.getenv("USE_ADX", "true").lower() == "true",
            adx_min_strength=float(os.getenv("ADX_MIN_STRENGTH", "20")),
            adx_strong=float(os.getenv("ADX_STRONG", "25")),
            use_bb_squeeze=os.getenv("USE_BB_SQUEEZE", "true").lower() == "true",
            bb_squeeze_threshold=float(os.getenv("BB_SQUEEZE_THRESHOLD", "0.02")),
            use_atr_dynamic=os.getenv("USE_ATR_DYNAMIC", "true").lower() == "true",
            atr_base_multiplier=float(os.getenv("ATR_BASE_MULTIPLIER", "1.5")),
            use_mtf=os.getenv("USE_MTF", "true").lower() == "true",
            use_divergence=os.getenv("USE_DIVERGENCE", "true").lower() == "true",
            divergence_bonus=float(os.getenv("DIVERGENCE_BONUS", "0.15")),
            use_session_filter=os.getenv("USE_SESSION_FILTER", "false").lower() == "true",
            
            # Confidence scoring
            use_confidence_scoring=os.getenv("USE_CONFIDENCE_SCORING", "true").lower() == "true",
            min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.4")),
            high_confidence=float(os.getenv("HIGH_CONFIDENCE", "0.6")),
            high_confidence_multiplier=float(os.getenv("HIGH_CONFIDENCE_MULTIPLIER", "1.5")),
            
            # Dynamic sizing
            use_dynamic_sizing=os.getenv("USE_DYNAMIC_SIZING", "true").lower() == "true",
            win_streak_bonus=float(os.getenv("WIN_STREAK_BONUS", "0.1")),
            max_streak_bonus=float(os.getenv("MAX_STREAK_BONUS", "0.5")),
            
            # Martingale prevention
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")),
            loss_cooldown_seconds=int(os.getenv("LOSS_COOLDOWN_SECONDS", "300")),
            loss_size_reduction=float(os.getenv("LOSS_SIZE_REDUCTION", "0.5")),
            
            # Historical analysis
            use_hour_filter=os.getenv("USE_HOUR_FILTER", "true").lower() == "true",
            min_hour_winrate=float(os.getenv("MIN_HOUR_WINRATE", "40")),
            
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
        logger.info("CONFIGURATION (v4)")
        logger.info("=" * 60)
        logger.info(f"  Symbols: {', '.join(self.symbols)}")
        logger.info(f"  Bet Size: ${self.bet_size}")
        logger.info(f"  Max Position: ${self.max_position}")
        logger.info(f"  Hedge Triggers: {[f'{t:.0%}' for t in self.hedge_triggers]}")
        logger.info(f"  Trail Size: {self.trail_size:.0%} (scale with drop: {self.hedge_scale_with_drop})")
        logger.info(f"  Dynamic Trail: early={self.trail_trigger_early:.0%}, mid={self.trail_trigger_mid:.0%}, late={self.trail_trigger_late:.0%}")
        logger.info(f"  Max Hedges: {self.max_hedges} (min {self.min_minutes_for_hedge}min left)")
        if self.add_winner_enabled:
            logger.info(f"  Add Winner: ✓ {self.add_winner_min_minutes}-{self.add_winner_max_minutes}min, "
                       f"dist>{self.add_winner_min_distance:.1%}, token>{self.add_winner_min_token_price:.0%}, "
                       f"max {self.add_winner_max_adds}x")
        else:
            logger.info(f"  Add Winner: ✗")
        logger.info(f"  Signal: cooldown={self.signal_cooldown}s, min_momentum={self.min_momentum:.2%}")
        logger.info(f"  Filters: RSI={self.rsi_oversold}/{self.rsi_overbought}, spread<{self.max_spread:.0%}, vol>{self.min_volume_ratio:.0%}")
        
        # v4 indicators
        indicators = []
        if self.use_macd: indicators.append("MACD")
        if self.use_stochastic: indicators.append(f"Stoch({self.stoch_oversold}/{self.stoch_overbought})")
        if self.use_adx: indicators.append(f"ADX(>{self.adx_min_strength:.0f})")
        if self.use_bb_squeeze: indicators.append("BB-Squeeze")
        if self.use_atr_dynamic: indicators.append("ATR")
        if self.use_mtf: indicators.append("MTF")
        if self.use_divergence: indicators.append("Divergence")
        logger.info(f"  v4 Indicators: {', '.join(indicators) if indicators else '✗'}")
        
        if self.use_confidence_scoring:
            logger.info(f"  Confidence: min={self.min_confidence:.0%}, high={self.high_confidence:.0%} (+{self.high_confidence_multiplier:.1f}x)")
        
        if self.use_dynamic_sizing:
            logger.info(f"  Dynamic Size: win bonus +{self.win_streak_bonus:.0%}/streak (max +{self.max_streak_bonus:.0%})")
        
        logger.info(f"  Loss Protection: {self.max_consecutive_losses} max losses → {self.loss_cooldown_seconds}s cooldown")
        logger.info(f"  Hour Filter: {'✓' if self.use_hour_filter else '✗'} (min winrate={self.min_hour_winrate:.0f}%)")
        logger.info(f"  Session Filter: {'✓' if self.use_session_filter else '✗'}")
        logger.info(f"  Simulation: {self.simulation_mode}")
        logger.info(f"  Discord: {'✓' if self.discord_webhook else '✗'}")
        logger.info(f"  Dashboard: {'http://localhost:' + str(self.dashboard_port) if self.dashboard_enabled else '✗'}")
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
    yes_entry_price: float = 0.0    # First entry price
    yes_peak_price: float = 0.0     # Highest price since entry (for trailing)
    
    # NO side
    no_shares: float = 0.0
    no_cost: float = 0.0
    no_entry_price: float = 0.0     # First entry price
    no_peak_price: float = 0.0      # Highest price since entry (for trailing)
    
    # Tracking
    trades: list = field(default_factory=list)
    hedge_count: int = 0
    last_trade_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Initial prediction
    initial_side: str = ""          # What we predicted first
    
    # Hedge confirmation tracking
    hedge_trigger_time: datetime = None  # When drop first detected
    hedge_trigger_drop: float = 0.0      # Drop % when first detected
    
    # Add to winner tracking
    add_count: int = 0                    # How many times we added to winner
    add_trigger_time: datetime = None     # When add conditions first detected
    
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
                self.yes_peak_price = price   # Initialize peak
            self.yes_shares += shares
            self.yes_cost += cost
        else:
            if self.no_shares == 0:
                self.no_entry_price = price   # First entry
                self.no_peak_price = price    # Initialize peak
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
    
    def get_spread(self, token_id: str) -> float:
        """Get spread as percentage of mid price"""
        book = self.books.get(token_id, {})
        best_bid = book.get('best_bid', 0)
        best_ask = book.get('best_ask', 1)
        mid = book.get('mid', 0.5)
        
        if mid <= 0:
            return 1.0  # 100% spread if no mid
        
        spread = (best_ask - best_bid) / mid
        return spread

# ========================== BINANCE (for initial momentum & settlement) ==========================

class BinanceClient:
    """Get momentum signal, RSI and volume from Binance"""
    
    SYMBOL_MAP = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT", 
        "SOL": "SOLUSDT",
        "XRP": "XRPUSDT",
    }
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._klines_cache: dict = {}  # Cache klines to avoid excessive API calls
        self._cache_time: dict = {}
        
    async def start(self):
        self.session = aiohttp.ClientSession()
        
    async def stop(self):
        if self.session:
            await self.session.close()
    
    async def _get_klines(self, symbol: str, limit: int = 50) -> list:
        """Get klines with caching (refreshes every 10s)"""
        binance_symbol = self.SYMBOL_MAP.get(symbol)
        if not binance_symbol or not self.session:
            return []
        
        cache_key = f"{symbol}_{limit}"
        now = time.time()
        
        # Use cache if fresh (< 10s)
        if cache_key in self._klines_cache and (now - self._cache_time.get(cache_key, 0)) < 10:
            return self._klines_cache[cache_key]
        
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval=1m&limit={limit}"
            async with self.session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    klines = await resp.json()
                    self._klines_cache[cache_key] = klines
                    self._cache_time[cache_key] = now
                    return klines
        except Exception as e:
            logger.debug(f"[{symbol}] Binance klines error: {e}")
        
        return self._klines_cache.get(cache_key, [])
    
    async def get_momentum(self, symbol: str, window: int = 5) -> tuple[float, float]:
        """Get momentum % change and current price"""
        klines = await self._get_klines(symbol, window + 1)
        
        if len(klines) >= 2:
            old_close = float(klines[0][4])
            new_close = float(klines[-1][4])
            momentum = ((new_close - old_close) / old_close) * 100
            return momentum, new_close
        
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
    
    async def get_rsi(self, symbol: str, period: int = 14) -> float:
        """Calculate RSI (Relative Strength Index)"""
        klines = await self._get_klines(symbol, period + 2)
        
        if len(klines) < period + 1:
            return 50.0  # Neutral if not enough data
        
        # Get closing prices
        closes = [float(k[4]) for k in klines]
        
        # Calculate price changes
        changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        
        # Separate gains and losses
        gains = [c if c > 0 else 0 for c in changes]
        losses = [-c if c < 0 else 0 for c in changes]
        
        # Calculate average gain/loss
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    async def get_volume_ratio(self, symbol: str, window: int = 20) -> float:
        """Get current volume vs average volume ratio"""
        klines = await self._get_klines(symbol, window + 1)
        
        if len(klines) < window:
            return 1.0  # Neutral if not enough data
        
        # Get volumes
        volumes = [float(k[5]) for k in klines]
        
        current_volume = volumes[-1]
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])
        
        if avg_volume == 0:
            return 1.0
        
        return current_volume / avg_volume
    
    async def get_full_analysis(self, symbol: str, rsi_period: int = 14) -> dict:
        """Get all indicators at once (efficient - uses cached klines)"""
        # Get more klines for all indicators (need ~50 for ADX, BB, etc.)
        klines = await self._get_klines(symbol, 60)
        
        result = {
            'price': 0.0,
            'momentum': 0.0,
            'rsi': 50.0,
            'volume_ratio': 1.0,
            'macd': 0.0,
            'macd_signal': 0.0,
            'macd_histogram': 0.0,
            'macd_bullish': False,
            'stoch_k': 50.0,
            'stoch_d': 50.0,
            'adx': 0.0,
            'plus_di': 0.0,
            'minus_di': 0.0,
            'atr': 0.0,
            'atr_percent': 0.0,
            'bb_upper': 0.0,
            'bb_middle': 0.0,
            'bb_lower': 0.0,
            'bb_width': 0.0,
            'bb_squeeze': False,
            'divergence': None,
        }
        
        if len(klines) < 2:
            return result
        
        # Parse OHLCV data
        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        
        # Current price
        result['price'] = closes[-1]
        
        # Momentum (5 candles)
        if len(closes) >= 6:
            result['momentum'] = ((closes[-1] - closes[-6]) / closes[-6]) * 100
        
        # RSI
        if len(closes) >= rsi_period + 1:
            changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains = [c if c > 0 else 0 for c in changes]
            losses = [-c if c < 0 else 0 for c in changes]
            avg_gain = sum(gains[-rsi_period:]) / rsi_period
            avg_loss = sum(losses[-rsi_period:]) / rsi_period
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                result['rsi'] = 100 - (100 / (1 + rs))
            else:
                result['rsi'] = 100.0
        
        # Volume ratio
        if len(volumes) >= 2:
            current_vol = volumes[-1]
            avg_vol = sum(volumes[:-1]) / len(volumes[:-1])
            if avg_vol > 0:
                result['volume_ratio'] = current_vol / avg_vol
        
        # === MACD (12, 26, 9) ===
        if len(closes) >= 26:
            ema12 = self._ema(closes, 12)
            ema26 = self._ema(closes, 26)
            macd_line = [ema12[i] - ema26[i] for i in range(len(ema26))]
            signal_line = self._ema(macd_line, 9)
            
            if signal_line:
                result['macd'] = macd_line[-1]
                result['macd_signal'] = signal_line[-1]
                result['macd_histogram'] = macd_line[-1] - signal_line[-1]
                # MACD bullish = histogram positive or crossing up
                result['macd_bullish'] = result['macd_histogram'] > 0
        
        # === Stochastic (14, 3) ===
        if len(closes) >= 14:
            stoch_k_values = []
            for i in range(14, len(closes) + 1):
                period_highs = highs[i-14:i]
                period_lows = lows[i-14:i]
                highest = max(period_highs)
                lowest = min(period_lows)
                if highest != lowest:
                    k = ((closes[i-1] - lowest) / (highest - lowest)) * 100
                else:
                    k = 50.0
                stoch_k_values.append(k)
            
            if len(stoch_k_values) >= 3:
                result['stoch_k'] = stoch_k_values[-1]
                result['stoch_d'] = sum(stoch_k_values[-3:]) / 3  # 3-period SMA of %K
        
        # === ADX (14) ===
        if len(closes) >= 28:  # Need 2x period for proper ADX
            tr_list = []
            plus_dm_list = []
            minus_dm_list = []
            
            for i in range(1, len(closes)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
                tr_list.append(tr)
                
                up_move = highs[i] - highs[i-1]
                down_move = lows[i-1] - lows[i]
                
                plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
                minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
                
                plus_dm_list.append(plus_dm)
                minus_dm_list.append(minus_dm)
            
            if len(tr_list) >= 14:
                # Smoothed averages (Wilder's smoothing)
                atr14 = sum(tr_list[-14:]) / 14
                plus_dm14 = sum(plus_dm_list[-14:]) / 14
                minus_dm14 = sum(minus_dm_list[-14:]) / 14
                
                # +DI and -DI
                plus_di = (plus_dm14 / atr14) * 100 if atr14 > 0 else 0
                minus_di = (minus_dm14 / atr14) * 100 if atr14 > 0 else 0
                
                result['plus_di'] = plus_di
                result['minus_di'] = minus_di
                result['atr'] = atr14
                result['atr_percent'] = (atr14 / closes[-1]) * 100 if closes[-1] > 0 else 0
                
                # DX and ADX
                di_sum = plus_di + minus_di
                if di_sum > 0:
                    dx = abs(plus_di - minus_di) / di_sum * 100
                    # For proper ADX we'd need historical DX values, simplified here
                    result['adx'] = dx
        
        # === Bollinger Bands (20, 2) ===
        if len(closes) >= 20:
            sma20 = sum(closes[-20:]) / 20
            variance = sum((c - sma20) ** 2 for c in closes[-20:]) / 20
            std_dev = variance ** 0.5
            
            result['bb_middle'] = sma20
            result['bb_upper'] = sma20 + (2 * std_dev)
            result['bb_lower'] = sma20 - (2 * std_dev)
            result['bb_width'] = (result['bb_upper'] - result['bb_lower']) / sma20 if sma20 > 0 else 0
            
            # Squeeze detection: BB width < 2% of price
            result['bb_squeeze'] = result['bb_width'] < 0.02
        
        # === Divergence Detection ===
        if len(closes) >= 10 and 'rsi' in result:
            # Compare last 5 candles with previous 5
            recent_price_trend = closes[-1] - closes[-5]
            
            # Calculate RSI for position -5
            if len(closes) >= rsi_period + 6:
                old_closes = closes[:-5]
                old_changes = [old_closes[i] - old_closes[i-1] for i in range(1, len(old_closes))]
                old_gains = [c if c > 0 else 0 for c in old_changes]
                old_losses = [-c if c < 0 else 0 for c in old_changes]
                old_avg_gain = sum(old_gains[-rsi_period:]) / rsi_period
                old_avg_loss = sum(old_losses[-rsi_period:]) / rsi_period
                if old_avg_loss > 0:
                    old_rs = old_avg_gain / old_avg_loss
                    old_rsi = 100 - (100 / (1 + old_rs))
                else:
                    old_rsi = 100.0
                
                rsi_trend = result['rsi'] - old_rsi
                
                # Bullish divergence: price down, RSI up
                if recent_price_trend < 0 and rsi_trend > 5:
                    result['divergence'] = 'BULLISH'
                # Bearish divergence: price up, RSI down
                elif recent_price_trend > 0 and rsi_trend < -5:
                    result['divergence'] = 'BEARISH'
        
        return result
    
    def _ema(self, data: list, period: int) -> list:
        """Calculate EMA"""
        if len(data) < period:
            return []
        
        multiplier = 2 / (period + 1)
        ema = [sum(data[:period]) / period]  # Start with SMA
        
        for price in data[period:]:
            ema.append((price - ema[-1]) * multiplier + ema[-1])
        
        return ema
    
    async def get_higher_timeframe_trend(self, symbol: str) -> str:
        """Get trend from 5-minute timeframe"""
        binance_symbol = self.SYMBOL_MAP.get(symbol)
        if not binance_symbol or not self.session:
            return "NEUTRAL"
        
        cache_key = f"{symbol}_5m"
        now = time.time()
        
        # Cache for 30s (5m data doesn't change as fast)
        if cache_key in self._klines_cache and (now - self._cache_time.get(cache_key, 0)) < 30:
            klines = self._klines_cache[cache_key]
        else:
            try:
                url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval=5m&limit=20"
                async with self.session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        klines = await resp.json()
                        self._klines_cache[cache_key] = klines
                        self._cache_time[cache_key] = now
                    else:
                        return "NEUTRAL"
            except:
                return "NEUTRAL"
        
        if len(klines) < 20:
            return "NEUTRAL"
        
        closes = [float(k[4]) for k in klines]
        
        # SMA 8 vs SMA 20
        sma8 = sum(closes[-8:]) / 8
        sma20 = sum(closes) / 20
        
        # Also check momentum
        momentum = ((closes[-1] - closes[-5]) / closes[-5]) * 100 if closes[-5] > 0 else 0
        
        if sma8 > sma20 and momentum > 0:
            return "BULLISH"
        elif sma8 < sma20 and momentum < 0:
            return "BEARISH"
        return "NEUTRAL"

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
            'adds': 0,
            'wss_messages': 0,
            'slots': 0,
            'total_pnl': 0.0,
            'orders_sent': 0,
            'orders_filled': 0,
            'orders_failed': 0,
            'start_time': time.time(),
        }
        
        # v4: Win/Loss tracking for dynamic sizing
        self.win_streak: int = 0
        self.consecutive_losses: int = 0
        self.last_loss_time: float = 0.0
        self.session_wins: int = 0
        self.session_losses: int = 0
        
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
    
    # ==================== HELPERS ====================
    
    def get_minutes_to_slot_end(self) -> float:
        """Get minutes remaining in current 15-minute slot"""
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        minutes_into_slot = now.minute % 15
        seconds_into_slot = minutes_into_slot * 60 + now.second
        seconds_remaining = (15 * 60) - seconds_into_slot
        return seconds_remaining / 60
    
    def get_dynamic_trail_trigger(self) -> float:
        """Get trail trigger based on time left in slot"""
        minutes_left = self.get_minutes_to_slot_end()
        
        if minutes_left < 3:
            return self.config.trail_trigger_late
        elif minutes_left < 7:
            return self.config.trail_trigger_mid
        else:
            return self.config.trail_trigger_early
    
    def get_hedge_trigger_for_count(self, hedge_count: int) -> float:
        """Get the trigger threshold for given hedge number"""
        triggers = self.config.hedge_triggers
        if hedge_count < len(triggers):
            return triggers[hedge_count]
        return triggers[-1]  # Use last trigger if exceeded
    
    def calculate_hedge_size(self, base_size: float, price_drop: float) -> float:
        """Calculate hedge size, optionally scaled by drop magnitude"""
        if self.config.hedge_scale_with_drop:
            # Scale: 10% drop = 100% of base, 20% drop = 150%, 30% drop = 200%
            scale = 1.0 + (price_drop * 5)  # 5x multiplier on drop
            return base_size * min(scale, 3.0)  # Cap at 3x
        return base_size
    
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
    
    # ==================== v4 HELPER METHODS ====================
    
    def calculate_confidence(self, analysis: dict, direction: str) -> tuple[float, list]:
        """
        Calculate confidence score (0-1) based on all indicators.
        Returns (score, reasons) tuple.
        """
        score = 0.0
        max_score = 0.0
        reasons = []
        
        # 1. Momentum alignment (+1)
        max_score += 1
        momentum = analysis.get('momentum', 0)
        if (direction == 'UP' and momentum > 0) or (direction == 'DOWN' and momentum < 0):
            score += 1
            reasons.append(f"mom={momentum:+.2f}%")
        
        # 2. RSI alignment (+1)
        max_score += 1
        rsi = analysis.get('rsi', 50)
        if direction == 'UP' and rsi < 50:
            score += 1
            reasons.append(f"RSI={rsi:.0f}<50")
        elif direction == 'DOWN' and rsi > 50:
            score += 1
            reasons.append(f"RSI={rsi:.0f}>50")
        
        # 3. MACD alignment (+1)
        if self.config.use_macd:
            max_score += 1
            macd_bullish = analysis.get('macd_bullish', False)
            if (direction == 'UP' and macd_bullish) or (direction == 'DOWN' and not macd_bullish):
                score += 1
                reasons.append("MACD✓")
        
        # 4. Stochastic alignment (+1)
        if self.config.use_stochastic:
            max_score += 1
            stoch_k = analysis.get('stoch_k', 50)
            if direction == 'UP' and stoch_k < self.config.stoch_overbought:
                score += 1
                reasons.append(f"Stoch={stoch_k:.0f}")
            elif direction == 'DOWN' and stoch_k > self.config.stoch_oversold:
                score += 1
                reasons.append(f"Stoch={stoch_k:.0f}")
        
        # 5. ADX strength (+1 or +2)
        if self.config.use_adx:
            max_score += 2
            adx = analysis.get('adx', 0)
            if adx > self.config.adx_strong:
                score += 2
                reasons.append(f"ADX={adx:.0f}🔥")
            elif adx > self.config.adx_min_strength:
                score += 1
                reasons.append(f"ADX={adx:.0f}")
        
        # 6. Volume confirmation (+1)
        max_score += 1
        volume_ratio = analysis.get('volume_ratio', 1)
        if volume_ratio >= 1.2:
            score += 1
            reasons.append(f"vol={volume_ratio:.1f}x")
        
        # 7. Divergence bonus (+1.5)
        if self.config.use_divergence:
            max_score += 1.5
            divergence = analysis.get('divergence')
            if divergence == 'BULLISH' and direction == 'UP':
                score += 1.5
                reasons.append("DIV🔼")
            elif divergence == 'BEARISH' and direction == 'DOWN':
                score += 1.5
                reasons.append("DIV🔽")
        
        # 8. BB Squeeze (potential breakout) (+0.5)
        if self.config.use_bb_squeeze:
            max_score += 0.5
            if analysis.get('bb_squeeze', False):
                score += 0.5
                reasons.append("BB-SQZ")
        
        # Normalize to 0-1
        confidence = score / max_score if max_score > 0 else 0
        
        return confidence, reasons
    
    def is_good_session(self) -> bool:
        """Check if current time is in active trading session"""
        if not self.config.use_session_filter:
            return True
        
        hour = datetime.now(timezone.utc).hour
        for start, end in self.config.active_sessions:
            if start <= hour < end:
                return True
        return False
    
    def check_loss_cooldown(self) -> bool:
        """Check if we're in loss cooldown period. Returns True if OK to trade."""
        if self.consecutive_losses < self.config.max_consecutive_losses:
            return True
        
        elapsed = time.time() - self.last_loss_time
        if elapsed >= self.config.loss_cooldown_seconds:
            # Reset after cooldown
            self.consecutive_losses = 0
            logger.info("🔄 Loss cooldown ended, resuming trading")
            return True
        
        remaining = self.config.loss_cooldown_seconds - elapsed
        logger.debug(f"⏸️ Loss cooldown: {remaining:.0f}s remaining")
        return False
    
    def get_dynamic_bet_size(self, base_size: float, confidence: float) -> float:
        """Calculate dynamic bet size based on confidence and win streak"""
        size = base_size
        
        if not self.config.use_dynamic_sizing:
            return size
        
        # Confidence multiplier
        if self.config.use_confidence_scoring:
            if confidence >= self.config.high_confidence:
                size *= self.config.high_confidence_multiplier
            elif confidence < self.config.min_confidence:
                size *= 0.5  # Reduce for low confidence
        
        # Win streak bonus
        streak_bonus = min(self.win_streak * self.config.win_streak_bonus, 
                          self.config.max_streak_bonus)
        size *= (1 + streak_bonus)
        
        # Loss penalty
        if self.consecutive_losses > 0:
            size *= self.config.loss_size_reduction
        
        return size
    
    def update_win_loss(self, won: bool):
        """Update win/loss tracking after settlement"""
        if won:
            self.win_streak += 1
            self.consecutive_losses = 0
            self.session_wins += 1
        else:
            self.win_streak = 0
            self.consecutive_losses += 1
            self.last_loss_time = time.time()
            self.session_losses += 1
            
            if self.consecutive_losses >= self.config.max_consecutive_losses:
                logger.warning(f"⚠️ {self.consecutive_losses} consecutive losses - entering cooldown for {self.config.loss_cooldown_seconds}s")
    
    # ==================== TRADING LOGIC ====================
    
    async def check_signals(self, slug: str):
        """Check if we should trade based on current token prices and indicators"""
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
        
        # Update peak prices (track highest value since entry)
        if pos.yes_shares > 0 and yes_price > pos.yes_peak_price:
            pos.yes_peak_price = yes_price
            logger.debug(f"[{symbol}] YES new peak: ${yes_price:.3f}")
        if pos.no_shares > 0 and no_price > pos.no_peak_price:
            pos.no_peak_price = no_price
            logger.debug(f"[{symbol}] NO new peak: ${no_price:.3f}")
        
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
            
            # v4: Check loss cooldown
            if not self.check_loss_cooldown():
                return
            
            # v4: Check session filter
            if not self.is_good_session():
                logger.debug(f"[{symbol}] Outside active session")
                return
            
            # === ENTRY FILTERS ===
            
            # 1. Spread check
            yes_spread = self.orderbook.get_spread(yes_token)
            no_spread = self.orderbook.get_spread(no_token)
            if yes_spread > self.config.max_spread or no_spread > self.config.max_spread:
                logger.debug(f"[{symbol}] Spread too wide: YES={yes_spread:.1%}, NO={no_spread:.1%}")
                return
            
            # 2. Get full Binance analysis (all indicators)
            analysis = await self.binance.get_full_analysis(symbol, self.config.rsi_period)
            momentum = analysis['momentum']
            rsi = analysis['rsi']
            volume_ratio = analysis['volume_ratio']
            adx = analysis.get('adx', 0)
            
            logger.debug(f"[{symbol}] Analysis: mom={momentum:.3f}%, RSI={rsi:.1f}, ADX={adx:.0f}, vol={volume_ratio:.2f}")
            
            # 3. Basic momentum check
            if abs(momentum) < self.config.min_momentum:
                logger.debug(f"[{symbol}] Momentum too weak: {momentum:.3f}% < {self.config.min_momentum:.2%}")
                return
            
            # 4. Volume check
            if volume_ratio < self.config.min_volume_ratio:
                logger.debug(f"[{symbol}] Volume too low: {volume_ratio:.2f} < {self.config.min_volume_ratio}")
                return
            
            # 5. v4: ADX filter (skip weak trends)
            if self.config.use_adx and adx < self.config.adx_min_strength:
                logger.debug(f"[{symbol}] ADX too weak: {adx:.0f} < {self.config.adx_min_strength:.0f} (sideways market)")
                return
            
            # 6. Hour performance filter
            if self.config.use_hour_filter:
                hour_stats = self.db.get_performance_by_hour()
                current_hour = datetime.now().strftime('%H')
                for stat in hour_stats:
                    if stat['hour'] == current_hour:
                        trades = stat['trades']
                        wins = stat['wins']
                        if trades >= 10:
                            win_rate = (wins / trades) * 100
                            if win_rate < self.config.min_hour_winrate:
                                logger.info(f"[{symbol}] ⏰ SKIP: Hour {current_hour}:00 has {win_rate:.0f}% win rate < {self.config.min_hour_winrate:.0f}%")
                                return
                        break
            
            # 7. v4: Multi-timeframe confirmation
            htf_trend = "NEUTRAL"
            if self.config.use_mtf:
                htf_trend = await self.binance.get_higher_timeframe_trend(symbol)
            
            # Determine direction
            direction = "UP" if momentum > 0 else "DOWN"
            
            # v4: MTF alignment check
            if self.config.use_mtf:
                if direction == "UP" and htf_trend == "BEARISH":
                    logger.info(f"[{symbol}] ⚠️ SKIP: MTF misalignment (1m=UP, 5m=BEARISH)")
                    return
                elif direction == "DOWN" and htf_trend == "BULLISH":
                    logger.info(f"[{symbol}] ⚠️ SKIP: MTF misalignment (1m=DOWN, 5m=BULLISH)")
                    return
            
            # RSI extremes filter
            if direction == "UP" and rsi > self.config.rsi_overbought:
                logger.info(f"[{symbol}] ⚠️ SKIP YES: RSI {rsi:.0f} > {self.config.rsi_overbought:.0f} (overbought)")
                return
            elif direction == "DOWN" and rsi < self.config.rsi_oversold:
                logger.info(f"[{symbol}] ⚠️ SKIP NO: RSI {rsi:.0f} < {self.config.rsi_oversold:.0f} (oversold)")
                return
            
            # Stochastic extremes filter
            if self.config.use_stochastic:
                stoch_k = analysis.get('stoch_k', 50)
                if direction == "UP" and stoch_k > self.config.stoch_overbought:
                    logger.info(f"[{symbol}] ⚠️ SKIP YES: Stoch {stoch_k:.0f} > {self.config.stoch_overbought:.0f} (overbought)")
                    return
                elif direction == "DOWN" and stoch_k < self.config.stoch_oversold:
                    logger.info(f"[{symbol}] ⚠️ SKIP NO: Stoch {stoch_k:.0f} < {self.config.stoch_oversold:.0f} (oversold)")
                    return
            
            # v4: Calculate confidence score
            confidence, reasons = self.calculate_confidence(analysis, direction)
            
            # Add HTF to reasons if aligned
            if htf_trend == ("BULLISH" if direction == "UP" else "BEARISH"):
                reasons.append(f"MTF={htf_trend}")
            
            # v4: Confidence threshold
            if self.config.use_confidence_scoring and confidence < self.config.min_confidence:
                logger.info(f"[{symbol}] ⚠️ SKIP: Confidence {confidence:.0%} < {self.config.min_confidence:.0%}")
                return
            
            # v4: Calculate dynamic bet size
            base_bet = self.config.bet_size
            dynamic_bet = self.get_dynamic_bet_size(base_bet, confidence)
            
            # Build reason string
            reason_str = ", ".join(reasons[:5])  # Limit to 5 reasons
            conf_emoji = "🔥" if confidence >= self.config.high_confidence else "✓"
            
            # Execute trade
            if direction == "UP":
                logger.info(f"[{symbol}] 📈 {conf_emoji} BUY YES (conf={confidence:.0%}, {reason_str})")
                await self.place_trade(pos, "YES", yes_price, 
                    f"Initial: conf={confidence:.0%}, {reason_str}",
                    confidence=confidence)
            else:
                logger.info(f"[{symbol}] 📉 {conf_emoji} BUY NO (conf={confidence:.0%}, {reason_str})")
                await self.place_trade(pos, "NO", no_price,
                    f"Initial: conf={confidence:.0%}, {reason_str}",
                    confidence=confidence)
            return
        
        # === SHARED SETUP (for hedge and add_winner) ===
        minutes_left = self.get_minutes_to_slot_end()
        binance_price = await self.binance.get_price(symbol)
        start_price = self.start_prices.get(symbol, 0)
        now = datetime.now(timezone.utc)
        
        # === HEDGE LOGIC ===
        can_hedge = (
            pos.hedge_count < self.config.max_hedges and
            minutes_left >= self.config.min_minutes_for_hedge and
            binance_price and start_price
        )
        
        if can_hedge:
            # Get dynamic trail trigger based on time
            trail_trigger = self.get_dynamic_trail_trigger()
            hedge_threshold = self.get_hedge_trigger_for_count(pos.hedge_count)
            effective_trigger = max(trail_trigger, hedge_threshold)
            
            # Volume check for hedge (optional)
            volume_ok = True
            if self.config.require_volume_for_hedge:
                volume_ratio = await self.binance.get_volume_ratio(symbol)
                volume_ok = volume_ratio >= self.config.min_volume_ratio
            
            if pos.initial_side == "YES" and pos.yes_peak_price > 0:
                price_drop = (pos.yes_peak_price - yes_price) / pos.yes_peak_price
                binance_losing = binance_price < start_price
                binance_change = ((binance_price - start_price) / start_price) * 100
                
                if price_drop >= effective_trigger and binance_losing and volume_ok:
                    if pos.hedge_trigger_time is None:
                        pos.hedge_trigger_time = now
                        pos.hedge_trigger_drop = price_drop
                        logger.info(f"[{symbol}] ⏱️ HEDGE PENDING #{pos.hedge_count+1}: "
                                   f"YES dropped {price_drop:.0%} from peak ${pos.yes_peak_price:.3f} (trigger={effective_trigger:.0%}), "
                                   f"Binance ${binance_price:.2f} < start ${start_price:.2f} ({binance_change:+.2f}%), "
                                   f"{minutes_left:.1f}min left, waiting {self.config.hedge_confirm_seconds}s...")
                    else:
                        elapsed = (now - pos.hedge_trigger_time).total_seconds()
                        if elapsed >= self.config.hedge_confirm_seconds:
                            logger.info(f"[{symbol}] 🛡️ HEDGE #{pos.hedge_count+1} CONFIRMED after {elapsed:.0f}s")
                            await self.place_trade(
                                pos, "NO", no_price, 
                                f"HEDGE #{pos.hedge_count+1}: YES -{price_drop:.0%}, Binance {binance_change:+.2f}%",
                                is_hedge=True,
                                price_drop=price_drop
                            )
                            pos.hedge_trigger_time = None
                            pos.hedge_trigger_drop = 0
                        else:
                            logger.debug(f"[{symbol}] Hedge waiting... ({elapsed:.0f}s/{self.config.hedge_confirm_seconds}s)")
                else:
                    if pos.hedge_trigger_time is not None:
                        reason = []
                        if not binance_losing:
                            reason.append(f"Binance ${binance_price:.2f} >= start")
                        if price_drop < effective_trigger:
                            reason.append(f"drop {price_drop:.0%} < trigger {effective_trigger:.0%}")
                        if not volume_ok:
                            reason.append("low volume")
                        logger.info(f"[{symbol}] ✅ HEDGE CANCELLED: {', '.join(reason)}")
                        pos.hedge_trigger_time = None
                        pos.hedge_trigger_drop = 0
            
            elif pos.initial_side == "NO" and pos.no_peak_price > 0:
                price_drop = (pos.no_peak_price - no_price) / pos.no_peak_price
                binance_losing = binance_price > start_price
                binance_change = ((binance_price - start_price) / start_price) * 100
                
                if price_drop >= effective_trigger and binance_losing and volume_ok:
                    if pos.hedge_trigger_time is None:
                        pos.hedge_trigger_time = now
                        pos.hedge_trigger_drop = price_drop
                        logger.info(f"[{symbol}] ⏱️ HEDGE PENDING #{pos.hedge_count+1}: "
                                   f"NO dropped {price_drop:.0%} from peak ${pos.no_peak_price:.3f} (trigger={effective_trigger:.0%}), "
                                   f"Binance ${binance_price:.2f} > start ${start_price:.2f} ({binance_change:+.2f}%), "
                                   f"{minutes_left:.1f}min left, waiting {self.config.hedge_confirm_seconds}s...")
                    else:
                        elapsed = (now - pos.hedge_trigger_time).total_seconds()
                        if elapsed >= self.config.hedge_confirm_seconds:
                            logger.info(f"[{symbol}] 🛡️ HEDGE #{pos.hedge_count+1} CONFIRMED after {elapsed:.0f}s")
                            await self.place_trade(
                                pos, "YES", yes_price,
                                f"HEDGE #{pos.hedge_count+1}: NO -{price_drop:.0%}, Binance {binance_change:+.2f}%",
                                is_hedge=True,
                                price_drop=price_drop
                            )
                            pos.hedge_trigger_time = None
                            pos.hedge_trigger_drop = 0
                        else:
                            logger.debug(f"[{symbol}] Hedge waiting... ({elapsed:.0f}s/{self.config.hedge_confirm_seconds}s)")
                else:
                    if pos.hedge_trigger_time is not None:
                        reason = []
                        if not binance_losing:
                            reason.append(f"Binance ${binance_price:.2f} <= start")
                        if price_drop < effective_trigger:
                            reason.append(f"drop {price_drop:.0%} < trigger {effective_trigger:.0%}")
                        if not volume_ok:
                            reason.append("low volume")
                        logger.info(f"[{symbol}] ✅ HEDGE CANCELLED: {', '.join(reason)}")
                        pos.hedge_trigger_time = None
                        pos.hedge_trigger_drop = 0
        
        # === ADD TO WINNER LOGIC ===
        if not self.config.add_winner_enabled:
            return
        
        if pos.add_count >= self.config.add_winner_max_adds:
            return
        
        if not binance_price or not start_price:
            return
        
        # Check time window for add_winner
        if minutes_left > self.config.add_winner_max_minutes or minutes_left < self.config.add_winner_min_minutes:
            if pos.add_trigger_time is not None:
                pos.add_trigger_time = None
            return
        
        # Calculate distance from start
        binance_distance = abs(binance_price - start_price) / start_price
        binance_change = ((binance_price - start_price) / start_price) * 100
        
        # Check if we're winning and should add
        should_add = False
        add_side = None
        add_price = 0
        
        if pos.initial_side == "YES":
            binance_winning = binance_price > start_price
            token_safe = yes_price >= self.config.add_winner_min_token_price
            distance_ok = binance_distance >= self.config.add_winner_min_distance
            not_hedging = pos.hedge_trigger_time is None
            
            if binance_winning and token_safe and distance_ok and not_hedging:
                should_add = True
                add_side = "YES"
                add_price = yes_price
                
        elif pos.initial_side == "NO":
            binance_winning = binance_price < start_price
            token_safe = no_price >= self.config.add_winner_min_token_price
            distance_ok = binance_distance >= self.config.add_winner_min_distance
            not_hedging = pos.hedge_trigger_time is None
            
            if binance_winning and token_safe and distance_ok and not_hedging:
                should_add = True
                add_side = "NO"
                add_price = no_price
        
        if should_add:
            if pos.add_trigger_time is None:
                pos.add_trigger_time = now
                logger.info(f"[{symbol}] 🎯 ADD PENDING #{pos.add_count+1}: "
                           f"{add_side}=${add_price:.3f} (>{self.config.add_winner_min_token_price:.0%}), "
                           f"Binance dist={binance_distance:.2%} (>{self.config.add_winner_min_distance:.1%}), "
                           f"{minutes_left:.1f}min left, waiting {self.config.add_winner_confirm_seconds}s...")
            else:
                elapsed = (now - pos.add_trigger_time).total_seconds()
                if elapsed >= self.config.add_winner_confirm_seconds:
                    logger.info(f"[{symbol}] 🚀 ADD #{pos.add_count+1} CONFIRMED after {elapsed:.0f}s")
                    await self.place_trade(
                        pos, add_side, add_price,
                        f"ADD #{pos.add_count+1}: {add_side}=${add_price:.2f}, Binance {binance_change:+.2f}%, {minutes_left:.1f}min left",
                        is_add=True
                    )
                    pos.add_trigger_time = None
                    pos.add_count += 1
                else:
                    logger.debug(f"[{symbol}] Add waiting... ({elapsed:.0f}s/{self.config.add_winner_confirm_seconds}s)")
        else:
            if pos.add_trigger_time is not None:
                logger.info(f"[{symbol}] ✅ ADD CANCELLED: conditions no longer met")
                pos.add_trigger_time = None
    
    async def place_trade(self, pos: Position, side: str, price: float, reason: str, is_hedge: bool = False, is_add: bool = False, price_drop: float = 0.0, confidence: float = 0.5):
        """Place a trade"""
        MIN_SHARES = 5  # Polymarket minimum
        
        if price <= 0 or price >= 1:
            return
        
        # Calculate size based on trade type
        if is_hedge:
            base_amount = self.config.bet_size * self.config.trail_size
            # Scale hedge size based on drop magnitude
            bet_amount = self.calculate_hedge_size(base_amount, price_drop)
            remaining = bet_amount  # No max_position limit for hedges
            logger.debug(f"[{pos.symbol}] Hedge size: base=${base_amount:.2f}, scaled=${bet_amount:.2f} (drop={price_drop:.0%})")
        elif is_add:
            bet_amount = self.config.bet_size * self.config.add_winner_size
            remaining = bet_amount  # No max_position limit for adds
            logger.debug(f"[{pos.symbol}] Add winner size: ${bet_amount:.2f}")
        else:
            # v4: Dynamic bet sizing for initial trades
            base_amount = self.config.bet_size
            bet_amount = self.get_dynamic_bet_size(base_amount, confidence)
            remaining = self.config.max_position - pos.total_cost
            bet_amount = min(bet_amount, remaining)
            if bet_amount != base_amount:
                logger.debug(f"[{pos.symbol}] Dynamic size: base=${base_amount:.2f}, adjusted=${bet_amount:.2f} (conf={confidence:.0%})")
        
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
        
        # Execute - different emoji for each type
        if is_hedge:
            emoji = "🛡️"
        elif is_add:
            emoji = "🚀"
        else:
            emoji = "🎲"
        
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
            elif is_add:
                self.stats['adds'] += 1
            
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
            
            # v4: Update win/loss tracking
            won = (winner == pos.initial_side)
            self.update_win_loss(won)
        
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
        logger.info(f"POLYNEKO v4 [{'🔬 SIM' if self.config.simulation_mode else '💰 PROD'}]")
        logger.info("=" * 60)
        logger.info("  Strategy: MULTI-INDICATOR CONFIDENCE SCORING")
        self.config.print_config()
        logger.info("")
        logger.info("  📊 Entry: Confidence scoring with MACD/Stoch/ADX/BB/ATR")
        logger.info("  🔍 MTF: Multi-timeframe trend confirmation")
        logger.info("  🛡️ Hedge: (1) token drops from peak + (2) Binance crosses start")
        logger.info("  ⏱️ Dynamic triggers based on time remaining")
        logger.info("  📈 Dynamic bet sizing based on confidence")
        if self.config.add_winner_enabled:
            logger.info("  🚀 Add to winner: doubles down when winning near slot end")
        logger.info("  🛑 Martingale prevention: cooldown after losses")
        
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
        
        # Start price monitor loop (checks hedge conditions every 2s)
        asyncio.create_task(self._price_monitor_loop())
        
        try:
            await self._main_loop()
        finally:
            await self.stop()
    
    async def _price_monitor_loop(self):
        """Okresowo sprawdzaj ceny i warunki hedge (niezależnie od WebSocket)"""
        logger.info("📡 Price monitor started (checking every 2s)")
        
        while self.running:
            try:
                for slug in list(self.positions.keys()):
                    pos = self.positions.get(slug)
                    if pos and pos.total_cost > 0 and len(pos.trades) > 0:
                        await self.check_signals(slug)
                
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Price monitor error: {e}")
                await asyncio.sleep(5)
    
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
        logger.info(f"POLYNEKO v4 [{mode}] | Trades: {self.stats['trades']} | Hedges: {self.stats['hedges']} | Adds: {self.stats['adds']}")
        logger.info(f"WSS msgs: {self.stats['wss_messages']} | Slots: {self.stats['slots']}")
        logger.info(f"Session: W:{self.session_wins} L:{self.session_losses} | Streak: W{self.win_streak} L{self.consecutive_losses}")
        
        if not self.config.simulation_mode:
            logger.info(f"Orders: {self.stats['orders_sent']} sent, {self.stats['orders_filled']} filled, {self.stats['orders_failed']} failed")
        
        logger.info(f"Total P&L: ${self.stats['total_pnl']:+.2f}")
        
        for slug, pos in self.positions.items():
            if pos.total_cost > 0:
                logger.info(f"  [{pos.symbol}] {pos.initial_side} | YES={pos.yes_shares:.0f}@${pos.avg_yes_price:.3f} "
                           f"NO={pos.no_shares:.0f}@${pos.avg_no_price:.3f} | ${pos.total_cost:.2f} | H:{pos.hedge_count} A:{pos.add_count}")
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
