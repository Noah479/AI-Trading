# Smoketest Report

- Time: 2025-10-28T19:18:16.631566Z
- Base: http://127.0.0.1:5001
- Logs Dir: ./logs

## Endpoints

- [OK] `/api/status` HTTP 200
- [OK] `/recent/signals?limit=50` HTTP 200
- [OK] `/recent/trades?limit=50` HTTP 200
- [OK] `/market` HTTP 200
- [OK] `/balance` HTTP 200
- [OK] `/verify` HTTP 200
- [OK] `/leaderboard` HTTP 200

## Signals

- items: 12

## Trades

- items: 3
  - [ISSUE] trades API: 有 3/3 条缺少 instId/side/size

## Market

- checked symbols: 6
  - [ISSUE] BNB-USDT: candles[4h] 为空
  - [ISSUE] BTC-USDT: candles[4h] 为空
  - [ISSUE] DOGE-USDT: candles[4h] 为空
  - [ISSUE] ETH-USDT: candles[4h] 为空
  - [ISSUE] SOL-USDT: candles[4h] 为空
  - [ISSUE] XRP-USDT: candles[4h] 为空

## Balance

- totalEq_incl_unrealized: 139926.77

## Logs Growth

- [OK] flask_server.log
- [WARN] recent_signals.jsonl
- [WARN] recent_trades.jsonl

## System

- Disk free: 639.21 GB
- CPU: 9.8%  MEM: 51.9%

## Summary

- FAIL: 7
- WARN: 2
- PASS: 11

