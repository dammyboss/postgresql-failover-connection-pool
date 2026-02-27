#!/bin/bash
set -e

# -------- [DO NOT CHANGE ANYTHING BELOW] ---------------------------------------- #
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Wait for k3s to be ready
ELAPSED=0
MAX_WAIT=180

until kubectl cluster-info >/dev/null 2>&1; do
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo "Error: k3s is not ready after ${MAX_WAIT} seconds"
        exit 1
    fi
    echo "Waiting for k3s... (${ELAPSED}s elapsed)"
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

echo "k3s is ready!"
# -------- [DO NOT CHANGE ANYTHING ABOVE] ---------------------------------------- #

# Note: PgBouncer image auto-imported by K3s from /var/lib/rancher/k3s/agent/images/

NS="bleater"

echo "=== PostgreSQL HA Failover Connection Pool Task Setup ==="
echo ""

# Step 0: Wait for cluster to stabilize after fast-boot
echo "Step 0: Waiting for cluster to stabilize..."
echo "  Checking k3s node readiness..."

# Wait for node to be Ready (no taints blocking scheduling)
WAIT_TIME=0
MAX_WAIT=300
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        echo "  Warning: Node not ready after ${MAX_WAIT}s, continuing anyway..."
        break
    fi
    echo "  Waiting for node to be Ready... (${WAIT_TIME}s)"
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

echo "  ✓ Node is Ready"

# Wait for PostgreSQL StatefulSet to exist and not be in flux
echo "  Checking PostgreSQL StatefulSet..."
WAIT_TIME=0
until kubectl get statefulset bleater-postgresql -n "$NS" >/dev/null 2>&1; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        echo "  Warning: StatefulSet not found after ${MAX_WAIT}s, continuing anyway..."
        break
    fi
    echo "  Waiting for StatefulSet to exist... (${WAIT_TIME}s)"
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

# Wait for any existing PostgreSQL pods to finish terminating
echo "  Checking for terminating PostgreSQL pods..."
WAIT_TIME=0
while kubectl get pods -n "$NS" -l app.kubernetes.io/name=postgresql 2>/dev/null | grep -q "Terminating"; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        echo "  Warning: Pods still terminating after ${MAX_WAIT}s, continuing anyway..."
        break
    fi
    echo "  Waiting for old pods to finish terminating... (${WAIT_TIME}s)"
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

# Wait for primary pod to be Running or not exist (clean slate)
echo "  Waiting for stable pod state..."
WAIT_TIME=0
until [ "$(kubectl get pod bleater-postgresql-0 -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] || \
      ! kubectl get pod bleater-postgresql-0 -n "$NS" >/dev/null 2>&1; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        echo "  Warning: Pod state unstable after ${MAX_WAIT}s, continuing anyway..."
        break
    fi
    POD_PHASE=$(kubectl get pod bleater-postgresql-0 -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    echo "  Pod state: $POD_PHASE, waiting for Running or clean state... (${WAIT_TIME}s)"
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

echo "✓ Cluster stabilized and ready"
echo ""

# Step 1: Create PostgreSQL read replica using StatefulSet scaling
echo "Step 1: Creating PostgreSQL read replica..."

# Scale the existing PostgreSQL StatefulSet to add a replica
# NOTE: Bitnami PostgreSQL chart supports replication when replicas > 1
kubectl patch statefulset bleater-postgresql -n "$NS" --type='json' -p='[
  {"op": "replace", "path": "/spec/replicas", "value": 2}
]'

# Wait for primary to be ready first (StatefulSets create pods sequentially)
echo "Ensuring primary pod is ready..."
kubectl wait --for=condition=ready pod/bleater-postgresql-0 -n "$NS" --timeout=600s

# Now wait for replica to be ready
echo "Waiting for replica pod to start (this may take 1-2 minutes)..."
kubectl wait --for=condition=ready pod/bleater-postgresql-1 -n "$NS" --timeout=600s

# Verify replication is working
echo "Verifying streaming replication..."
sleep 10
kubectl exec -n "$NS" bleater-postgresql-1 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -c "SELECT pg_is_in_recovery();" | grep "t" || {
    echo "Warning: Replication check returned unexpected result, continuing anyway..."
}

echo "✓ PostgreSQL replica created"
echo ""

# Step 2: Ensure bleats table exists and populate test data on primary
echo "Step 2: Ensuring bleats table exists and populating test data..."

# Check if bleats table exists, create if needed
kubectl exec -n "$NS" bleater-postgresql-0 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -c "
  CREATE TABLE IF NOT EXISTS bleats (
    id SERIAL PRIMARY KEY,
    text TEXT,
    profile_id INTEGER,
    creation_date TIMESTAMP DEFAULT NOW()
  );
"

# Insert test data
kubectl exec -n "$NS" bleater-postgresql-0 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -c "
  INSERT INTO bleats (id, text, profile_id, creation_date)
  VALUES (99999, 'Test bleat for failover scenario', 1, NOW())
  ON CONFLICT (id) DO NOTHING;
"

# Verify data replicated to replica (give replication time to catch up)
echo "Waiting for data to replicate..."
sleep 15

REPLICA_COUNT=$(kubectl exec -n "$NS" bleater-postgresql-1 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -t -c "SELECT COUNT(*) FROM bleats WHERE id=99999;" 2>/dev/null | tr -d ' ')
if [ "$REPLICA_COUNT" = "1" ]; then
    echo "✓ Data successfully replicated to replica"
else
    echo "Warning: Data not yet replicated (found '$REPLICA_COUNT' rows), continuing anyway..."
fi
echo ""

# Step 3: Deploy PgBouncer with misconfigured connection pool
echo "Step 3: Deploying PgBouncer connection pooler with broken configuration..."

# Get primary pod IP for initial PgBouncer config (this will become stale after failover)
PRIMARY_IP=$(kubectl get pod bleater-postgresql-0 -n "$NS" -o jsonpath='{.status.podIP}')
echo "Primary PostgreSQL IP: $PRIMARY_IP"

kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: pgbouncer-config
  namespace: $NS
data:
  pgbouncer.ini: |
    [databases]
    bleater = host=$PRIMARY_IP port=5432 dbname=bleater user=bleater

    [pgbouncer]
    listen_addr = 0.0.0.0
    listen_port = 5432
    auth_type = trust
    auth_file = /etc/pgbouncer/userlist.txt
    pool_mode = session
    max_client_conn = 100
    default_pool_size = 20
    server_lifetime = 7200
    server_idle_timeout = 600

  userlist.txt: |
    "bleater" "bleater123"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pgbouncer
  namespace: $NS
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pgbouncer
  template:
    metadata:
      labels:
        app: pgbouncer
    spec:
      containers:
      - name: pgbouncer
        image: edoburu/pgbouncer:latest
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 5432
        env:
        - name: DATABASE_URL
          value: "postgres://bleater:bleater123@$PRIMARY_IP:5432/bleater"
        - name: POOL_MODE
          value: "session"
        - name: MAX_CLIENT_CONN
          value: "100"
        - name: DEFAULT_POOL_SIZE
          value: "20"
        - name: SERVER_LIFETIME
          value: "7200"
        - name: SERVER_IDLE_TIMEOUT
          value: "600"
        volumeMounts:
        - name: config
          mountPath: /etc/pgbouncer/pgbouncer.ini
          subPath: pgbouncer.ini
        - name: config
          mountPath: /etc/pgbouncer/userlist.txt
          subPath: userlist.txt
      volumes:
      - name: config
        configMap:
          name: pgbouncer-config
---
apiVersion: v1
kind: Service
metadata:
  name: pgbouncer
  namespace: $NS
spec:
  selector:
    app: pgbouncer
  ports:
  - port: 5432
    targetPort: 5432
  type: ClusterIP
EOF

echo "✓ PgBouncer deployed"
echo ""

# Step 4: Wait for PgBouncer to start and establish connections
echo "Step 4: Waiting for PgBouncer to start and establish connections..."
kubectl wait --for=condition=ready pod -l app=pgbouncer -n "$NS" --timeout=180s || {
    echo "Warning: PgBouncer pod did not become ready in time"
    echo "Checking PgBouncer pod status:"
    kubectl get pods -n "$NS" -l app=pgbouncer
    echo "Checking PgBouncer logs:"
    kubectl logs -n "$NS" -l app=pgbouncer --tail=30 || true
    echo "Continuing anyway..."
}

# Give PgBouncer time to establish connection pool
sleep 15

# Test connectivity through PgBouncer (should work before failover)
echo "Testing initial connectivity through PgBouncer..."
TEST_RESULT=$(kubectl exec -n "$NS" bleater-postgresql-0 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -h pgbouncer -t -c "SELECT COUNT(*) FROM bleats WHERE id=99999;" 2>/dev/null | tr -d ' ') || true

if [ "$TEST_RESULT" = "1" ]; then
    echo "✓ Initial connectivity through PgBouncer working"
else
    echo "Warning: Initial connectivity test returned: $TEST_RESULT"
fi
echo ""

# Step 5: Trigger failover - delete primary pod
echo "Step 5: Triggering failover by deleting primary pod..."
echo "This simulates a hardware failure or pod crash..."

kubectl delete pod bleater-postgresql-0 -n "$NS" --grace-period=0 --force

# Wait for the failure to propagate and PgBouncer to start experiencing connection issues
sleep 10

echo ""
echo "==================================================================="
echo "=== BROKEN STATE CREATED ==="
echo "==================================================================="
echo ""
echo "Summary of what happened:"
echo "  ✓ PostgreSQL HA setup created (primary + replica)"
echo "  ✓ PgBouncer connection pooler deployed"
echo "  ✓ Primary pod (bleater-postgresql-0) deleted - simulates crash"
echo "  ✗ Kubernetes will restart primary (takes 60-90s, gets NEW IP)"
echo "  ✗ PgBouncer connection pool still points to OLD primary IP: $PRIMARY_IP"
echo "  ✗ PgBouncer has stale connections cached (server_lifetime=7200s)"
echo "  ✓ Read replica (bleater-postgresql-1) is available with all data"
echo "  ✗ ALL connection attempts through PgBouncer will FAIL"
echo ""
echo "Current state:"
echo "  - PgBouncer service: pgbouncer.bleater.svc.cluster.local:5432"
echo "  - PgBouncer config points to: $PRIMARY_IP (STALE)"
echo "  - Primary pod: Restarting (new IP will be different)"
echo "  - Replica pod: bleater-postgresql-1 (healthy, has all data)"
echo ""
echo "What the agent must do:"
echo "  1. Identify PgBouncer has stale connections to old primary IP"
echo "  2. Choose one approach:"
echo "     Option A: Promote replica to new primary + update PgBouncer"
echo "     Option B: Wait for primary restart + update PgBouncer to new IP"
echo "  3. Update PgBouncer ConfigMap to point to correct PostgreSQL instance"
echo "  4. Restart/reload PgBouncer to clear stale connection pool"
echo "  5. Verify connectivity: Test queries through PgBouncer work"
echo "  6. Verify data integrity: Test data (id=99999) still accessible"
echo ""
echo "==================================================================="
echo ""
