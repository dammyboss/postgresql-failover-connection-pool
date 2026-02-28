import subprocess
import json
import re
from apex_arena._types import GradingResult

def run_kubectl(*args, namespace="bleater", timeout=20):
    """Execute a kubectl command and return stdout, returncode."""
    cmd = ["kubectl", "-n", namespace] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip(), result.returncode

def grade(transcript: str) -> GradingResult:
    """
    Grade the PostgreSQL failover connection pool recovery task.

    Scoring criteria (4 subscores, all binary 0.0 or 1.0):
    1. uses_dns_not_ip (35%) - Config uses DNS name instead of IP address
    2. database_accessible (20%) - Can connect and query through PgBouncer
    3. data_integrity_verified (15%) - Test data still accessible
    4. connection_pool_optimized (30%) - Fixed ALL problematic pool settings (all 3 required)

    Total weights = 100%

    Returns a score between 0.0 and 1.0.
    """
    subscores = {}
    weights = {}

    pgbouncer_ini = ""
    try:
        stdout, returncode = run_kubectl("get", "configmap", "pgbouncer-config", "-o", "json")
        if returncode == 0:
            cm = json.loads(stdout)
            pgbouncer_ini = cm.get("data", {}).get("pgbouncer.ini", "")
    except Exception as e:
        print(f"Error retrieving PgBouncer ConfigMap: {e}")

    # Check 1: Uses DNS name instead of IP address (35%)
    try:
        if pgbouncer_ini:
            dns_patterns = [
                r"bleater-postgresql-0\.bleater-postgresql",
                r"bleater-postgresql-1\.bleater-postgresql",
                r"bleater-postgresql\.bleater"
            ]
            uses_dns = any(re.search(pattern, pgbouncer_ini) for pattern in dns_patterns)

            ip_pattern = r"host=\d+\.\d+\.\d+\.\d+"
            uses_ip = re.search(ip_pattern, pgbouncer_ini) is not None

            if uses_dns and not uses_ip:
                subscores["uses_dns_not_ip"] = 1.0
                print("✓ Config uses DNS name (resilient to pod restarts)")
            else:
                subscores["uses_dns_not_ip"] = 0.0
                if uses_ip:
                    print("✗ Config still uses IP address (will break on next restart)")
                else:
                    print("✗ Config does not use proper DNS name")
        else:
            subscores["uses_dns_not_ip"] = 0.0
            print("✗ Cannot verify DNS usage (config not found)")
    except Exception as e:
        print(f"✗ Error checking DNS usage: {e}")
        subscores["uses_dns_not_ip"] = 0.0

    weights["uses_dns_not_ip"] = 0.35

    # Check 2: Database accessible through PgBouncer (20%)
    try:
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

    weights["database_accessible"] = 0.20

    # Check 3: Data integrity - test bleat still accessible (15%)
    try:
        stdout, returncode = run_kubectl(
            "exec", "bleater-postgresql-0", "--",
            "psql", "-U", "bleater", "-d", "bleater", "-h", "pgbouncer.bleater.svc.cluster.local",
            "-t", "-c", "SELECT COUNT(*) FROM bleats WHERE id=99999;",
            timeout=15
        )
        count = stdout.strip()
        if returncode == 0 and "1" in count:
            subscores["data_integrity_verified"] = 1.0
            print("✓ Test data accessible through PgBouncer")
        else:
            subscores["data_integrity_verified"] = 0.0
            print(f"✗ Cannot access test data (count={count})")
    except Exception as e:
        print(f"✗ Error checking data integrity: {e}")
        subscores["data_integrity_verified"] = 0.0

    weights["data_integrity_verified"] = 0.15

    # Check 4: Connection pool settings optimized (30%)
    try:
        if pgbouncer_ini:
            fixed_count = 0

            if re.search(r"server_lifetime\s*=\s*([0-9]+)", pgbouncer_ini):
                lifetime = int(re.search(r"server_lifetime\s*=\s*([0-9]+)", pgbouncer_ini).group(1))
                if lifetime <= 3600:
                    fixed_count += 1

            if re.search(r"server_idle_timeout\s*=\s*([0-9]+)", pgbouncer_ini):
                idle_timeout = int(re.search(r"server_idle_timeout\s*=\s*([0-9]+)", pgbouncer_ini).group(1))
                if idle_timeout < 600:
                    fixed_count += 1

            if "server_reset_query" in pgbouncer_ini:
                fixed_count += 1

            if fixed_count >= 3:
                subscores["connection_pool_optimized"] = 1.0
                print(f"✓ Connection pool settings fully optimized ({fixed_count}/3 settings fixed)")
            else:
                subscores["connection_pool_optimized"] = 0.0
                print(f"✗ Connection pool settings not fully optimized ({fixed_count}/3 settings fixed, need all 3)")
        else:
            subscores["connection_pool_optimized"] = 0.0
            print("✗ Cannot verify pool settings (config not found)")
    except Exception as e:
        print(f"✗ Error checking pool optimization: {e}")
        subscores["connection_pool_optimized"] = 0.0

    weights["connection_pool_optimized"] = 0.30

    total_score = sum(subscores[k] * weights[k] for k in subscores) / sum(weights.values())

    feedback_lines = []
    feedback_lines.append(f"Score: {total_score:.3f}\n")

    for key in subscores:
        status = "✅" if subscores[key] >= 1.0 else "❌"
        weight_pct = int(weights[key] * 100)
        feedback_lines.append(f"{status} {key}: {subscores[key]:.1f} (weight: {weight_pct}%)")

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
        status = "✅" if value >= 1.0 else "❌"
        print(f"  {status} {key}: {value:.3f} (weight: {weight_pct}%)")
    print(f"\nFeedback:")
    print(result.feedback)
    print(f"{'='*60}\n")
