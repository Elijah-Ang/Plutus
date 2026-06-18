#!/bin/bash
set -euo pipefail

# Run the python test using project's virtual environment
.venv/bin/python -c "
import json
import sys
from app.ai_review import review_proposal, AIReviewer
from app.utils import load_config

proposal = {
    'id': 'test-proposal-123',
    'mode': 'PAPER',
    'symbol': 'QQQ',
    'side': 'buy',
    'notional': 5,
    'reason': 'price above trend filters',
    'risk': 'all deterministic risk checks passed',
    'warning': 'educational paper-trading test only'
}

config = load_config()
ai_config = config.get('ai', {})

print('--- Running AI Summary Test with Safe Scenario ---')
result = review_proposal(proposal, ai_config)

print('AI Response fields:')
for k, v in result.items():
    print(f'  {k}: {v}')

# Verify shape
required = {'summary', 'risks', 'telegram_message', 'caution_level', 'should_block_for_reasoning_only', 'reasoning_notes'}
if not required.issubset(result.keys()):
    print('ERROR: AI response missing required fields!', file=sys.stderr)
    sys.exit(1)

# Check content expectations
print('\n--- Verifying content expectations ---')
if not isinstance(result['risks'], list):
    print('ERROR: Risks must be a list!', file=sys.stderr)
    sys.exit(1)
if result['caution_level'] not in {'low', 'medium', 'high'}:
    print('ERROR: Invalid caution level!', file=sys.stderr)
    sys.exit(1)
print('Content structure is valid.')

# Test fallback behavior with invalid client/response
print('\n--- Testing fallback behavior with invalid input ---')
class MockClient:
    class Responses:
        def create(self, **kwargs):
            return type('R', (), {'output_text': 'invalid-json-response'})()
    responses = Responses()

fallback_result = AIReviewer(ai_config, MockClient()).review(proposal)
print('Fallback Result:')
for k, v in fallback_result.items():
    print(f'  {k}: {v}')

if 'Deterministic fallback' not in fallback_result['reasoning_notes']:
    print('ERROR: Fallback failed!', file=sys.stderr)
    sys.exit(1)
print('Fallback validation passed.')
"
