#!/usr/bin/env bash
set -u

echo "=== service ==="
systemctl status sc4s --no-pager -l || true

echo
echo "=== container ==="
docker ps --filter name=SC4S || true

echo
echo "=== health ==="
docker exec SC4S syslog-ng-ctl healthcheck --timeout 5 || true

echo
echo "=== listeners ==="
ss -lntup || true

echo
echo "=== HEC destination counters ==="
docker exec SC4S syslog-ng-ctl stats 2>/dev/null | grep 'dst.http;d_hec_fmt' || true

echo
echo "=== UDP errors ==="
netstat -su 2>/dev/null | grep -Ei 'receive|error|buffer' || true

echo
echo "=== filesystem ==="
df -hT
df -i

echo
echo "=== recent errors ==="
docker logs --since 10m SC4S 2>&1 | grep -Ei 'error|failed|invalid|incorrect index|status_code|drop|queue' | tail -200 || true

echo
echo "NOTE: This script is read-only. Review full output before drawing conclusions."
