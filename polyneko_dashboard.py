#!/usr/bin/env python3
"""
POLYNEKO v4 - Web Dashboard + Analytics + Auto-Tuning
======================================================
Beautiful real-time dashboard with:
- Live statistics & P&L charts
- Advanced analytics (Sharpe, Sortino, Max Drawdown)
- Auto-parameter tuning recommendations
- Win rate heatmaps
- Trade/Settlement history
"""

import os
import json
import sqlite3
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger(__name__)

# ========================== HTML TEMPLATE ==========================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🐱 PolyNeko v4 Dashboard</title>
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
            background-clip: text;
            margin-bottom: 10px;
        }
        
        .header .subtitle {
            color: #888;
            font-size: 1.1em;
        }
        
        /* Tabs */
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        
        .tab {
            padding: 12px 24px;
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.3s;
            color: #888;
            font-weight: 500;
        }
        
        .tab:hover {
            background: rgba(255,255,255,0.15);
            color: #fff;
        }
        
        .tab.active {
            background: linear-gradient(90deg, #ff6b6b, #feca57);
            color: #1a1a2e;
            border-color: transparent;
        }
        
        .tab-content {
            display: none;
        }
        
        .tab-content.active {
            display: block;
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
            font-size: 0.8em;
        }
        
        .hour-cell .hour {
            color: #888;
            margin-bottom: 3px;
        }
        
        .hour-cell .wr {
            font-weight: 700;
        }
        
        /* Recommendation cards */
        .recommendation {
            background: linear-gradient(135deg, rgba(0,210,106,0.1), rgba(0,210,106,0.05));
            border-left: 4px solid #00d26a;
            padding: 20px;
            margin-bottom: 15px;
            border-radius: 0 12px 12px 0;
        }
        
        .recommendation.medium {
            background: linear-gradient(135deg, rgba(254,202,87,0.1), rgba(254,202,87,0.05));
            border-left-color: #feca57;
        }
        
        .rec-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        
        .rec-param {
            font-weight: 700;
            font-size: 1.1em;
            color: #00d26a;
        }
        
        .recommendation.medium .rec-param {
            color: #feca57;
        }
        
        .rec-confidence {
            background: #00d26a;
            color: #1a1a2e;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
        }
        
        .recommendation.medium .rec-confidence {
            background: #feca57;
        }
        
        .rec-values {
            font-size: 1em;
            margin-bottom: 8px;
        }
        
        .rec-values strong {
            color: #48dbfb;
        }
        
        .rec-reason {
            color: #888;
            font-size: 0.9em;
            margin-bottom: 5px;
        }
        
        .rec-expected {
            color: #00d26a;
            font-size: 0.9em;
        }
        
        .metrics-row {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 15px;
        }
        
        .metric-mini {
            background: rgba(255,255,255,0.05);
            padding: 15px 20px;
            border-radius: 10px;
            text-align: center;
            min-width: 120px;
        }
        
        .metric-mini .label {
            color: #888;
            font-size: 0.8em;
            text-transform: uppercase;
            margin-bottom: 5px;
        }
        
        .metric-mini .val {
            font-size: 1.4em;
            font-weight: 700;
        }
        
        .heatmap {
            display: grid;
            grid-template-columns: repeat(24, 1fr);
            gap: 3px;
            margin-top: 15px;
        }
        
        .heatmap-cell {
            aspect-ratio: 1;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.7rem;
            cursor: pointer;
            transition: transform 0.2s;
        }
        
        .heatmap-cell:hover {
            transform: scale(1.2);
            z-index: 10;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🐱 PolyNeko v4</h1>
        <p class="subtitle">Advanced Trading Analytics Dashboard</p>
        <div class="live-indicator" style="margin-top: 15px;">
            <div class="live-dot"></div>
            <span>Live • Auto-refresh 10s</span>
        </div>
    </div>
    
    <div class="tabs">
        <div class="tab active" onclick="showTab('overview')">📊 Overview</div>
        <div class="tab" onclick="showTab('analytics')">📈 Analytics</div>
        <div class="tab" onclick="showTab('heatmap')">🗓️ Heatmap</div>
        <div class="tab" onclick="showTab('tuning')">🔧 Auto-Tuning</div>
        <div class="tab" onclick="showTab('history')">📜 History</div>
    </div>
    
    <!-- OVERVIEW TAB -->
    <div id="overview" class="tab-content active">
        <div class="grid">
            <div class="card">
                <h3>💰 Total P&L</h3>
                <div class="value" id="totalPnl">$0.00</div>
                <div class="change" id="todayPnl">Today: $0.00</div>
            </div>
            <div class="card">
                <h3>🎯 Win Rate</h3>
                <div class="value" id="winRate">0%</div>
                <div class="change" id="winLoss">0W / 0L</div>
            </div>
            <div class="card">
                <h3>📊 Total Trades</h3>
                <div class="value" id="totalTrades">0</div>
                <div class="change" id="todayTrades">Today: 0</div>
            </div>
            <div class="card">
                <h3>💹 ROI</h3>
                <div class="value" id="roi">0%</div>
                <div class="change" id="totalCost">Invested: $0</div>
            </div>
        </div>
        
        <div class="chart-container">
            <h3>📈 P&L Over Time (Last 24h)</h3>
            <canvas id="pnlChart" height="100"></canvas>
        </div>
        
        <div class="card">
            <h3>🪙 Performance by Symbol</h3>
            <div class="symbol-badges" id="symbolBadges"></div>
        </div>
    </div>
    
    <!-- ANALYTICS TAB -->
    <div id="analytics" class="tab-content">
        <div class="metrics-row" id="advancedMetrics">
            <div class="metric-mini">
                <div class="label">Sharpe Ratio</div>
                <div class="val" id="sharpeRatio">-</div>
            </div>
            <div class="metric-mini">
                <div class="label">Sortino Ratio</div>
                <div class="val" id="sortinoRatio">-</div>
            </div>
            <div class="metric-mini">
                <div class="label">Profit Factor</div>
                <div class="val" id="profitFactor">-</div>
            </div>
            <div class="metric-mini">
                <div class="label">Expectancy</div>
                <div class="val" id="expectancy">-</div>
            </div>
            <div class="metric-mini">
                <div class="label">Max Drawdown</div>
                <div class="val negative" id="maxDrawdown">-</div>
            </div>
        </div>
        
        <div class="two-col">
            <div class="card">
                <h3>🛡️ Hedge Effectiveness</h3>
                <div id="hedgeStats">Loading...</div>
            </div>
            <div class="card">
                <h3>🔥 Streak Analysis</h3>
                <div id="streakStats">Loading...</div>
            </div>
        </div>
        
        <div class="chart-container" style="margin-top: 20px;">
            <h3>💰 Equity Curve</h3>
            <canvas id="equityChart" height="100"></canvas>
        </div>
    </div>
    
    <!-- HEATMAP TAB -->
    <div id="heatmap" class="tab-content">
        <div class="card">
            <h3>⏰ Win Rate by Hour (UTC)</h3>
            <div class="heatmap" id="hourlyHeatmap"></div>
            <p style="margin-top: 15px; color: #666; font-size: 0.85em;">
                🟢 High win rate &nbsp; 🟡 Medium &nbsp; 🔴 Low &nbsp; • Hover for details
            </p>
        </div>
        
        <div class="card" style="margin-top: 20px;">
            <h3>📅 Performance by Day of Week</h3>
            <div id="dailyStats"></div>
        </div>
        
        <div class="card" style="margin-top: 20px;">
            <h3>🕐 Detailed Hourly Performance</h3>
            <div class="performance-grid" id="hourlyPerformance"></div>
        </div>
    </div>
    
    <!-- AUTO-TUNING TAB -->
    <div id="tuning" class="tab-content">
        <div class="card" style="margin-bottom: 20px;">
            <h3>🔧 Auto-Tuning Recommendations</h3>
            <p style="color: #888; margin-bottom: 20px;">
                AI-powered parameter optimization based on your trading history.
                Recommendations update every 6 hours.
            </p>
            <div id="recommendations">Loading...</div>
        </div>
    </div>
    
    <!-- HISTORY TAB -->
    <div id="history" class="tab-content">
        <div class="two-col">
            <div class="table-container">
                <h3>📝 Recent Settlements</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Symbol</th>
                            <th>Winner</th>
                            <th>P&L</th>
                        </tr>
                    </thead>
                    <tbody id="settlementsTable"></tbody>
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
                            <th>Cost</th>
                        </tr>
                    </thead>
                    <tbody id="tradesTable"></tbody>
                </table>
            </div>
        </div>
    </div>
    
    <div class="refresh-info">
        Last updated: <span id="lastUpdate">-</span>
    </div>
    
    <script>
        let pnlChart = null;
        let equityChart = null;
        
        function showTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            event.target.classList.add('active');
        }
        
        function formatValue(val, type = 'number') {
            if (val === null || val === undefined) return '-';
            if (val >= 999999) return '∞';
            if (val <= -999999) return '-∞';
            if (isNaN(val)) return '-';
            if (type === 'pct') return val.toFixed(1) + '%';
            if (type === 'money') return '$' + val.toFixed(2);
            if (type === 'ratio') return val.toFixed(2);
            return val.toString();
        }
        
        function getHeatmapColor(winRate, trades) {
            if (trades < 3) return 'rgba(255,255,255,0.1)';
            if (winRate >= 60) return '#00d26a';
            if (winRate >= 55) return '#4ade80';
            if (winRate >= 50) return '#a3e635';
            if (winRate >= 45) return '#feca57';
            if (winRate >= 40) return '#fb923c';
            return '#ff6b6b';
        }
        
        async function fetchData() {
            try {
                const statsRes = await fetch('/api/stats');
                const stats = await statsRes.json();
                
                const analyticsRes = await fetch('/api/analytics');
                const analytics = await analyticsRes.json();
                
                const tuningRes = await fetch('/api/tuning');
                const tuning = await tuningRes.json();
                
                updateOverview(stats);
                updateAnalytics(analytics);
                updateHeatmap(analytics);
                updateTuning(tuning);
                updateHistory(stats);
                updateCharts(stats, analytics);
                
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
                
            } catch (err) {
                console.error('Error fetching data:', err);
            }
        }
        
        function updateOverview(stats) {
            const totalPnl = stats.total_pnl || 0;
            document.getElementById('totalPnl').textContent = '$' + totalPnl.toFixed(2);
            document.getElementById('totalPnl').className = 'value ' + (totalPnl >= 0 ? 'positive' : 'negative');
            
            const todayPnl = stats.today_pnl || 0;
            document.getElementById('todayPnl').textContent = 'Today: $' + todayPnl.toFixed(2);
            document.getElementById('todayPnl').className = 'change ' + (todayPnl >= 0 ? 'positive' : 'negative');
            
            const winRate = stats.win_rate || 0;
            document.getElementById('winRate').textContent = winRate.toFixed(1) + '%';
            document.getElementById('winRate').className = 'value ' + (winRate >= 50 ? 'positive' : 'negative');
            
            const wins = stats.wins || 0;
            const losses = stats.total_settlements - wins;
            document.getElementById('winLoss').textContent = wins + 'W / ' + losses + 'L';
            
            document.getElementById('totalTrades').textContent = stats.total_settlements || 0;
            document.getElementById('todayTrades').textContent = 'Today: ' + (stats.today_settlements || 0);
            
            const roi = stats.roi || 0;
            document.getElementById('roi').textContent = roi.toFixed(1) + '%';
            document.getElementById('roi').className = 'value ' + (roi >= 0 ? 'positive' : 'negative');
            document.getElementById('totalCost').textContent = 'Invested: $' + (stats.total_cost || 0).toFixed(0);
            
            const symbolBadges = document.getElementById('symbolBadges');
            symbolBadges.innerHTML = (stats.by_symbol || []).map(s => {
                const wr = s.trades > 0 ? (s.wins / s.trades * 100).toFixed(0) : 0;
                const pnlClass = s.pnl >= 0 ? 'positive' : 'negative';
                return `
                    <div class="symbol-badge">
                        <div class="name">${s.symbol}</div>
                        <div class="pnl ${pnlClass}">$${s.pnl.toFixed(2)}</div>
                        <div style="color: #888; font-size: 0.8em;">${wr}% (${s.trades})</div>
                    </div>
                `;
            }).join('');
        }
        
        function updateAnalytics(analytics) {
            document.getElementById('sharpeRatio').textContent = formatValue(analytics.sharpe_ratio, 'ratio');
            document.getElementById('sharpeRatio').className = 'val ' + (analytics.sharpe_ratio > 1 ? 'positive' : '');
            
            document.getElementById('sortinoRatio').textContent = formatValue(analytics.sortino_ratio, 'ratio');
            document.getElementById('sortinoRatio').className = 'val ' + (analytics.sortino_ratio > 1 ? 'positive' : '');
            
            document.getElementById('profitFactor').textContent = formatValue(analytics.profit_factor, 'ratio');
            document.getElementById('profitFactor').className = 'val ' + (analytics.profit_factor > 1 ? 'positive' : 'negative');
            
            document.getElementById('expectancy').textContent = formatValue(analytics.expectancy, 'money');
            document.getElementById('expectancy').className = 'val ' + (analytics.expectancy > 0 ? 'positive' : 'negative');
            
            const dd = analytics.max_drawdown?.max_drawdown_pct || 0;
            document.getElementById('maxDrawdown').textContent = dd.toFixed(1) + '%';
            
            const h = analytics.hedge_effectiveness || {};
            document.getElementById('hedgeStats').innerHTML = `
                <div style="display: grid; gap: 10px;">
                    <div style="display: flex; justify-content: space-between;">
                        <span>Hedged Trades:</span>
                        <span>${h.hedged?.count || 0} (${(h.hedged?.win_rate || 0).toFixed(0)}% win)</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Hedged Avg P&L:</span>
                        <span class="${(h.hedged?.avg_pnl || 0) >= 0 ? 'positive' : 'negative'}">$${(h.hedged?.avg_pnl || 0).toFixed(2)}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Unhedged Trades:</span>
                        <span>${h.unhedged?.count || 0} (${(h.unhedged?.win_rate || 0).toFixed(0)}% win)</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Unhedged Avg P&L:</span>
                        <span class="${(h.unhedged?.avg_pnl || 0) >= 0 ? 'positive' : 'negative'}">$${(h.unhedged?.avg_pnl || 0).toFixed(2)}</span>
                    </div>
                </div>
            `;
            
            const s = analytics.streak_analysis || {};
            document.getElementById('streakStats').innerHTML = `
                <div style="display: grid; gap: 10px;">
                    <div style="display: flex; justify-content: space-between;">
                        <span>Max Win Streak:</span>
                        <span class="positive">🔥 ${s.max_win_streak || 0}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Max Loss Streak:</span>
                        <span class="negative">💀 ${s.max_loss_streak || 0}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Current Win Streak:</span>
                        <span>${s.current_win_streak || 0}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Current Loss Streak:</span>
                        <span>${s.current_loss_streak || 0}</span>
                    </div>
                </div>
            `;
        }
        
        function updateHeatmap(analytics) {
            const hourly = analytics.hourly_performance || {};
            let heatmapHtml = '';
            for (let h = 0; h < 24; h++) {
                const hour = String(h).padStart(2, '0');
                const data = hourly[hour] || { trades: 0, win_rate: 0, pnl: 0 };
                const color = getHeatmapColor(data.win_rate, data.trades);
                heatmapHtml += `
                    <div class="heatmap-cell" 
                         style="background: ${color}"
                         title="${hour}:00 UTC&#10;${data.trades} trades&#10;${data.win_rate.toFixed(0)}% win rate&#10;$${data.pnl.toFixed(2)} P&L">
                        ${hour}
                    </div>
                `;
            }
            document.getElementById('hourlyHeatmap').innerHTML = heatmapHtml;
            
            const daily = analytics.daily_performance || {};
            const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
            let dailyHtml = '<div style="display: grid; gap: 10px;">';
            for (const day of days) {
                const data = daily[day] || { trades: 0, win_rate: 0, pnl: 0 };
                const pnlClass = data.pnl >= 0 ? 'positive' : 'negative';
                const wrClass = data.win_rate >= 50 ? 'positive' : data.win_rate >= 40 ? 'neutral' : 'negative';
                dailyHtml += `
                    <div style="display: flex; justify-content: space-between; padding: 8px; background: rgba(255,255,255,0.05); border-radius: 8px;">
                        <span>${day}</span>
                        <span>${data.trades} trades</span>
                        <span class="${wrClass}">${data.win_rate.toFixed(0)}%</span>
                        <span class="${pnlClass}">$${data.pnl.toFixed(2)}</span>
                    </div>
                `;
            }
            dailyHtml += '</div>';
            document.getElementById('dailyStats').innerHTML = dailyHtml;
            
            let hourlyPerfHtml = '';
            for (let h = 0; h < 24; h++) {
                const hour = String(h).padStart(2, '0');
                const data = hourly[hour] || { trades: 0, win_rate: 0 };
                const bgColor = getHeatmapColor(data.win_rate, data.trades);
                const textColor = data.trades >= 3 ? '#1a1a2e' : '#888';
                hourlyPerfHtml += `
                    <div class="hour-cell" style="background: ${bgColor}; color: ${textColor};">
                        <div class="hour">${hour}:00</div>
                        <div class="wr">${data.trades >= 3 ? data.win_rate.toFixed(0) + '%' : '-'}</div>
                    </div>
                `;
            }
            document.getElementById('hourlyPerformance').innerHTML = hourlyPerfHtml;
        }
        
        function updateTuning(recommendations) {
            if (!recommendations || recommendations.length === 0) {
                document.getElementById('recommendations').innerHTML = `
                    <div style="text-align: center; padding: 40px; color: #888;">
                        <div style="font-size: 3em; margin-bottom: 15px;">✅</div>
                        <div>No recommendations at this time.</div>
                        <div style="font-size: 0.9em; margin-top: 10px;">Need more trading data for analysis (min 20-50 trades).</div>
                    </div>
                `;
                return;
            }
            
            let html = '';
            for (const rec of recommendations) {
                const confClass = rec.confidence >= 0.7 ? '' : 'medium';
                html += `
                    <div class="recommendation ${confClass}">
                        <div class="rec-header">
                            <span class="rec-param">${rec.parameter}</span>
                            <span class="rec-confidence">${(rec.confidence * 100).toFixed(0)}% confidence</span>
                        </div>
                        <div class="rec-values">
                            ${rec.current_value} → <strong>${rec.recommended_value}</strong>
                        </div>
                        <div class="rec-reason">${rec.reason}</div>
                        <div class="rec-expected">Expected: ${rec.expected_improvement}</div>
                    </div>
                `;
            }
            document.getElementById('recommendations').innerHTML = html;
        }
        
        function updateHistory(stats) {
            const settlements = stats.recent_settlements || [];
            document.getElementById('settlementsTable').innerHTML = settlements.map(s => {
                const time = new Date(s.timestamp).toLocaleTimeString();
                const pnlClass = s.pnl >= 0 ? 'positive' : 'negative';
                const winnerBadge = s.winner === 'YES' ? 'badge-yes' : 'badge-no';
                return `
                    <tr>
                        <td>${time}</td>
                        <td>${s.symbol}</td>
                        <td><span class="badge ${winnerBadge}">${s.winner}</span></td>
                        <td class="${pnlClass}">$${s.pnl.toFixed(2)}</td>
                    </tr>
                `;
            }).join('');
            
            const trades = stats.recent_trades || [];
            document.getElementById('tradesTable').innerHTML = trades.map(t => {
                const time = new Date(t.timestamp).toLocaleTimeString();
                const sideBadge = t.is_hedge ? 'badge-hedge' : (t.side === 'YES' ? 'badge-yes' : 'badge-no');
                const sideText = t.is_hedge ? 'HEDGE' : t.side;
                return `
                    <tr>
                        <td>${time}</td>
                        <td>${t.symbol}</td>
                        <td><span class="badge ${sideBadge}">${sideText}</span></td>
                        <td>$${t.cost.toFixed(2)}</td>
                    </tr>
                `;
            }).join('');
        }
        
        function updateCharts(stats, analytics) {
            const hourlyData = stats.hourly_pnl || [];
            if (hourlyData.length > 0) {
                const labels = hourlyData.map(h => h.hour.split(' ')[1] || h.hour);
                const values = hourlyData.map(h => h.pnl);
                
                let cumulative = 0;
                const cumulativeValues = values.map(v => {
                    cumulative += v;
                    return cumulative;
                });
                
                const ctx = document.getElementById('pnlChart').getContext('2d');
                if (pnlChart) {
                    pnlChart.data.labels = labels;
                    pnlChart.data.datasets[0].data = values;
                    pnlChart.data.datasets[0].backgroundColor = values.map(v => v >= 0 ? 'rgba(0,210,106,0.6)' : 'rgba(255,107,107,0.6)');
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
                            plugins: { legend: { labels: { color: '#888' } } },
                            scales: {
                                x: { ticks: { color: '#888' }, grid: { color: 'rgba(255,255,255,0.1)' } },
                                y: { 
                                    ticks: { color: '#888', callback: v => '$' + v.toFixed(0) },
                                    grid: { color: 'rgba(255,255,255,0.1)' }
                                }
                            }
                        }
                    });
                }
            }
            
            const equity = analytics.equity_curve || [];
            if (equity.length > 0) {
                const ctx = document.getElementById('equityChart').getContext('2d');
                if (equityChart) {
                    equityChart.data.labels = equity.map((e, i) => i + 1);
                    equityChart.data.datasets[0].data = equity.map(e => e.cumulative_pnl);
                    equityChart.update();
                } else {
                    equityChart = new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: equity.map((e, i) => i + 1),
                            datasets: [{
                                label: 'Equity',
                                data: equity.map(e => e.cumulative_pnl),
                                borderColor: '#00d26a',
                                backgroundColor: 'rgba(0,210,106,0.1)',
                                fill: true,
                                tension: 0.3,
                                pointRadius: 2,
                            }]
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: true,
                            plugins: { legend: { display: false } },
                            scales: {
                                x: { 
                                    title: { display: true, text: 'Trade #', color: '#666' },
                                    ticks: { color: '#888' },
                                    grid: { color: 'rgba(255,255,255,0.1)' }
                                },
                                y: { 
                                    title: { display: true, text: 'P&L ($)', color: '#666' },
                                    ticks: { color: '#888' },
                                    grid: { color: 'rgba(255,255,255,0.1)' }
                                }
                            }
                        }
                    });
                }
            }
        }
        
        fetchData();
        setInterval(fetchData, 10000);
    </script>
</body>
</html>
'''

# ========================== ANALYTICS ENGINE ==========================

class AnalyticsEngine:
    """Calculate advanced performance metrics"""
    
    def __init__(self, db_path: str = "polyneko.db"):
        self.db_path = db_path
    
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def get_all_settlements(self) -> list:
        try:
            conn = self._get_connection()
            cursor = conn.execute('SELECT * FROM settlements ORDER BY timestamp ASC')
            results = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return results
        except Exception:
            return []
    
    def get_equity_curve(self) -> list:
        settlements = self.get_all_settlements()
        equity_curve = []
        cumulative_pnl = 0.0
        
        for s in settlements:
            cumulative_pnl += s['pnl']
            equity_curve.append({
                'timestamp': s['timestamp'],
                'pnl': s['pnl'],
                'cumulative_pnl': cumulative_pnl,
                'symbol': s['symbol'],
            })
        
        return equity_curve
    
    def get_returns(self) -> list:
        settlements = self.get_all_settlements()
        returns = []
        for s in settlements:
            cost = s.get('yes_cost', 0) + s.get('no_cost', 0)
            if cost > 0:
                returns.append(s['pnl'] / cost)
        return returns
    
    def calculate_sharpe_ratio(self, risk_free_rate: float = 0.0) -> float:
        returns = self.get_returns()
        if len(returns) < 2:
            return 0.0
        
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return 0.0
        
        sharpe = (mean_return - risk_free_rate) / std_dev
        return sharpe * math.sqrt(96 * 365)
    
    def calculate_sortino_ratio(self, risk_free_rate: float = 0.0) -> float:
        returns = self.get_returns()
        if len(returns) < 2:
            return 0.0
        
        mean_return = sum(returns) / len(returns)
        negative_returns = [r for r in returns if r < 0]
        
        if not negative_returns:
            return 999999
        
        downside_variance = sum(r ** 2 for r in negative_returns) / len(returns)
        downside_deviation = math.sqrt(downside_variance)
        
        if downside_deviation == 0:
            return 999999
        
        sortino = (mean_return - risk_free_rate) / downside_deviation
        return sortino * math.sqrt(96 * 365)
    
    def calculate_max_drawdown(self) -> dict:
        equity_curve = self.get_equity_curve()
        
        if not equity_curve:
            return {'max_drawdown': 0, 'max_drawdown_pct': 0, 'peak': 0, 'trough': 0, 'duration_trades': 0}
        
        peak = equity_curve[0]['cumulative_pnl']
        max_drawdown = 0
        max_drawdown_peak = 0
        max_drawdown_trough = 0
        max_drawdown_duration = 0
        current_drawdown_start = None
        
        for i, point in enumerate(equity_curve):
            pnl = point['cumulative_pnl']
            
            if pnl > peak:
                peak = pnl
                current_drawdown_start = None
            
            drawdown = peak - pnl
            
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                max_drawdown_peak = peak
                max_drawdown_trough = pnl
                if current_drawdown_start is None:
                    current_drawdown_start = i
                max_drawdown_duration = i - current_drawdown_start
        
        drawdown_pct = (max_drawdown / max_drawdown_peak * 100) if max_drawdown_peak > 0 else 0
        
        return {
            'max_drawdown': max_drawdown,
            'max_drawdown_pct': drawdown_pct,
            'peak': max_drawdown_peak,
            'trough': max_drawdown_trough,
            'duration_trades': max_drawdown_duration,
        }
    
    def calculate_win_rate(self) -> dict:
        settlements = self.get_all_settlements()
        
        if not settlements:
            return {'overall': 0, 'total_trades': 0, 'wins': 0, 'losses': 0, 'by_symbol': {}}
        
        wins = sum(1 for s in settlements if s['pnl'] > 0)
        losses = sum(1 for s in settlements if s['pnl'] < 0)
        total = wins + losses
        
        overall = (wins / total * 100) if total > 0 else 0
        
        by_symbol = {}
        symbols = set(s['symbol'] for s in settlements)
        
        for sym in symbols:
            sym_trades = [s for s in settlements if s['symbol'] == sym]
            sym_wins = sum(1 for s in sym_trades if s['pnl'] > 0)
            sym_total = len(sym_trades)
            by_symbol[sym] = {
                'win_rate': (sym_wins / sym_total * 100) if sym_total > 0 else 0,
                'trades': sym_total,
                'wins': sym_wins,
                'total_pnl': sum(s['pnl'] for s in sym_trades),
            }
        
        return {
            'overall': overall,
            'total_trades': total,
            'wins': wins,
            'losses': losses,
            'by_symbol': by_symbol,
        }
    
    def get_performance_by_hour(self) -> dict:
        settlements = self.get_all_settlements()
        hourly = {str(h).zfill(2): {'trades': 0, 'wins': 0, 'pnl': 0, 'win_rate': 0} for h in range(24)}
        
        for s in settlements:
            try:
                ts = datetime.fromisoformat(s['timestamp'].replace('Z', '+00:00'))
                hour = ts.strftime('%H')
                hourly[hour]['trades'] += 1
                hourly[hour]['pnl'] += s['pnl']
                if s['pnl'] > 0:
                    hourly[hour]['wins'] += 1
            except:
                pass
        
        for hour, data in hourly.items():
            if data['trades'] > 0:
                data['win_rate'] = data['wins'] / data['trades'] * 100
        
        return hourly
    
    def get_performance_by_day(self) -> dict:
        settlements = self.get_all_settlements()
        days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        daily = {d: {'trades': 0, 'wins': 0, 'pnl': 0, 'win_rate': 0} for d in days}
        
        for s in settlements:
            try:
                ts = datetime.fromisoformat(s['timestamp'].replace('Z', '+00:00'))
                day = days[ts.weekday()]
                daily[day]['trades'] += 1
                daily[day]['pnl'] += s['pnl']
                if s['pnl'] > 0:
                    daily[day]['wins'] += 1
            except:
                pass
        
        for day, data in daily.items():
            if data['trades'] > 0:
                data['win_rate'] = data['wins'] / data['trades'] * 100
        
        return daily
    
    def get_hedge_effectiveness(self) -> dict:
        settlements = self.get_all_settlements()
        
        hedged = [s for s in settlements if s.get('hedge_count', 0) > 0]
        unhedged = [s for s in settlements if s.get('hedge_count', 0) == 0]
        
        def calc_stats(trades):
            if not trades:
                return {'count': 0, 'win_rate': 0, 'avg_pnl': 0, 'total_pnl': 0}
            wins = sum(1 for t in trades if t['pnl'] > 0)
            return {
                'count': len(trades),
                'win_rate': wins / len(trades) * 100,
                'avg_pnl': sum(t['pnl'] for t in trades) / len(trades),
                'total_pnl': sum(t['pnl'] for t in trades),
            }
        
        return {'hedged': calc_stats(hedged), 'unhedged': calc_stats(unhedged)}
    
    def get_streak_analysis(self) -> dict:
        settlements = self.get_all_settlements()
        
        if not settlements:
            return {'max_win_streak': 0, 'max_loss_streak': 0, 'current_win_streak': 0, 'current_loss_streak': 0}
        
        max_win = max_loss = current_win = current_loss = 0
        
        for s in settlements:
            if s['pnl'] > 0:
                current_win += 1
                current_loss = 0
                max_win = max(max_win, current_win)
            elif s['pnl'] < 0:
                current_loss += 1
                current_win = 0
                max_loss = max(max_loss, current_loss)
            else:
                current_win = current_loss = 0
        
        return {
            'max_win_streak': max_win,
            'max_loss_streak': max_loss,
            'current_win_streak': current_win,
            'current_loss_streak': current_loss,
        }
    
    def get_profit_factor(self) -> float:
        settlements = self.get_all_settlements()
        gross_profit = sum(s['pnl'] for s in settlements if s['pnl'] > 0)
        gross_loss = abs(sum(s['pnl'] for s in settlements if s['pnl'] < 0))
        
        if gross_loss == 0:
            return 999999 if gross_profit > 0 else 0
        return gross_profit / gross_loss
    
    def get_expectancy(self) -> float:
        settlements = self.get_all_settlements()
        if not settlements:
            return 0
        
        wins = [s['pnl'] for s in settlements if s['pnl'] > 0]
        losses = [abs(s['pnl']) for s in settlements if s['pnl'] < 0]
        total = len(settlements)
        
        win_rate = len(wins) / total if total > 0 else 0
        loss_rate = len(losses) / total if total > 0 else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        return (win_rate * avg_win) - (loss_rate * avg_loss)
    
    def get_full_analytics(self) -> dict:
        def safe_round(val, decimals=2):
            if val == float('inf') or val >= 999999:
                return 999999
            if val == float('-inf') or val <= -999999:
                return -999999
            if math.isnan(val):
                return 0
            return round(val, decimals)
        
        return {
            'sharpe_ratio': safe_round(self.calculate_sharpe_ratio()),
            'sortino_ratio': safe_round(self.calculate_sortino_ratio()),
            'max_drawdown': self.calculate_max_drawdown(),
            'win_rate': self.calculate_win_rate(),
            'hourly_performance': self.get_performance_by_hour(),
            'daily_performance': self.get_performance_by_day(),
            'hedge_effectiveness': self.get_hedge_effectiveness(),
            'streak_analysis': self.get_streak_analysis(),
            'profit_factor': safe_round(self.get_profit_factor()),
            'expectancy': safe_round(self.get_expectancy()),
            'equity_curve': self.get_equity_curve()[-100:],
        }


# ========================== AUTO-PARAMETER TUNING ==========================

@dataclass
class TuningRecommendation:
    parameter: str
    current_value: float
    recommended_value: float
    reason: str
    confidence: float
    expected_improvement: str


class AutoTuner:
    def __init__(self, db_path: str = "polyneko.db"):
        self.db_path = db_path
        self.analytics = AnalyticsEngine(db_path)
        self.recommendations = []
    
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def analyze_confidence_threshold(self, current_min_confidence: float = 0.4) -> Optional[TuningRecommendation]:
        try:
            conn = self._get_connection()
            cursor = conn.execute('''
                SELECT t.reason, s.pnl 
                FROM trades t
                JOIN settlements s ON t.slot = s.slot AND t.symbol = s.symbol
                WHERE t.is_hedge = 0 AND t.reason LIKE '%conf=%'
            ''')
            trades = cursor.fetchall()
            conn.close()
        except:
            return None
        
        if len(trades) < 20:
            return None
        
        conf_results = []
        for t in trades:
            try:
                reason = t['reason']
                if 'conf=' in reason:
                    conf_str = reason.split('conf=')[1].split('%')[0].split(',')[0]
                    conf = float(conf_str) / 100
                    conf_results.append({'confidence': conf, 'pnl': t['pnl']})
            except:
                pass
        
        if len(conf_results) < 20:
            return None
        
        buckets = {
            '30-40%': {'wins': 0, 'total': 0},
            '40-50%': {'wins': 0, 'total': 0},
            '50-60%': {'wins': 0, 'total': 0},
            '60-70%': {'wins': 0, 'total': 0},
            '70%+': {'wins': 0, 'total': 0},
        }
        
        for cr in conf_results:
            c = cr['confidence']
            won = cr['pnl'] > 0
            
            if c < 0.4:
                bucket = '30-40%'
            elif c < 0.5:
                bucket = '40-50%'
            elif c < 0.6:
                bucket = '50-60%'
            elif c < 0.7:
                bucket = '60-70%'
            else:
                bucket = '70%+'
            
            buckets[bucket]['total'] += 1
            if won:
                buckets[bucket]['wins'] += 1
        
        best_threshold = current_min_confidence
        best_improvement = 0
        
        for threshold, bucket in [(0.5, '40-50%'), (0.6, '50-60%')]:
            if buckets[bucket]['total'] >= 5:
                bucket_win_rate = buckets[bucket]['wins'] / buckets[bucket]['total']
                if bucket_win_rate < 0.45:
                    if threshold > best_threshold:
                        best_threshold = threshold
                        best_improvement = (0.50 - bucket_win_rate) * 100
        
        if best_threshold != current_min_confidence:
            return TuningRecommendation(
                parameter='MIN_CONFIDENCE',
                current_value=current_min_confidence,
                recommended_value=best_threshold,
                reason=f"Low win rate in confidence bucket",
                confidence=0.7,
                expected_improvement=f"+{best_improvement:.0f}% win rate improvement"
            )
        
        return None
    
    def analyze_bet_sizing(self, current_bet_size: float = 25.0) -> Optional[TuningRecommendation]:
        win_data = self.analytics.calculate_win_rate()
        settlements = self.analytics.get_all_settlements()
        
        if win_data['total_trades'] < 50:
            return None
        
        wins = [s['pnl'] for s in settlements if s['pnl'] > 0]
        losses = [abs(s['pnl']) for s in settlements if s['pnl'] < 0]
        
        if not wins or not losses:
            return None
        
        win_rate = win_data['wins'] / win_data['total_trades']
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        
        if avg_loss == 0:
            return None
        
        win_loss_ratio = avg_win / avg_loss
        kelly = win_rate - ((1 - win_rate) / win_loss_ratio)
        fractional_kelly = kelly * 0.5
        
        if fractional_kelly <= 0:
            return TuningRecommendation(
                parameter='BET_SIZE',
                current_value=current_bet_size,
                recommended_value=current_bet_size * 0.5,
                reason=f"Negative Kelly ({kelly:.2%}) - reduce exposure",
                confidence=0.9,
                expected_improvement="Reduced risk of ruin"
            )
        
        recommended_bet = 1000 * fractional_kelly
        
        if abs(recommended_bet - current_bet_size) > current_bet_size * 0.3:
            return TuningRecommendation(
                parameter='BET_SIZE',
                current_value=current_bet_size,
                recommended_value=round(recommended_bet, 0),
                reason=f"Kelly suggests ${recommended_bet:.0f} (using 50% Kelly)",
                confidence=0.7,
                expected_improvement="Mathematically optimal sizing"
            )
        
        return None
    
    def analyze_hour_filter(self, current_min_winrate: float = 40.0) -> list:
        hourly = self.analytics.get_performance_by_hour()
        recommendations = []
        
        bad_hours = []
        for hour, data in hourly.items():
            if data['trades'] >= 10 and data['win_rate'] < 35:
                bad_hours.append(hour)
        
        if bad_hours:
            recommendations.append(TuningRecommendation(
                parameter='SKIP_HOURS',
                current_value=0,
                recommended_value=len(bad_hours),
                reason=f"Hours {', '.join(bad_hours)} have <35% win rate",
                confidence=0.8,
                expected_improvement=f"Skip {len(bad_hours)} unprofitable hours"
            ))
        
        return recommendations
    
    def run_full_analysis(self, current_config: dict) -> list:
        self.recommendations = []
        
        rec = self.analyze_confidence_threshold(current_config.get('min_confidence', 0.4))
        if rec:
            self.recommendations.append(rec)
        
        recs = self.analyze_hour_filter(current_config.get('min_hour_winrate', 40))
        self.recommendations.extend(recs)
        
        rec = self.analyze_bet_sizing(current_config.get('bet_size', 25))
        if rec:
            self.recommendations.append(rec)
        
        self.recommendations.sort(key=lambda r: r.confidence, reverse=True)
        return self.recommendations
    
    def get_recommendations_dict(self) -> list:
        return [asdict(r) for r in self.recommendations]


# ========================== DASHBOARD SERVER ==========================

class Dashboard:
    def __init__(self, db_path: str = "polyneko.db", port: int = 8080):
        self.db_path = db_path
        self.port = port
        self.app = web.Application()
        self.runner = None
        self.analytics = AnalyticsEngine(db_path)
        self.tuner = AutoTuner(db_path)
        self.current_config = {}
        self._setup_routes()
    
    def set_config(self, config: dict):
        self.current_config = config
    
    def _setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/api/stats', self.handle_stats)
        self.app.router.add_get('/api/analytics', self.handle_analytics)
        self.app.router.add_get('/api/tuning', self.handle_tuning)
    
    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    async def handle_index(self, request):
        return web.Response(text=HTML_TEMPLATE, content_type='text/html')
    
    async def handle_stats(self, request):
        try:
            conn = self._get_db()
            stats = {}
            
            cursor = conn.execute('SELECT COUNT(*) as cnt FROM trades')
            stats['total_trades'] = cursor.fetchone()['cnt']
            
            cursor = conn.execute('SELECT COUNT(*) as cnt FROM settlements')
            stats['total_settlements'] = cursor.fetchone()['cnt']
            
            cursor = conn.execute('SELECT COALESCE(SUM(pnl), 0) as total FROM settlements')
            stats['total_pnl'] = cursor.fetchone()['total']
            
            cursor = conn.execute('SELECT COUNT(*) as cnt FROM settlements WHERE pnl > 0')
            wins = cursor.fetchone()['cnt']
            stats['wins'] = wins
            stats['win_rate'] = (wins / stats['total_settlements'] * 100) if stats['total_settlements'] > 0 else 0
            
            cursor = conn.execute('SELECT COALESCE(SUM(cost), 0) as total FROM trades')
            stats['total_cost'] = cursor.fetchone()['total']
            
            stats['roi'] = (stats['total_pnl'] / stats['total_cost'] * 100) if stats['total_cost'] > 0 else 0
            
            cursor = conn.execute('''
                SELECT symbol, COUNT(*) as trades, COALESCE(SUM(pnl), 0) as pnl, 
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
                FROM settlements GROUP BY symbol
            ''')
            stats['by_symbol'] = [dict(row) for row in cursor.fetchall()]
            
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            cursor = conn.execute('SELECT COUNT(*) as cnt, COALESCE(SUM(pnl), 0) as pnl FROM settlements WHERE timestamp LIKE ?', (f'{today}%',))
            row = cursor.fetchone()
            stats['today_settlements'] = row['cnt']
            stats['today_pnl'] = row['pnl']
            
            cursor = conn.execute('''
                SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour, COALESCE(SUM(pnl), 0) as pnl
                FROM settlements WHERE timestamp > datetime('now', '-24 hours')
                GROUP BY hour ORDER BY hour
            ''')
            stats['hourly_pnl'] = [dict(row) for row in cursor.fetchall()]
            
            cursor = conn.execute('SELECT * FROM settlements ORDER BY timestamp DESC LIMIT 20')
            stats['recent_settlements'] = [dict(row) for row in cursor.fetchall()]
            
            cursor = conn.execute('SELECT * FROM trades ORDER BY timestamp DESC LIMIT 20')
            stats['recent_trades'] = [dict(row) for row in cursor.fetchall()]
            
            conn.close()
            return web.json_response(stats)
        except Exception as e:
            return web.json_response({'error': str(e), 'total_settlements': 0, 'total_pnl': 0, 'win_rate': 0, 'wins': 0, 'total_cost': 0, 'roi': 0, 'by_symbol': [], 'today_settlements': 0, 'today_pnl': 0, 'hourly_pnl': [], 'recent_settlements': [], 'recent_trades': []})
    
    async def handle_analytics(self, request):
        try:
            data = self.analytics.get_full_analytics()
            return web.json_response(data)
        except Exception as e:
            return web.json_response({'error': str(e)})
    
    async def handle_tuning(self, request):
        try:
            self.tuner.run_full_analysis(self.current_config)
            return web.json_response(self.tuner.get_recommendations_dict())
        except Exception as e:
            return web.json_response([])
    
    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await site.start()
        logger.info(f"🌐 Dashboard running at http://localhost:{self.port}")
    
    async def stop(self):
        if self.runner:
            await self.runner.cleanup()


# ========================== TUNING INTEGRATION ==========================

class TuningIntegration:
    def __init__(self, db_path: str = "polyneko.db", interval: int = 21600):
        self.tuner = AutoTuner(db_path)
        self.last_tuning_check = 0
        self.tuning_interval = interval
    
    def should_check_tuning(self) -> bool:
        import time
        now = time.time()
        if now - self.last_tuning_check >= self.tuning_interval:
            self.last_tuning_check = now
            return True
        return False
    
    def get_recommendations(self, current_config: dict) -> list:
        return self.tuner.run_full_analysis(current_config)
    
    def log_recommendations(self, recommendations, logger):
        if not recommendations:
            logger.info("🔧 Auto-Tuning: No recommendations at this time")
            return
        
        logger.info("=" * 60)
        logger.info("🔧 AUTO-TUNING RECOMMENDATIONS")
        logger.info("=" * 60)
        
        for rec in recommendations:
            conf_emoji = "🟢" if rec.confidence >= 0.7 else "🟡"
            logger.info(f"  {conf_emoji} {rec.parameter}: {rec.current_value} → {rec.recommended_value}")
            logger.info(f"     Reason: {rec.reason}")
            logger.info(f"     Expected: {rec.expected_improvement}")
        
        logger.info("=" * 60)


# ========================== STANDALONE MODE ==========================

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S'
    )
    
    db_path = os.getenv('DB_PATH', 'polyneko.db')
    port = int(os.getenv('DASHBOARD_PORT', '8080'))
    
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
