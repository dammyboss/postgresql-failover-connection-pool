import subprocess
import json
import re
from apex_arena._types import GradingResult

def run_kubectl(*args, namespace="bleater", timeout=20):
    cmd = ["kubectl", "-n", namespace] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip(), result.returncode

def grade(transcript: str) -> GradingResult:
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

    try:
        if pgbouncer_ini:
            dns_patterns = [
                r"bleater-postgresql-0\.bleater-postgresql",
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

    weights["uses_dns_not_ip"] = 0.25

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

    weights["database_accessible"] = 0.25

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

    weights["data_integrity_verified"] = 0.25

    try:
        if pgbouncer_ini:
            if "server_reset_query" in pgbouncer_ini:
                subscores["connection_pool_optimized"] = 1.0
                print("✓ server_reset_query configured for proper connection cleanup")
            else:
                subscores["connection_pool_optimized"] = 0.0
                print("✗ server_reset_query not configured — stale connections will not be cleaned up")
        else:
            subscores["connection_pool_optimized"] = 0.0
            print("✗ Cannot verify pool settings (config not found)")
    except Exception as e:
        print(f"✗ Error checking pool optimization: {e}")
        subscores["connection_pool_optimized"] = 0.0

    weights["connection_pool_optimized"] = 0.25

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
