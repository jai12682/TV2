from typing import Dict, List
import threading

slave_accounts: List[Dict] = []
current_positions: Dict[str, Dict] = {}
current_config: Dict[str, Dict] = {}
pending_orders: List[Dict] = []
closed_positions: List[Dict] = []
all_closed_positions: List[Dict] = []
data_lock = threading.Lock()
shutdown_event = threading.Event()
thread_status = {
    "db_updater": True,
    "balance_updater": True,
    "sync_slave_positions": True
}
api_weight_used = 0
last_weight_reset = 0.0
exchange_info_cache = None

CONFIG = {
    "db_update_interval": 10,
    "balance_update_interval": 30,
    "slave_sync_interval": 10,
    "state_save_interval": 300,
    "excel_export_interval": 300,
    "max_retries": 3,
    "max_backoff": 30,
    "max_api_weight": 1200,
    "weight_reset_interval": 60
}