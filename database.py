import sqlite3
import threading
from trading_utils import logger, log_message
from globals import (current_positions, current_config, pending_orders, closed_positions, 
                     all_closed_positions, data_lock, shutdown_event, CONFIG)

def get_db_connection(db_file="trading_data.db"):
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_database(db_file="trading_data.db"):
    with get_db_connection(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS Config (
            user_id TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            api_secret TEXT NOT NULL,
            status INTEGER DEFAULT 1,
            available_fund REAL DEFAULT 0.0,
            live_pnl REAL DEFAULT 0.0,
            multiplier REAL DEFAULT 1.0,
            leverage INTEGER DEFAULT 1
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS Orders (
            order_id TEXT PRIMARY KEY,
            user_id TEXT,
            symbol TEXT,
            side TEXT,
            order_type TEXT,
            price REAL,
            quantity REAL,
            status TEXT,
            time TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS ClosedPositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            symbol TEXT,
            position_amount REAL,
            entry_price REAL,
            exit_price REAL,
            realized_pnl REAL,
            close_time TEXT
        )''')
        conn.commit()
        log_message('INFO', "Database initialized successfully")

def db_updater(db_file="trading_data.db"):
    while not shutdown_event.is_set():
        try:
            with get_db_connection(db_file) as conn:
                cursor = conn.cursor()
                with data_lock:
                    for user_id, config in current_config.items():
                        cursor.execute('''INSERT OR REPLACE INTO Config 
                            (user_id, api_key, api_secret, status, available_fund, live_pnl, multiplier, leverage)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                            (user_id, config['api_key'], config['api_secret'], config['status'],
                             config['available_fund'], config['live_pnl'], config.get('multiplier', 1.0),
                             config.get('leverage', 1)))
                    for order in pending_orders:
                        cursor.execute('''INSERT OR REPLACE INTO Orders 
                            (order_id, user_id, symbol, side, order_type, price, quantity, status, time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (order['order_id'], order['user_id'], order['symbol'], order['side'],
                             order['order_type'], order['price'], order['quantity'], order['status'], order['time']))
                    for pos in closed_positions:
                        cursor.execute('''INSERT INTO ClosedPositions 
                            (user_id, symbol, position_amount, entry_price, exit_price, realized_pnl, close_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?)''',
                            (pos['user_id'], pos['symbol'], pos['position_amount'], pos['entry_price'],
                             pos['exit_price'], pos['realized_pnl'], pos['close_time']))
                    closed_positions.clear()
                conn.commit()
                log_message('INFO', "Database updated successfully")
        except Exception as e:
            log_message('ERROR', f"Error updating database: {e}")
        threading.Event().wait(CONFIG["db_update_interval"])