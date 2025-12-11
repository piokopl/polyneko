#!/usr/bin/env python3
"""
POLYNEKO v2 - Web Dashboard

Real-time monitoring dashboard for the prediction bot.
Runs alongside the main bot and provides:
- Live statistics
- Trade history
- Settlement results
- P&L charts
- Performance analytics
"""

import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from aiohttp import web

logger = logging.getLogger(__name__)

# ========================== HTML TEMPLATE ==========================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🐱 PolyNeko v2 Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e4e4e4;
            padding: 20px;
        }
        
        .header {
            text-align: center;
            margin-bottom: 30px;
            padding: 20px;
            background: rgba(255,255,255,0.05);
            border-radius: 15px;
            backdrop-filter: blur(10px);
        }
        
        .header h1 {
            font-size: 2.5em;
            background: linear-gradient(90deg, #ff6b6b, #feca57, #48dbfb);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }
        
        .header .subtitle {
            color: #888;
            font-size: 1.1em;
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .card {
            background: rgba(255,255,255,0.08);
            border-radius: 15px;
            padding: 25px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            transition: transform 0.3s, box-shadow 0.3s;
        }
        
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
        }
        
        .card h3 {
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #888;
            margin-bottom: 15px;
        }
        
        .card .value {
            font-size: 2.5em;
            font-weight: 700;
            margin-bottom: 10px;
        }
        
        .card .change {
            font-size: 0.95em;
        }
        
        .positive { color: #00d26a; }
        .negative { color: #ff6b6b; }
        .neutral { color: #feca57; }
        
        .chart-container {
            background: rgba(255,255,255,0.08);
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 30px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        
        .chart-container h3 {
            margin-bottom: 20px;
            color: #fff;
        }
        
        .table-container {
            background: rgba(255,255,255,0.08);
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 30px;
            border: 1px solid rgba(255,255,255,0.1);
            overflow-x: auto;
        }
        
        .table-container h3 {
            margin-bottom: 20px;
            color: #fff;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th, td {
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        
        th {
            color: #888;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.85em;
            letter-spacing: 1px;
        }
        
        tr:hover {
            background: rgba(255,255,255,0.05);
        }
        
        .badge {
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.8em;
            font-weight: 600;
        }
        
        .badge-yes { background: rgba(0,210,106,0.2); color: #00d26a; }
        .badge-no { background: rgba(255,107,107,0.2); color: #ff6b6b; }
        .badge-hedge { background: rgba(254,202,87,0.2); color: #feca57; }
        
        .symbol-badges {
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            margin-top: 20px;
        }
        
        .symbol-badge {
            background: rgba(255,255,255,0.1);
            padding: 10px 20px;
            border-radius: 10px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        
        .symbol-badge .name {
            font-weight: 700;
            font-size: 1.1em;
            margin-bottom: 5px;
        }
        
        .symbol-badge .pnl {
            font-size: 0.9em;
        }
        
        .refresh-info {
            text-align: center;
            color: #666;
            margin-top: 20px;
            font-size: 0.9em;
        }
        
        .two-col {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }
        
        @media (max-width: 900px) {
            .two-col {
                grid-template-columns: 1fr;
            }
        }
        
        .live-indicator {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: rgba(0,210,106,0.2);
            color: #00d26a;
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.9em;
        }
        
        .live-dot {
            width: 8px;
            height: 8px;
            background: #00d26a;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .performance-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(60px, 1fr));
            gap: 5px;
            margin-top: 15px;
        }
        
        .hour-cell {
            padding: 10px 5px;
            text-align: center;
            border-radius: 8px;
            font-size: 0.85em;
        }
        
        .hour-cell .hour {
            font-weight: 600;
            margin-bottom: 3px;
        }
        
        .hour-cell .pnl {
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🐱 PolyNeko v2</h1>
        <p class="subtitle">Real-time Trading Dashboard</p>
        <div style="margin-top: 15px;">
            <span class="live-indicator">
                <span class="live-dot"></span>
                Live - Auto-refresh every 10s
            </span>
        </div>
    </div>
    
    <div class="grid">
        <div class="card">
            <h3>💰 Total P&L</h3>
            <div class="value" id="total-pnl">$0.00</div>
            <div class="change" id="total-pnl-today">Today: $0.00</div>
        </div>
        
        <div class="card">
            <h3>📊 Win Rate</h3>
            <div class="value" id="win-rate">0%</div>
            <div class="change" id="total-settlements">0 settlements</div>
        </div>
        
        <div class="card">
            <h3>📈 ROI</h3>
            <div class="value" id="roi">0%</div>
            <div class="change" id="total-cost">Cost: $0.00</div>
        </div>
        
        <div class="card">
            <h3>🎯 Total Trades</h3>
            <div class="value" id="total-trades">0</div>
            <div class="change" id="trades-today">Today: 0</div>
        </div>
    </div>
    
    <div class="chart-container">
        <h3>📈 P&L Over Time (Last 24h)</h3>
        <canvas id="pnl-chart" height="100"></canvas>
    </div>
    
    <div class="table-container">
        <h3>🏆 Performance by Symbol</h3>
        <div class="symbol-badges" id="symbol-stats"></div>
    </div>
    
    <div class="table-container">
        <h3>⏰ Performance by Hour</h3>
        <div class="performance-grid" id="hourly-performance"></div>
    </div>
    
    <div class="two-col">
        <div class="table-container">
            <h3>📜 Recent Settlements</h3>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Symbol</th>
                        <th>Winner</th>
                        <th>P&L</th>
                    </tr>
                </thead>
                <tbody id="settlements-table"></tbody>
            </table>
        </div>
        
        <div class="table-container">
            <h3>🔄 Recent Trades</h3>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Size</th>
                        <th>Type</th>
                    </tr>
                </thead>
                <tbody id="trades-table"></tbody>
            </table>
        </div>
    </div>
    
    <div class="refresh-info">
        Last updated: <span id="last-update">-</span>
    </div>
    
    <script>
        let pnlChart = null;
        
        function formatTime(isoString) {
            const date = new Date(isoString);
            return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
        }
        
        function formatPnl(value) {
            const formatted = '$' + Math.abs(value).toFixed(2);
            if (value > 0) return '+' + formatted;
            if (value < 0) return '-' + formatted.slice(1);
            return formatted;
        }
        
        function getPnlClass(value) {
            if (value > 0) return 'positive';
            if (value < 0) return 'negative';
            return 'neutral';
        }
        
        function getHourColor(pnl) {
            if (pnl > 5) return 'rgba(0,210,106,0.4)';
            if (pnl > 0) return 'rgba(0,210,106,0.2)';
            if (pnl < -5) return 'rgba(255,107,107,0.4)';
            if (pnl < 0) return 'rgba(255,107,107,0.2)';
            return 'rgba(255,255,255,0.1)';
        }
        
        async function fetchData() {
            try {
                const response = await fetch('/api/stats');
                const data = await response.json();
                updateDashboard(data);
            } catch (error) {
                console.error('Error fetching data:', error);
            }
        }
        
        function updateDashboard(data) {
            // Main stats
            const totalPnlEl = document.getElementById('total-pnl');
            totalPnlEl.textContent = formatPnl(data.total_pnl);
            totalPnlEl.className = 'value ' + getPnlClass(data.total_pnl);
            
            document.getElementById('total-pnl-today').textContent = 'Today: ' + formatPnl(data.today_pnl);
            document.getElementById('total-pnl-today').className = 'change ' + getPnlClass(data.today_pnl);
            
            document.getElementById('win-rate').textContent = data.win_rate.toFixed(1) + '%';
            document.getElementById('total-settlements').textContent = data.total_settlements + ' settlements';
            
            const roiEl = document.getElementById('roi');
            roiEl.textContent = (data.roi >= 0 ? '+' : '') + data.roi.toFixed(1) + '%';
            roiEl.className = 'value ' + getPnlClass(data.roi);
            
            document.getElementById('total-cost').textContent = 'Cost: $' + data.total_cost.toFixed(2);
            document.getElementById('total-trades').textContent = data.total_trades;
            document.getElementById('trades-today').textContent = 'Today: ' + (data.today_settlements || 0);
            
            // Symbol stats
            const symbolContainer = document.getElementById('symbol-stats');
            symbolContainer.innerHTML = data.by_symbol.map(s => `
                <div class="symbol-badge">
                    <span class="name">${s.symbol}</span>
                    <span class="pnl ${getPnlClass(s.pnl)}">${formatPnl(s.pnl)}</span>
                    <span style="font-size:0.8em;color:#888">${s.wins}/${s.trades} wins</span>
                </div>
            `).join('');
            
            // Hourly performance
            const hourlyContainer = document.getElementById('hourly-performance');
            hourlyContainer.innerHTML = data.hourly_performance.map(h => `
                <div class="hour-cell" style="background: ${getHourColor(h.pnl)}">
                    <div class="hour">${h.hour}:00</div>
                    <div class="pnl ${getPnlClass(h.pnl)}">${formatPnl(h.pnl)}</div>
                </div>
            `).join('');
            
            // P&L Chart
            updateChart(data.hourly_pnl);
            
            // Recent settlements
            const settlementsTable = document.getElementById('settlements-table');
            settlementsTable.innerHTML = data.recent_settlements.slice(0, 10).map(s => `
                <tr>
                    <td>${formatTime(s.timestamp)}</td>
                    <td><strong>${s.symbol}</strong></td>
                    <td><span class="badge badge-${s.winner.toLowerCase()}">${s.winner}</span></td>
                    <td class="${getPnlClass(s.pnl)}">${formatPnl(s.pnl)}</td>
                </tr>
            `).join('');
            
            // Recent trades
            const tradesTable = document.getElementById('trades-table');
            tradesTable.innerHTML = data.recent_trades.slice(0, 10).map(t => `
                <tr>
                    <td>${formatTime(t.timestamp)}</td>
                    <td><strong>${t.symbol}</strong></td>
                    <td><span class="badge badge-${t.side.toLowerCase()}">${t.side}</span></td>
                    <td>${t.shares.toFixed(0)} @ $${t.price.toFixed(2)}</td>
                    <td>${t.is_hedge ? '<span class="badge badge-hedge">HEDGE</span>' : 'Initial'}</td>
                </tr>
            `).join('');
            
            // Last update
            document.getElementById('last-update').textContent = new Date().toLocaleString();
        }
        
        function updateChart(hourlyData) {
            const ctx = document.getElementById('pnl-chart').getContext('2d');
            
            const labels = hourlyData.map(h => h.hour.split(' ')[1] || h.hour);
            const values = hourlyData.map(h => h.pnl);
            
            // Calculate cumulative P&L
            let cumulative = 0;
            const cumulativeValues = values.map(v => {
                cumulative += v;
                return cumulative;
            });
            
            if (pnlChart) {
                pnlChart.data.labels = labels;
                pnlChart.data.datasets[0].data = values;
                pnlChart.data.datasets[1].data = cumulativeValues;
                pnlChart.update();
            } else {
                pnlChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: 'Hourly P&L',
                            data: values,
                            backgroundColor: values.map(v => v >= 0 ? 'rgba(0,210,106,0.6)' : 'rgba(255,107,107,0.6)'),
                            borderRadius: 5,
                            order: 2
                        }, {
                            label: 'Cumulative P&L',
                            data: cumulativeValues,
                            type: 'line',
                            borderColor: '#48dbfb',
                            backgroundColor: 'rgba(72,219,251,0.1)',
                            fill: true,
                            tension: 0.4,
                            pointRadius: 3,
                            order: 1
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: true,
                        plugins: {
                            legend: {
                                labels: { color: '#888' }
                            }
                        },
                        scales: {
                            x: {
                                ticks: { color: '#888' },
                                grid: { color: 'rgba(255,255,255,0.1)' }
                            },
                            y: {
                                ticks: { 
                                    color: '#888',
                                    callback: function(value) {
                                        return '$' + value.toFixed(0);
                                    }
                                },
                                grid: { color: 'rgba(255,255,255,0.1)' }
                            }
                        }
                    }
                });
            }
        }
        
        // Initial fetch and auto-refresh
        fetchData();
        setInterval(fetchData, 10000);
    </script>
</body>
</html>
'''

# ========================== DASHBOARD SERVER ==========================

class Dashboard:
    """Web dashboard server"""
    
    def __init__(self, db_path: str = "polyneko.db", port: int = 80):
        self.db_path = db_path
        self.port = port
        self.app = web.Application()
        self.runner = None
        self._setup_routes()
    
    def _setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/api/stats', self.handle_stats)
        self.app.router.add_get('/api/trades', self.handle_trades)
        self.app.router.add_get('/api/settlements', self.handle_settlements)
    
    def _get_db(self):
        """Get database connection"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    async def handle_index(self, request):
        """Serve main dashboard page"""
        return web.Response(text=HTML_TEMPLATE, content_type='text/html')
    
    async def handle_stats(self, request):
        """API: Get all stats"""
        conn = self._get_db()
        try:
            stats = {}
            
            # Total trades
            cursor = conn.execute('SELECT COUNT(*) as cnt FROM trades')
            stats['total_trades'] = cursor.fetchone()['cnt']
            
            # Total settlements
            cursor = conn.execute('SELECT COUNT(*) as cnt FROM settlements')
            stats['total_settlements'] = cursor.fetchone()['cnt']
            
            # Total P&L
            cursor = conn.execute('SELECT COALESCE(SUM(pnl), 0) as total FROM settlements')
            stats['total_pnl'] = cursor.fetchone()['total']
            
            # Win rate
            cursor = conn.execute('SELECT COUNT(*) as cnt FROM settlements WHERE pnl > 0')
            wins = cursor.fetchone()['cnt']
            stats['win_rate'] = (wins / stats['total_settlements'] * 100) if stats['total_settlements'] > 0 else 0
            
            # Total cost
            cursor = conn.execute('SELECT COALESCE(SUM(cost), 0) as total FROM trades')
            stats['total_cost'] = cursor.fetchone()['total']
            
            # ROI
            stats['roi'] = (stats['total_pnl'] / stats['total_cost'] * 100) if stats['total_cost'] > 0 else 0
            
            # By symbol
            cursor = conn.execute('''
                SELECT symbol, COUNT(*) as trades, COALESCE(SUM(pnl), 0) as pnl, 
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
                FROM settlements GROUP BY symbol
            ''')
            stats['by_symbol'] = [dict(row) for row in cursor.fetchall()]
            
            # Today's stats
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            cursor = conn.execute('''
                SELECT COUNT(*) as cnt, COALESCE(SUM(pnl), 0) as pnl
                FROM settlements WHERE timestamp LIKE ?
            ''', (f'{today}%',))
            row = cursor.fetchone()
            stats['today_settlements'] = row['cnt']
            stats['today_pnl'] = row['pnl']
            
            # Hourly P&L (last 24h)
            cursor = conn.execute('''
                SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour, 
                       COALESCE(SUM(pnl), 0) as pnl
                FROM settlements 
                WHERE timestamp > datetime('now', '-24 hours')
                GROUP BY hour ORDER BY hour
            ''')
            stats['hourly_pnl'] = [dict(row) for row in cursor.fetchall()]
            
            # Performance by hour of day
            cursor = conn.execute('''
                SELECT strftime('%H', timestamp) as hour, 
                       COUNT(*) as trades,
                       COALESCE(SUM(pnl), 0) as pnl,
                       AVG(pnl) as avg_pnl,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
                FROM settlements 
                GROUP BY hour ORDER BY hour
            ''')
            stats['hourly_performance'] = [dict(row) for row in cursor.fetchall()]
            
            # Recent settlements
            cursor = conn.execute('''
                SELECT * FROM settlements ORDER BY timestamp DESC LIMIT 20
            ''')
            stats['recent_settlements'] = [dict(row) for row in cursor.fetchall()]
            
            # Recent trades
            cursor = conn.execute('''
                SELECT * FROM trades ORDER BY timestamp DESC LIMIT 20
            ''')
            stats['recent_trades'] = [dict(row) for row in cursor.fetchall()]
            
            return web.json_response(stats)
            
        finally:
            conn.close()
    
    async def handle_trades(self, request):
        """API: Get recent trades"""
        limit = int(request.query.get('limit', 50))
        conn = self._get_db()
        try:
            cursor = conn.execute('''
                SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
            ''', (limit,))
            trades = [dict(row) for row in cursor.fetchall()]
            return web.json_response(trades)
        finally:
            conn.close()
    
    async def handle_settlements(self, request):
        """API: Get recent settlements"""
        limit = int(request.query.get('limit', 50))
        conn = self._get_db()
        try:
            cursor = conn.execute('''
                SELECT * FROM settlements ORDER BY timestamp DESC LIMIT ?
            ''', (limit,))
            settlements = [dict(row) for row in cursor.fetchall()]
            return web.json_response(settlements)
        finally:
            conn.close()
    
    async def start(self):
        """Start the dashboard server"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await site.start()
        logger.info(f"🌐 Dashboard running at http://localhost:{self.port}")
    
    async def stop(self):
        """Stop the dashboard server"""
        if self.runner:
            await self.runner.cleanup()


# ========================== STANDALONE MODE ==========================

async def main():
    """Run dashboard standalone"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S'
    )
    
    db_path = os.getenv('DB_PATH', 'polyneko.db')
    port = int(os.getenv('DASHBOARD_PORT', '80'))
    
    dashboard = Dashboard(db_path=db_path, port=port)
    await dashboard.start()
    
    logger.info("Dashboard is running. Press Ctrl+C to stop.")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await dashboard.stop()


if __name__ == "__main__":
    asyncio.run(main())
