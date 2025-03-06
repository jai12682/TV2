import sqlite3
import threading
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from flask import Flask, request, jsonify, render_template, flash, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
from binance.client import Client
from binance.exceptions import BinanceAPIException
from concurrent_log_handler import ConcurrentRotatingFileHandler
import logging
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import requests.exceptions
from flask_caching import Cache

# Set up logging
logger = logging.getLogger('trading_app')
logger.setLevel(logging.INFO)
handler = ConcurrentRotatingFileHandler('trading_data.log', maxBytes=10*1024*1024, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(handler)
logger.addHandler(stream_handler)

log_lock = threading.Lock()

def log_message(level, message):
    with log_lock:
        if level == 'INFO':
            logger.info(message)
        elif level == 'ERROR':
            logger.error(message)

# Utility function to round quantity to the correct step size and precision
def round_quantity(quantity, step_size, precision):
    quantity_decimal = Decimal(str(quantity))
    step_size_decimal = Decimal(str(step_size))
    rounded_quantity = (quantity_decimal // step_size_decimal) * step_size_decimal
    return float(rounded_quantity.quantize(Decimal(f'0.{"0" * precision}'), rounding=ROUND_DOWN))

# Global variables
slave_accounts = []
current_positions = {}
current_config = {}
pending_orders = []
closed_positions = []
all_closed_positions = []
data_lock = threading.Lock()
shutdown_event = threading.Event()
thread_status = {
    "db_updater": True,
    "balance_updater": True,
    "sync_slave_positions": True
}
CONFIG = {
    "db_update_interval": 1,
    "balance_update_interval": 5,
    "slave_sync_interval": 10,
    "state_save_interval": 300,
    "excel_export_interval": 300,
    "max_retries": 3,
    "max_backoff": 30,
    "max_api_weight": 1200,
    "weight_reset_interval": 60
}

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})

class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

@login_manager.user_loader
def load_user(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, password_hash FROM Users WHERE id = ?", (user_id,))
        user_data = cursor.fetchone()
        if user_data:
            return User(user_data[0], user_data[1], user_data[2])
    return None

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
            size_usdt REAL,
            status TEXT,
            time TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS ClosedPositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            symbol TEXT,
            quantity REAL,
            size_usdt REAL,
            entry_price REAL,
            exit_price REAL,
            realized_pnl REAL,
            close_time TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT
        )''')
        conn.commit()
        # Validate schema
        cursor.execute("PRAGMA table_info(Orders)")
        columns = [col[1] for col in cursor.fetchall()]
        required_columns = ['order_id', 'user_id', 'symbol', 'side', 'order_type', 'price', 'quantity', 'size_usdt', 'status', 'time']
        missing_columns = [col for col in required_columns if col not in columns]
        if missing_columns:
            log_message('ERROR', f"Orders table is missing required columns: {missing_columns}. Please migrate the database schema.")
        cursor.execute("PRAGMA table_info(ClosedPositions)")
        columns = [col[1] for col in cursor.fetchall()]
        required_columns = ['id', 'user_id', 'symbol', 'quantity', 'size_usdt', 'entry_price', 'exit_price', 'realized_pnl', 'close_time']
        missing_columns = [col for col in required_columns if col not in columns]
        if missing_columns:
            log_message('ERROR', f"ClosedPositions table is missing required columns: {missing_columns}. Please migrate the database schema.")
        log_message('INFO', "Database initialized successfully")

def db_updater(db_file="trading_data.db"):
    update_count = 0
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
                            (order_id, user_id, symbol, side, order_type, price, quantity, size_usdt, status, time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (order['order_id'], order['user_id'], order['symbol'], order['side'],
                             order['order_type'], order['price'], order['quantity'], order['size_usdt'], order['status'], order['time']))
                    for pos in closed_positions:
                        cursor.execute('''INSERT INTO ClosedPositions 
                            (user_id, symbol, quantity, size_usdt, entry_price, exit_price, realized_pnl, close_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                            (pos['user_id'], pos['symbol'], pos['quantity'], pos['size_usdt'], pos['entry_price'],
                             pos['exit_price'], pos['realized_pnl'], pos['close_time']))
                    closed_positions.clear()
                conn.commit()
                update_count += 1
                if update_count % 300 == 0:
                    log_message('INFO', "Database updated successfully")
        except Exception as e:
            log_message('ERROR', f"Error updating database: {e}")
        threading.Event().wait(CONFIG["db_update_interval"])

@retry(
    stop=stop_after_attempt(CONFIG["max_retries"]),
    wait=wait_exponential(multiplier=1, min=1, max=CONFIG["max_backoff"]),
    retry=retry_if_exception_type((requests.exceptions.RequestException, BinanceAPIException))
)
def sync_closed_positions():
    failed_users = []
    with data_lock:
        for user_id, config in current_config.items():
            try:
                client = Client(config['api_key'], config['api_secret'], requests_params={"timeout": 20})
                trades = client.futures_account_trades()
                current_positions_info = client.futures_position_information()
                position_dict = {pos['symbol']: float(pos['positionAmt']) for pos in current_positions_info}
                
                for trade in trades:
                    symbol = trade['symbol']
                    side = trade['side']
                    quantity = float(trade['qty'])
                    price = float(trade['price'])
                    realized_pnl = float(trade['realizedPnl'])
                    trade_time = datetime.fromtimestamp(trade['time'] / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    
                    matching_order = next((order for order in pending_orders if order['user_id'] == user_id and order['symbol'] == symbol and order['side'] != side and order['status'] == 'FILLED'), None)
                    if matching_order:
                        current_position_amt = position_dict.get(symbol, 0.0)
                        if current_position_amt == 0 and realized_pnl != 0:
                            entry_price = matching_order['price']
                            if entry_price is None or entry_price == 0:
                                entry_price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                                log_message('INFO', f"Fetched current price {entry_price} for {symbol} as entry price was invalid")
                            size_usdt = quantity * entry_price
                            with get_db_connection() as conn:
                                cursor = conn.cursor()
                                cursor.execute("UPDATE Orders SET quantity = ?, size_usdt = ?, price = ? WHERE order_id = ?",
                                               (quantity, size_usdt, entry_price, matching_order['order_id']))
                                conn.commit()
                                log_message('INFO', f"Updated order {matching_order['order_id']} with quantity {quantity}, size_usdt {size_usdt}, price {entry_price}")
                            closed_positions.append({
                                "user_id": user_id,
                                "symbol": symbol,
                                "quantity": quantity,
                                "size_usdt": size_usdt,
                                "entry_price": entry_price,
                                "exit_price": price,
                                "realized_pnl": realized_pnl,
                                "close_time": trade_time
                            })
                            pending_orders.remove(matching_order)
                            log_message('INFO', f"Closed position for {user_id} on {symbol}: Realized PNL {realized_pnl}")
            except Exception as e:
                failed_users.append(user_id)
                log_message('ERROR', f"Error syncing closed positions for {user_id}: {str(e)}. Check internet connectivity or Binance API status.")
    if failed_users:
        log_message('ERROR', f"Failed to sync closed positions for users: {', '.join(failed_users)}")

def sync_closed_positions_periodically():
    while not shutdown_event.is_set():
        sync_closed_positions()
        threading.Event().wait(60)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if data is None:
            log_message('ERROR', "No JSON data received in request body")
            return jsonify({"error": "No JSON data received"}), 400
        log_message('INFO', f"Received webhook data: {data}")
    except Exception as e:
        log_message('ERROR', f"Failed to parse JSON: {str(e)}")
        return jsonify({"error": f"Invalid JSON: {str(e)}"}), 400

    if data.get('token') != "secret123":
        log_message('ERROR', "Unauthorized webhook request")
        return jsonify({"error": "Unauthorized"}), 401

    action = data.get('action', 'trade').lower()
    market = data.get('market', 'futures').lower()

    with data_lock:
        for user_id, config in current_config.items():
            if not config['status']:
                log_message('INFO', f"Skipping user {user_id} (status is off)")
                continue
            try:
                client = Client(config['api_key'], config['api_secret'], requests_params={"timeout": 20})

                if action == "trade":
                    symbol = data.get('symbol', '').upper()
                    side = data.get('side', '').lower()
                    size = float(data.get('size', 0))

                    if not symbol or side not in ['buy', 'sell'] or size <= 0 or size > 100 or market not in ['futures', 'spot']:
                        log_message('ERROR', f"Invalid webhook data for trade: {data}")
                        return jsonify({"error": "Invalid data"}), 400

                    if market == "futures":
                        balance = float(client.futures_account()['availableBalance'])
                    else:
                        asset = symbol.replace('USDT', '') if 'USDT' in symbol else 'BTC'
                        balance = float(next(b['free'] for b in client.get_account()['balances'] if b['asset'] == 'USDT'))
                    # Retry fetching price to ensure it's valid
                    price = None
                    for attempt in range(CONFIG["max_retries"]):
                        try:
                            price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                            if price > 0:
                                break
                        except Exception as e:
                            log_message('ERROR', f"Failed to fetch price for {symbol} (attempt {attempt + 1}/{CONFIG['max_retries']}): {e}")
                            if attempt == CONFIG["max_retries"] - 1:
                                raise Exception(f"Failed to fetch price for {symbol} after {CONFIG['max_retries']} attempts")
                            threading.Event().wait(1)

                    notional = balance * (size / 100) * config['multiplier']
                    quantity = notional / price
                    if market == "futures":
                        quantity *= config['leverage']
                        notional_value = quantity * price
                        if notional_value < 5:
                            log_message('INFO', f"Skipping order for {user_id}: Notional value {notional_value} is below minimum 5 USDT. Balance: {balance}, Multiplier: {config['multiplier']}, Leverage: {config['leverage']}, Size: {size}, Price: {price}")
                            continue

                    info = client.get_symbol_info(symbol)
                    step_size = None
                    quantity_precision = info.get('quantityPrecision', 0)
                    for filt in info['filters']:
                        if filt['filterType'] == 'LOT_SIZE':
                            step_size = float(filt['stepSize'])
                            break
                    if step_size is None:
                        log_message('ERROR', f"Could not find LOT_SIZE filter for {symbol}")
                        continue

                    quantity = round_quantity(quantity, step_size, quantity_precision)
                    log_message('INFO', f"Calculated quantity for {user_id} on {symbol}: {quantity} (stepSize: {step_size}, precision: {quantity_precision})")

                    if quantity == 0:
                        log_message('INFO', f"Quantity for {user_id} on {symbol} is 0 after rounding")
                        continue

                    order_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    size_usdt = quantity * price
                    if market == "futures":
                        order = client.futures_create_order(
                            symbol=symbol,
                            side=side.upper(),
                            type="MARKET",
                            quantity=quantity
                        )
                    else:
                        order = client.order_market_buy(symbol=symbol, quantity=quantity) if side == "buy" else client.order_market_sell(symbol=symbol, quantity=quantity)

                    order_id = str(order['orderId'])
                    pending_orders.append({
                        "order_id": order_id,
                        "user_id": user_id,
                        "symbol": symbol,
                        "side": side.upper(),
                        "order_type": "MARKET",
                        "price": price,
                        "quantity": quantity,
                        "size_usdt": size_usdt,
                        "status": "FILLED",
                        "time": order_time
                    })
                    log_message('INFO', f"Placed {market} {side} order for {user_id} on {symbol}: {quantity} units (Size USDT: {size_usdt})")

                elif action == "close":
                    symbol = data.get('symbol', '').upper()
                    percentage = float(data.get('percentage', 100))

                    if not symbol or percentage <= 0 or percentage > 100 or market not in ['futures', 'spot']:
                        log_message('ERROR', f"Invalid webhook data for close: {data}")
                        return jsonify({"error": "Invalid data"}), 400

                    positions = client.futures_position_information()
                    position = next((pos for pos in positions if pos['symbol'] == symbol and float(pos['positionAmt']) != 0), None)
                    if not position:
                        open_symbols = [pos['symbol'] for pos in positions if float(pos['positionAmt']) != 0]
                        log_message('INFO', f"No open position for {user_id} on {symbol} to close. Open positions: {open_symbols}")
                        continue

                    position_amt = float(position['positionAmt'])
                    side_to_close = "SELL" if position_amt > 0 else "BUY"
                    quantity_to_close = abs(position_amt) * (percentage / 100)

                    info = client.get_symbol_info(symbol)
                    step_size = None
                    quantity_precision = info.get('quantityPrecision', 0)
                    for filt in info['filters']:
                        if filt['filterType'] == 'LOT_SIZE':
                            step_size = float(filt['stepSize'])
                            break
                    if step_size is None:
                        log_message('ERROR', f"Could not find LOT_SIZE filter for {symbol}")
                        continue

                    quantity_to_close = round_quantity(quantity_to_close, step_size, quantity_precision)
                    log_message('INFO', f"Calculated quantity to close for {user_id} on {symbol}: {quantity_to_close} (stepSize: {step_size}, precision: {quantity_precision})")

                    if quantity_to_close == 0:
                        log_message('INFO', f"Quantity to close for {user_id} on {symbol} is 0 after rounding")
                        continue

                    order_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                    notional_value = quantity_to_close * price
                    log_message('INFO', f"Notional value of closing order for {user_id} on {symbol}: {notional_value} USDT")

                    order = client.futures_create_order(
                        symbol=symbol,
                        side=side_to_close,
                        type="MARKET",
                        quantity=quantity_to_close,
                        reduceOnly=True
                    )

                    order_id = str(order['orderId'])
                    size_usdt = quantity_to_close * price
                    pending_orders.append({
                        "order_id": order_id,
                        "user_id": user_id,
                        "symbol": symbol,
                        "side": side_to_close,
                        "order_type": "MARKET",
                        "price": price,
                        "quantity": quantity_to_close,
                        "size_usdt": size_usdt,
                        "status": "FILLED",
                        "time": order_time
                    })
                    log_message('INFO', f"Closed {percentage}% of position for {user_id} on {symbol}: {quantity_to_close} units via {side_to_close} order")

                elif action == "close_all":
                    if market not in ['futures', 'spot']:
                        log_message('ERROR', f"Invalid market in close_all: {data}")
                        return jsonify({"error": "Invalid data"}), 400

                    positions = client.futures_position_information()
                    open_positions = [pos for pos in positions if float(pos['positionAmt']) != 0]

                    if not open_positions:
                        log_message('INFO', f"No open positions to close for {user_id}")
                        continue

                    for position in open_positions:
                        symbol = position['symbol']
                        position_amt = float(position['positionAmt'])
                        side_to_close = "SELL" if position_amt > 0 else "BUY"
                        quantity_to_close = abs(position_amt)

                        info = client.get_symbol_info(symbol)
                        step_size = None
                        quantity_precision = info.get('quantityPrecision', 0)
                        for filt in info['filters']:
                            if filt['filterType'] == 'LOT_SIZE':
                                step_size = float(filt['stepSize'])
                                break
                        if step_size is None:
                            log_message('ERROR', f"Could not find LOT_SIZE filter for {symbol}")
                            continue

                        quantity_to_close = round_quantity(quantity_to_close, step_size, quantity_precision)
                        log_message('INFO', f"Calculated quantity to close for {user_id} on {symbol}: {quantity_to_close} (stepSize: {step_size}, precision: {quantity_precision})")

                        if quantity_to_close == 0:
                            log_message('INFO', f"Quantity to close for {user_id} on {symbol} is 0 after rounding")
                            continue

                        order_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                        notional_value = quantity_to_close * price
                        log_message('INFO', f"Notional value of closing order for {user_id} on {symbol}: {notional_value} USDT")

                        order = client.futures_create_order(
                            symbol=symbol,
                            side=side_to_close,
                            type="MARKET",
                            quantity=quantity_to_close,
                            reduceOnly=True
                        )

                        order_id = str(order['orderId'])
                        size_usdt = quantity_to_close * price
                        pending_orders.append({
                            "order_id": order_id,
                            "user_id": user_id,
                            "symbol": symbol,
                            "side": side_to_close,
                            "order_type": "MARKET",
                            "price": price,
                            "quantity": quantity_to_close,
                            "size_usdt": size_usdt,
                            "status": "FILLED",
                            "time": order_time
                        })
                        log_message('INFO', f"Closed all position for {user_id} on {symbol}: {quantity_to_close} units via {side_to_close} order")

            except Exception as e:
                log_message('ERROR', f"Error processing webhook for {user_id}: {e}")
    return jsonify({"message": "Webhook processed"}), 200

@app.route('/update_order_sizes', methods=['POST'])
@login_required
def update_order_sizes():
    with data_lock:
        for user_id, config in current_config.items():
            try:
                client = Client(config['api_key'], config['api_secret'], requests_params={"timeout": 20})
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT order_id, user_id, symbol, status FROM Orders WHERE size_usdt IS NULL OR size_usdt = 0")
                    orders = [dict(row) for row in cursor.fetchall()]
                    for order in orders:
                        order_id = order['order_id']
                        symbol = order['symbol']
                        status = order['status']
                        try:
                            price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                            binance_order = client.futures_get_order(symbol=symbol, orderId=order_id)
                            executed_qty = float(binance_order.get('executedQty', 0))
                            orig_qty = float(binance_order.get('origQty', 0))
                            if status == "FILLED" and executed_qty > 0:
                                size_usdt = executed_qty * price
                                cursor.execute("UPDATE Orders SET quantity = ?, size_usdt = ? WHERE order_id = ?",
                                               (executed_qty, size_usdt, order_id))
                                conn.commit()
                                log_message('INFO', f"Updated FILLED order {order_id} with quantity {executed_qty}, size_usdt {size_usdt}")
                            elif status == "NEW" and orig_qty > 0:
                                size_usdt = orig_qty * price
                                cursor.execute("UPDATE Orders SET quantity = ?, size_usdt = ? WHERE order_id = ?",
                                               (orig_qty, size_usdt, order_id))
                                conn.commit()
                                log_message('INFO', f"Updated NEW order {order_id} with quantity {orig_qty}, size_usdt {size_usdt}")
                            else:
                                log_message('WARNING', f"No valid quantity for order {order_id}, skipping size_usdt update")
                        except Exception as e:
                            log_message('ERROR', f"Failed to update size_usdt for order {order_id}: {e}")
            except Exception as e:
                log_message('ERROR', f"Error updating order sizes for {user_id}: {e}")
    return jsonify({"message": "Order sizes updated"}), 200

@app.route('/update_closed_position_sizes', methods=['POST'])
@login_required
def update_closed_position_sizes():
    with data_lock:
        for user_id, config in current_config.items():
            try:
                client = Client(config['api_key'], config['api_secret'], requests_params={"timeout": 20})
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, user_id, symbol, quantity, entry_price FROM ClosedPositions WHERE size_usdt IS NULL OR size_usdt = 0")
                    positions = [dict(row) for row in cursor.fetchall()]
                    for pos in positions:
                        pos_id = pos['id']
                        symbol = pos['symbol']
                        quantity = pos['quantity']
                        entry_price = pos['entry_price']
                        try:
                            if quantity is None or quantity == 0:
                                log_message('WARNING', f"No quantity for closed position {pos_id}, skipping size_usdt update")
                                continue
                            if entry_price is None or entry_price == 0:
                                entry_price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                                log_message('INFO', f"Fetched current price {entry_price} for {symbol} as entry price was invalid")
                                cursor.execute("UPDATE ClosedPositions SET entry_price = ? WHERE id = ?",
                                               (entry_price, pos_id))
                            size_usdt = quantity * entry_price
                            cursor.execute("UPDATE ClosedPositions SET size_usdt = ? WHERE id = ?",
                                           (size_usdt, pos_id))
                            conn.commit()
                            log_message('INFO', f"Updated closed position {pos_id} with size_usdt {size_usdt}")
                        except Exception as e:
                            log_message('ERROR', f"Failed to update size_usdt for closed position {pos_id}: {e}")
            except Exception as e:
                log_message('ERROR', f"Error updating closed position sizes for {user_id}: {e}")
    return jsonify({"message": "Closed position sizes updated"}), 200

@app.route('/')
@login_required
def index():
    return render_template('dashboard.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, password_hash FROM Users WHERE username = ?", (username,))
            user_data = cursor.fetchone()
            if user_data and check_password_hash(user_data[2], password):
                user = User(user_data[0], user_data[1], user_data[2])
                login_user(user)
                return redirect(url_for('index'))
            else:
                flash("Invalid username or password")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/save_api', methods=['POST'])
@login_required
def save_api():
    user_id = request.form.get('user_id')
    api_key = request.form.get('api_key')
    api_secret = request.form.get('api_secret')
    multiplier = float(request.form.get('multiplier', 1.0))
    leverage = int(request.form.get('leverage', 1))

    if not user_id or not api_key or not api_secret or multiplier < 0 or leverage < 1:
        return jsonify({"error": "Invalid input"}), 400

    try:
        client = Client(api_key, api_secret, requests_params={"timeout": 20})
        client.get_account()
    except Exception as e:
        log_message('ERROR', f"Invalid Binance API keys for {user_id}: {e}")
        return jsonify({"error": f"Invalid API keys: {str(e)}"}), 400

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""INSERT OR REPLACE INTO Config 
            (user_id, api_key, api_secret, status, available_fund, live_pnl, multiplier, leverage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, api_key, api_secret, 1, 0.0, 0.0, multiplier, leverage))
        conn.commit()

    with data_lock:
        current_config[user_id] = {
            "available_fund": 0.0,
            "live_pnl": 0.0,
            "status": 1,
            "api_key": api_key,
            "api_secret": api_secret,
            "multiplier": multiplier,
            "leverage": leverage
        }
    log_message('INFO', f"Saved API credentials for {user_id}")
    return jsonify({"message": "API credentials saved"}), 200

@app.route('/config', methods=['GET'])
@login_required
@cache.cached(timeout=10)
def get_config():
    failed_users = []
    with data_lock:
        for user_id, config in current_config.items():
            if not config['status']:
                continue
            try:
                client = Client(config['api_key'], config['api_secret'], requests_params={"timeout": 20})
                balance = float(client.futures_account()['availableBalance'])
                config['available_fund'] = balance
                config['live_pnl'] = float(client.futures_account()['totalUnrealizedProfit'])
            except Exception as e:
                failed_users.append(user_id)
                log_message('ERROR', f"Error fetching config for {user_id}: {str(e)}. Check internet connectivity or Binance API status.")
        if failed_users:
            log_message('ERROR', f"Failed to fetch config for users: {', '.join(failed_users)}")
        return jsonify([{"user_id": k, **v} for k, v in current_config.items()])

@app.route('/orders', methods=['GET'])
@login_required
def get_orders():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM Orders")
        orders = [dict(row) for row in cursor.fetchall()]
    return jsonify(orders)

@app.route('/open_positions', methods=['GET'])
@login_required
def get_open_positions():
    positions = []
    with data_lock:
        for user_id, config in current_config.items():
            if not config['status']:
                continue
            try:
                client = Client(config['api_key'], config['api_secret'], requests_params={"timeout": 20})
                futures_positions = client.futures_position_information()
                for pos in futures_positions:
                    if float(pos['positionAmt']) != 0:
                        unrealized_pnl = float(pos.get('unRealizedProfit', pos.get('unrealizedProfit', 0.0)))
                        size_usdt = float(pos['positionAmt']) * float(pos['markPrice'])
                        position = {
                            "user_id": user_id,
                            "symbol": pos['symbol'],
                            "size_usdt": round(size_usdt, 2),
                            "entry_price": float(pos['entryPrice']),
                            "mark_price": float(pos['markPrice']),
                            "unrealized_pnl": unrealized_pnl
                        }
                        positions.append(position)
            except Exception as e:
                log_message('ERROR', f"Error fetching positions for {user_id}: {str(e)}. Check internet connectivity or Binance API status.")
    if positions:
        log_message('INFO', f"Returning {len(positions)} open positions: {[pos['symbol'] for pos in positions]}")
    else:
        log_message('INFO', "No open positions found for active users")
    return jsonify(positions)

@app.route('/closed_positions', methods=['GET'])
@login_required
def get_closed_positions():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ClosedPositions")
        closed_positions = [dict(row) for row in cursor.fetchall()]
    return jsonify(closed_positions)

@app.route('/sync_closed_positions', methods=['POST'])
@login_required
def manual_sync_closed_positions():
    sync_closed_positions()
    return jsonify({"message": "Closed positions synced"}), 200

@app.route('/update_status', methods=['POST'])
@login_required
def update_status():
    user_id = request.json['user_id']
    status = int(request.json['status'])
    with data_lock:
        if user_id in current_config:
            current_config[user_id]['status'] = status
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE Config SET status = ? WHERE user_id = ?", (status, user_id))
                conn.commit()
            log_message('INFO', f"Updated status for {user_id} to {status}")
            return jsonify({"message": "Status updated"}), 200
    return jsonify({"error": "User not found"}), 404

@app.route('/update_multiplier', methods=['POST'])
@login_required
def update_multiplier():
    user_id = request.json['user_id']
    multiplier = float(request.json['multiplier'])
    if multiplier < 0:
        return jsonify({"error": "Multiplier must be non-negative"}), 400
    with data_lock:
        if user_id in current_config:
            current_config[user_id]['multiplier'] = multiplier
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE Config SET multiplier = ? WHERE user_id = ?", (multiplier, user_id))
                conn.commit()
            log_message('INFO', f"Updated multiplier for {user_id} to {multiplier}")
            return jsonify({"message": "Multiplier updated"}), 200
    return jsonify({"error": "User not found"}), 404

@app.route('/update_leverage', methods=['POST'])
@login_required
def update_leverage():
    user_id = request.json['user_id']
    leverage = int(request.json['leverage'])
    if leverage < 1:
        return jsonify({"error": "Leverage must be at least 1"}), 400
    with data_lock:
        if user_id in current_config:
            current_config[user_id]['leverage'] = leverage
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE Config SET leverage = ? WHERE user_id = ?", (leverage, user_id))
                conn.commit()
            log_message('INFO', f"Updated leverage for {user_id} to {leverage}")
            return jsonify({"message": "Leverage updated"}), 200
    return jsonify({"error": "User not found"}), 404

@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    user_id = request.json['user_id']
    with data_lock:
        if user_id in current_config:
            del current_config[user_id]
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Config WHERE user_id = ?", (user_id,))
                conn.commit()
            log_message('INFO', f"Deleted account for {user_id}")
            return jsonify({"message": "Account deleted"}), 200
    return jsonify({"error": "User not found"}), 404

def read_api_keys(db_file="trading_data.db"):
    with get_db_connection(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, api_key, api_secret, status, available_fund, live_pnl, multiplier, leverage FROM Config")
        current_config.clear()
        for row in cursor.fetchall():
            if all([row[1], row[2]]):
                user_id = row[0]
                current_config[user_id] = {
                    "available_fund": float(row[4] or 0.0),
                    "live_pnl": float(row[5] or 0.0),
                    "status": int(row[3] or 1),
                    "api_key": row[1],
                    "api_secret": row[2],
                    "multiplier": float(row[6] or 1.0),
                    "leverage": int(row[7] or 1)
                }
    log_message('INFO', "Loaded API keys from database")

def main():
    initialize_database()
    read_api_keys()
    db_thread = threading.Thread(target=db_updater, args=("trading_data.db",))
    db_thread.daemon = True
    db_thread.start()
    sync_thread = threading.Thread(target=sync_closed_positions_periodically)
    sync_thread.daemon = True
    sync_thread.start()
    log_message('INFO', "Starting server on http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)

if __name__ == "__main__":
    main()