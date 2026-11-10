#!/usr/bin/env python3
"""Send a completed Actions search result to the dispatch gateway."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    args = parser.parse_args()
    request_id = os.environ.get('REQUEST_ID', '')
    try:
        uuid.UUID(request_id, version=4)
    except ValueError as exc:
        raise SystemExit('Invalid request ID') from exc
    base = os.environ.get('RESULT_CALLBACK_URL', '').rstrip('/')
    secret = os.environ.get('RESULT_CALLBACK_SECRET', '')
    if not base.startswith('https://') or not secret:
        raise SystemExit('Callback URL and secret must be configured')
    filename = Path(args.input)
    if filename.exists():
        body = json.loads(filename.read_text())
    else:
        body = {'results': [], 'warnings': ['Search workflow failed before producing results'],
                'generated_at': datetime.now(timezone.utc).isoformat()}
    body['request_id'] = request_id
    body['query'] = os.environ.get('SEARCH_QUERY', '')
    data = json.dumps(body).encode('utf-8')
    request = urllib.request.Request(base + '/internal/results', data=data, method='POST', headers={
        'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json',
        'User-Agent': 'A2search-Actions',
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise SystemExit(f'Callback rejected: {response.status}')
    except urllib.error.URLError as exc:
        raise SystemExit('Callback failed') from exc


if __name__ == '__main__':
    main()
