import subprocess
import json
import time
from apex_arena._types import GradingResult

def run_kubectl(*args, namespace="bleater", timeout=20):
    """Execute a kubectl command and return stdout, returncode."""
    cmd = ["kubectl", "-n", namespace] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip(), result.returncode

def grade(transcript: str) -> GradingResult:
    """
    Grade the PostgreSQL failover connection pool recovery task.

    Scoring criteria:
    1. Stale connections cleared (30%) - PgBouncer config updated OR pod restarted
    2. Database accessible (30%) - Can connect and query through PgBouncer
    3. Correct primary target (20%) - PgBouncer points to valid PostgreSQL instance
    4. Data integrity (20%) - Test data still accessible

    Returns a score between 0.0 and 1.0.
    """
    subscores = {}
    weights = {}
    namespace = "bleater"

    # Check 1: Stale connections cleared from PgBouncer (30%)
    # Verify PgBouncer config updated OR pod restarted
    try:
        # Get PgBouncer ConfigMap
        stdout, returncode = run_kubectl(
            "get", "configmap", "pgbouncer-config", "-o", "json"
        )
        if returncode == 0:
            cm = json.loads(stdout)
            pgbouncer_ini = cm.get("data", {}).get("pgbouncer.ini", "")

            # Check if PgBouncer was updated to point to a valid PostgreSQL instance
            # Either the restarted primary (bleater-postgresql-0) or promoted replica (bleater-postgresql-1)
            # Check for updated host configuration (using DNS names instead of IP)
            if "bleater-postgresql-0" in pgbouncer_ini or "bleater-postgresql-1" in pgbouncer_ini:
                # Config uses pod DNS (good - survives IP changes)
                subscores["stale_connections_cleared"] = 1.0
                print("✓ PgBouncer config updated with valid PostgreSQL host")
            else:
                # Check if PgBouncer pod was restarted (alternative fix method)
                stdout_pod, _ = run_kubectl("get", "pod", "-l", "app=pgbouncer", "-o", "json")
                pods = json.loads(stdout_pod)
                if pods["items"]:
                    restart_count = pods["items"][0]["status"]["containerStatuses"][0].get("restartCount", 0)
                    if restart_count > 0:
                        subscores["stale_connections_cleared"] = 1.0
                        print(f"✓ PgBouncer pod restarted ({restart_count} restarts)")
                    else:
                        subscores["stale_connections_cleared"] = 0.0
                        print("✗ PgBouncer not restarted and config not updated")
                else:
                    subscores["stale_connections_cleared"] = 0.0
                    print("✗ PgBouncer pod not found")
        else:
            subscores["stale_connections_cleared"] = 0.0
            print("✗ Could not retrieve PgBouncer ConfigMap")
    except Exception as e:
        print(f"✗ Error checking stale connections: {e}")
        subscores["stale_connections_cleared"] = 0.0

    weights["stale_connections_cleared"] = 0.30

    # Check 2: Database accessible through PgBouncer (30%)
    try:
        # Try to connect and query through PgBouncer
        # Use any available PostgreSQL pod to execute the query through PgBouncer
        stdout, returncode = run_kubectl(
            "exec", "bleater-postgresql-0", "--",
            "psql", "-U", "bleater", "-d", "bleater", "-h", "pgbouncer.bleater.svc.cluster.local",
            "-c", "SELECT 1;",
            timeout=15
        )
        if returncode == 0 and ("1 row" in stdout or "(1 row)" in stdout):
            subscores["database_accessible"] = 1.0
            print("✓ Can connect and query through PgBouncer")
        else:
            subscores["database_accessible"] = 0.0
            print(f"✗ Cannot query through PgBouncer (returncode={returncode})")
    except Exception as e:
        print(f"✗ Error checking database access: {e}")
        subscores["database_accessible"] = 0.0

    weights["database_accessible"] = 0.30

    # Check 3: PgBouncer points to correct primary (20%)
    try:
        # Verify PgBouncer config points to a healthy PostgreSQL instance
        stdout, _ = run_kubectl("get", "configmap", "pgbouncer-config", "-o", "jsonpath={.data.pgbouncer\\.ini}")

        # Check if host is set to a valid pod DNS or service
        # Valid targets: bleater-postgresql-0, bleater-postgresql-1, or bleater-postgresql service
        valid_targets = ["bleater-postgresql-0", "bleater-postgresql-1", "bleater-postgresql.bleater"]
        if any(target in stdout for target in valid_targets):
            subscores["correct_primary_target"] = 1.0
            print("✓ PgBouncer configured with valid PostgreSQL target")
        else:
            subscores["correct_primary_target"] = 0.0
            print(f"✗ PgBouncer not pointing to valid target")
    except Exception as e:
        print(f"✗ Error checking primary target: {e}")
        subscores["correct_primary_target"] = 0.0

    weights["correct_primary_target"] = 0.20

    # Check 4: Data integrity - test bleat still accessible (20%)
    try:
        # Query through PgBouncer to verify test data persisted
        stdout, returncode = run_kubectl(
            "exec", "bleater-postgresql-0", "--",
            "psql", "-U", "bleater", "-d", "bleater", "-h", "pgbouncer.bleater.svc.cluster.local",
            "-t", "-c", "SELECT COUNT(*) FROM bleats WHERE id=99999;",
            timeout=15
        )
        count = stdout.strip()
        if returncode == 0 and "1" in count:
            subscores["data_integrity"] = 1.0
            print("✓ Test data accessible through PgBouncer")
        else:
            subscores["data_integrity"] = 0.0
            print(f"✗ Cannot access test data (count={count})")
    except Exception as e:
        print(f"✗ Error checking data integrity: {e}")
        subscores["data_integrity"] = 0.0

    weights["data_integrity"] = 0.20

    # Calculate final score
    total_score = sum(subscores[k] * weights[k] for k in subscores) / sum(weights.values())

    # Generate feedback
    feedback_lines = []
    if subscores.get("stale_connections_cleared", 0) >= 1.0:
        feedback_lines.append("✅ Stale connections cleared")
    else:
        feedback_lines.append("❌ PgBouncer still has stale connections")

    if subscores.get("database_accessible", 0) >= 1.0:
        feedback_lines.append("✅ Database accessible through PgBouncer")
    else:
        feedback_lines.append("❌ Cannot connect through PgBouncer")

    if subscores.get("correct_primary_target", 0) >= 1.0:
        feedback_lines.append("✅ PgBouncer configured correctly")
    else:
        feedback_lines.append("❌ PgBouncer not pointing to valid target")

    if subscores.get("data_integrity", 0) >= 1.0:
        feedback_lines.append("✅ Data integrity verified")
    else:
        feedback_lines.append("❌ Cannot access test data")

    feedback = "\n".join(feedback_lines)

    return GradingResult(
        score=round(total_score, 3),
        subscores=subscores,
        weights=weights,
        feedback=feedback
    )

if __name__ == "__main__":
    result = grade("n/a")
    print(f"\n{'='*60}")
    print(f"SCORE: {result.score}")
    print(f"{'='*60}")
    print(f"\nSubscores:")
    for key, value in result.subscores.items():
        weight_pct = int(result.weights[key] * 100)
        print(f"  {key}: {value:.3f} (weight: {weight_pct}%)")
    print(f"\nFeedback:")
    print(result.feedback)
    print(f"{'='*60}\n")
