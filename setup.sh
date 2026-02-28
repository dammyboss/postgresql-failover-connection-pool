#!/bin/bash
set -e

# -------- [DO NOT CHANGE ANYTHING BELOW] ---------------------------------------- #
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

ELAPSED=0
MAX_WAIT=180

until kubectl cluster-info >/dev/null 2>&1; do
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo "Error: k3s is not ready after ${MAX_WAIT} seconds"
        exit 1
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

# -------- [DO NOT CHANGE ANYTHING ABOVE] ---------------------------------------- #

NS="bleater"

WAIT_TIME=0
MAX_WAIT=300
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        break
    fi
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

WAIT_TIME=0
until kubectl get statefulset bleater-postgresql -n "$NS" >/dev/null 2>&1; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        break
    fi
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

WAIT_TIME=0
while kubectl get pods -n "$NS" -l app.kubernetes.io/name=postgresql 2>/dev/null | grep -q "Terminating"; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        break
    fi
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

WAIT_TIME=0
until [ "$(kubectl get pod bleater-postgresql-0 -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] || \
      ! kubectl get pod bleater-postgresql-0 -n "$NS" >/dev/null 2>&1; do
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        break
    fi
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))
done

kubectl patch statefulset bleater-postgresql -n "$NS" --type='json' -p='[
  {"op": "replace", "path": "/spec/replicas", "value": 2}
]'

kubectl wait --for=condition=ready pod/bleater-postgresql-0 -n "$NS" --timeout=600s
kubectl wait --for=condition=ready pod/bleater-postgresql-1 -n "$NS" --timeout=600s

sleep 10
kubectl exec -n "$NS" bleater-postgresql-1 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -c "SELECT pg_is_in_recovery();" | grep "t" || true

kubectl exec -n "$NS" bleater-postgresql-0 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -c "
  CREATE TABLE IF NOT EXISTS bleats (
    id SERIAL PRIMARY KEY,
    text TEXT,
    profile_id INTEGER,
    creation_date TIMESTAMP DEFAULT NOW()
  );
"

kubectl exec -n "$NS" bleater-postgresql-0 -- env PGPASSWORD=bleater123 psql -U bleater -d bleater -c "
  INSERT INTO bleats (id, text, profile_id, creation_date)
  VALUES (99999, 'Test bleat for failover scenario', 1, NOW())
  ON CONFLICT (id) DO NOTHING;
"

sleep 15

PRIMARY_IP=$(kubectl get pod bleater-postgresql-0 -n "$NS" -o jsonpath='{.status.podIP}')
echo "$PRIMARY_IP" > /tmp/original_primary_ip.txt

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
        image: edoburu/pgbouncer:v1.25.1-p0
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

kubectl wait --for=condition=ready pod -l app=pgbouncer -n "$NS" --timeout=180s || true

sleep 15

kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: pgbouncer-operational-runbook
  namespace: $NS
data:
  runbook.md: |
    # PgBouncer Operational Standards

    ## Backend Connection Timeouts
    server_lifetime must not exceed 3600 seconds. Recommended: 300.
    server_idle_timeout must not exceed 60 seconds. Recommended: 30.

    ## Backend Targeting
    Always use the pod-specific headless DNS name for direct primary targeting.
    Format: <pod>.<statefulset-service>.<namespace>.svc.cluster.local
    Example: bleater-postgresql-0.bleater-postgresql.bleater.svc.cluster.local
    Never use IP addresses — they change on pod restart.

    ## Connection Cleanup
    server_reset_query = DISCARD ALL must be set.
    This ensures connections are cleaned up properly after backend failures.

    ## Auth
    auth_type = trust with a userlist.txt file is the supported auth method.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: pgbouncer-legacy-config
  namespace: $NS
data:
  notes.txt: |
    # Legacy PgBouncer Configuration Notes (OUTDATED — DO NOT USE)
    # These settings were used in the original single-pod deployment.
    # They are preserved here for historical reference only.

    # Old host reference (no longer valid after StatefulSet migration):
    # host=bleater-postgresql.bleater.svc.cluster.local

    # Old timeout values (too aggressive for this workload):
    # server_lifetime = 120
    # server_idle_timeout = 10

    # Old pool mode (caused transaction ordering issues):
    # pool_mode = transaction
EOF

kubectl delete pod bleater-postgresql-0 -n "$NS" --grace-period=0 --force

sleep 10
