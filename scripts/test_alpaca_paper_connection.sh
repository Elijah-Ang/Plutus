#!/bin/bash
set -euo pipefail

.venv/bin/python << 'EOF'
import sys
from app.broker_alpaca import AlpacaBroker
from app.utils import load_config

config = load_config()
print('Broker Mode configured as:', config.get('mode'))
print('Live Enabled configured as:', config.get('live_enabled'))

try:
    broker = AlpacaBroker(config)
    
    # Confirm it is paper
    if broker.mode != 'paper':
        print('ERROR: Broker is NOT in paper mode! Stopping connection test for safety.', file=sys.stderr)
        sys.exit(1)
        
    print('Connecting to Alpaca Paper...')
    account = broker.get_account()
    
    print('\n--- Alpaca Connection Successful ---')
    print(f'Account Status: {account.status}')
    print(f'Cash Balance: ${float(account.cash):.2f}')
    print(f'Buying Power: ${float(account.buying_power):.2f}')
    
    # Confirm endpoint mode via base URL
    base_url = getattr(broker.trading, '_base_url', '')
    if 'paper' in str(base_url).lower():
         print('Alpaca Endpoint Mode Confirmation: PAPER (Confirmed via base URL BaseURL.TRADING_PAPER)')
    else:
         print(f'Alpaca Endpoint Mode Confirmation: WARNING: Base URL is {base_url}!')

    positions = broker.get_positions()
    print(f'Active Positions: {len(positions)}')
    for pos in positions:
         print(f'  - {pos.symbol}: {pos.qty} shares @ {pos.avg_entry_price}')
         
    orders = broker.get_open_orders()
    print(f'Open Orders: {len(orders)}')
    for ord in orders:
         print(f'  - {ord.symbol} {ord.side}: {ord.qty or ord.notional}')
         
    is_open = broker.is_market_open()
    print(f'Market Open Status: {"OPEN" if is_open else "CLOSED"}')

except Exception as e:
    print(f'Connection Failed: {type(e).__name__} - {str(e)}', file=sys.stderr)
    sys.exit(1)
EOF
