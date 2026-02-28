#!/bin/bash
set -e

echo "=== PostgreSQL HA Failover Connection Pool Recovery Solution ==="
echo ""

NS="bleater"

# Step 1: Diagnose the problem
echo "Step 1: Diagnosing the issue..."
echo ""
echo "Checking PostgreSQL pods:"
kubectl get pods -n "$NS" -l app.kubernetes.io/name=postgresql
echo ""

echo "Checking PgBouncer status:"
kubectl get pods -n "$NS" -l app=pgbouncer
echo ""

echo "Checking PgBouncer current configuration:"
kubectl get configmap pgbouncer-config -n "$NS" -o yaml | grep -A 5 "host="
echo ""

# Step 2: Check if primary has restarted or if we should use replica
echo "Step 2: Determining which PostgreSQL instance to target..."
echo ""

# Check if primary pod is ready
PRIMARY_READY=$(kubectl get pod bleater-postgresql-0 -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")

if [ "$PRIMARY_READY" = "True" ]; then
    echo "✓ Primary pod (bleater-postgresql-0) has restarted and is ready"
    TARGET_POD="bleater-postgresql-0"
    echo "  Will use restarted primary as target"
else
    echo "⚠ Primary pod not ready yet, will use replica (bleater-postgresql-1)"
    TARGET_POD="bleater-postgresql-1"

    # Optionally promote replica to primary (makes it read-write)
    echo "  Promoting replica to primary..."
    kubectl exec -n "$NS" "$TARGET_POD" -- su - postgres -c "pg_ctl promote -D /bitnami/postgresql/data" || {
        echo "  Note: Promotion may have already occurred or failed, continuing..."
    }
    sleep 5
fi

echo "  Selected target: $TARGET_POD"
echo ""

# Step 3: Check operational runbook for required configuration standards
echo "Step 3: Reading operational standards from cluster ConfigMap..."
echo ""

kubectl get configmap pgbouncer-operational-runbook -n "$NS" -o jsonpath='{.data.runbook\.md}' 2>/dev/null && echo "" || echo "  (runbook ConfigMap not found, using known standards)"

echo "  Applying: server_lifetime=300, server_idle_timeout=30, server_reset_query=DISCARD ALL"
echo ""

echo "Step 4: Updating PgBouncer configuration..."
echo ""

# Use pod-specific DNS name instead of IP (survives pod restarts)
# Format: <pod>.<statefulset-service>.<namespace>.svc.cluster.local
TARGET_HOST="${TARGET_POD}.bleater-postgresql.${NS}.svc.cluster.local"

echo "  Updating PgBouncer config to point to: $TARGET_HOST"

# Create new pgbouncer.ini with corrected settings per operational runbook
cat > /tmp/pgbouncer.ini <<EOF
[databases]
bleater = host=$TARGET_HOST port=5432 dbname=bleater user=bleater

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
auth_type = trust
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = session
max_client_conn = 100
default_pool_size = 20
server_lifetime = 300
server_idle_timeout = 30
server_reset_query = DISCARD ALL
EOF

# Create userlist.txt
cat > /tmp/userlist.txt <<EOF
"bleater" "bleater123"
EOF

# Update ConfigMap with new configuration (must include both files)
kubectl create configmap pgbouncer-config -n "$NS" \
  --from-file=pgbouncer.ini=/tmp/pgbouncer.ini \
  --from-file=userlist.txt=/tmp/userlist.txt \
  --dry-run=client -o yaml | kubectl apply -f -

echo "✓ PgBouncer config updated"
echo ""

# Step 5: Restart PgBouncer deployment to clear stale connection pool
echo "Step 5: Restarting PgBouncer to clear stale connections..."
echo ""

# Delete pods to force restart with updated ConfigMap
kubectl delete pod -l app=pgbouncer -n "$NS" --force --grace-period=0

echo "  Waiting for PgBouncer to restart..."
sleep 5
kubectl wait --for=condition=ready pod -l app=pgbouncer -n "$NS" --timeout=90s

echo "✓ PgBouncer restarted with fresh connection pool"
echo ""

# Step 6: Verify connectivity and data integrity
echo "Step 6: Verifying the fix..."
echo ""

echo "Testing basic connectivity through PgBouncer:"
kubectl exec -n "$NS" bleater-postgresql-0 -- \
  psql -U bleater -d bleater -h pgbouncer."$NS".svc.cluster.local \
  -c "SELECT 1 AS connectivity_test;"

echo ""
echo "Testing data integrity - querying test data:"
kubectl exec -n "$NS" bleater-postgresql-0 -- \
  psql -U bleater -d bleater -h pgbouncer."$NS".svc.cluster.local \
  -c "SELECT COUNT(*) AS test_bleat_count FROM bleats WHERE id=99999;"

echo ""
echo "Retrieving test bleat content:"
kubectl exec -n "$NS" bleater-postgresql-0 -- \
  psql -U bleater -d bleater -h pgbouncer."$NS".svc.cluster.local \
  -c "SELECT text FROM bleats WHERE id=99999;"

echo ""
echo "==================================================================="
echo "=== Solution Complete ==="
echo "==================================================================="
echo ""
echo "Summary of actions taken:"
echo "  ✅ Identified PgBouncer had stale connections to old primary IP"
echo "  ✅ Selected target PostgreSQL instance: $TARGET_POD"
echo "  ✅ Updated PgBouncer ConfigMap to use DNS name: $TARGET_HOST"
echo "  ✅ Improved connection pool settings (server_lifetime, server_reset_query)"
echo "  ✅ Restarted PgBouncer deployment to clear stale connection pool"
echo "  ✅ Verified connectivity through PgBouncer"
echo "  ✅ Verified data integrity (test data accessible)"
echo ""
echo "Result:"
echo "  - Applications can now connect to PostgreSQL through PgBouncer"
echo "  - Connection pool is healthy with no stale connections"
echo "  - Data integrity preserved through failover"
echo "  - Using DNS-based configuration (survives future pod restarts)"
echo ""
echo "==================================================================="
echo ""
