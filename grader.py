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

    Scoring criteria (6 subscores, all binary 0.0 or 1.0):
    1. pgbouncer_config_updated (15%) - Config changed from original stale IP
    2. pgbouncer_restarted (15%) - PgBouncer pod restarted to clear cached connections
    3. uses_dns_not_ip (15%) - Config uses DNS name instead of IP address
    4. database_accessible (25%) - Can connect and query through PgBouncer
    5. data_integrity_verified (20%) - Test data still accessible
    6. connection_pool_optimized (10%) - Fixed problematic pool settings

    Total weights = 100%

    Returns a score between 0.0 and 1.0.
    """
    subscores = {}
    weights = {}
    namespace = "bleater"

    # Read original PRIMARY_IP stored by setup.sh
    original_ip = None
    try:
        with open("/tmp/original_primary_ip.txt", "r") as f:
            original_ip = f.read().strip()
    except Exception as e:
        print(f"Warning: Could not read original IP from /tmp/original_primary_ip.txt: {e}")

    # Get current PgBouncer ConfigMap
    pgbouncer_ini = ""
    try:
        stdout, returncode = run_kubectl("get", "configmap", "pgbouncer-config", "-o", "json")
        if returncode == 0:
            cm = json.loads(stdout)
            pgbouncer_ini = cm.get("data", {}).get("pgbouncer.ini", "")
    except Exception as e:
        print(f"Error retrieving PgBouncer ConfigMap: {e}")

    # Check 1: PgBouncer config updated from original IP (15%)
    try:
        if original_ip and pgbouncer_ini:
            # Check if config no longer contains the original stale IP
            if original_ip not in pgbouncer_ini:
                subscores["pgbouncer_config_updated"] = 1.0
                print(f"✓ PgBouncer config updated (original IP {original_ip} removed)")
            else:
                subscores["pgbouncer_config_updated"] = 0.0
                print(f"✗ PgBouncer config still contains stale IP {original_ip}")
        else:
            subscores["pgbouncer_config_updated"] = 0.0
            print("✗ Cannot verify config update (missing original IP or config)")
    except Exception as e:
        print(f"✗ Error checking config update: {e}")
        subscores["pgbouncer_config_updated"] = 0.0

    weights["pgbouncer_config_updated"] = 0.15

    # Check 2: PgBouncer pod restarted (15%)
    try:
        stdout_pod, returncode = run_kubectl("get", "pod", "-l", "app=pgbouncer", "-o", "json")
        if returncode == 0:
            pods = json.loads(stdout_pod)
            if pods["items"]:
                pod = pods["items"][0]
                restart_count = pod["status"]["containerStatuses"][0].get("restartCount", 0)

                # Check pod age (creation timestamp)
                from datetime import datetime, timezone
                creation_time_str = pod["metadata"]["creationTimestamp"]
                creation_time = datetime.fromisoformat(creation_time_str.replace('Z', '+00:00'))
                current_time = datetime.now(timezone.utc)
                pod_age_seconds = (current_time - creation_time).total_seconds()

                # Pod is considered "restarted" if either:
                # 1. restartCount > 0 (container crashed and restarted inside same pod)
                # 2. Pod is very young (< 600 seconds = 10 minutes), meaning it was recreated
                if restart_count > 0 or pod_age_seconds < 600:
                    subscores["pgbouncer_restarted"] = 1.0
                    if restart_count > 0:
                        print(f"✓ PgBouncer pod restarted ({restart_count} restarts)")
                    else:
                        print(f"✓ PgBouncer pod recreated (age: {int(pod_age_seconds)}s)")
                else:
                    subscores["pgbouncer_restarted"] = 0.0
                    print(f"✗ PgBouncer pod not restarted (age: {int(pod_age_seconds)}s, restartCount: {restart_count})")
            else:
                subscores["pgbouncer_restarted"] = 0.0
                print("✗ PgBouncer pod not found")
        else:
            subscores["pgbouncer_restarted"] = 0.0
            print("✗ Could not retrieve PgBouncer pod info")
    except Exception as e:
        print(f"✗ Error checking pod restart: {e}")
        subscores["pgbouncer_restarted"] = 0.0

    weights["pgbouncer_restarted"] = 0.15

    # Check 3: Uses DNS name instead of IP address (15%)
    try:
        if pgbouncer_ini:
            # Check for DNS patterns (pod DNS or service DNS)
            dns_patterns = [
                r"bleater-postgresql-0\.bleater-postgresql",
                r"bleater-postgresql-1\.bleater-postgresql",
                r"bleater-postgresql\.bleater"
            ]
            uses_dns = any(re.search(pattern, pgbouncer_ini) for pattern in dns_patterns)

            # Also check for IP address pattern (to ensure it's NOT using IP)
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

    weights["uses_dns_not_ip"] = 0.15

    # Check 4: Database accessible through PgBouncer (25%)
    try:
        # Try to connect and query through PgBouncer
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

    # Check 5: Data integrity - test bleat still accessible (20%)
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
            subscores["data_integrity_verified"] = 1.0
            print("✓ Test data accessible through PgBouncer")
        else:
            subscores["data_integrity_verified"] = 0.0
            print(f"✗ Cannot access test data (count={count})")
    except Exception as e:
        print(f"✗ Error checking data integrity: {e}")
        subscores["data_integrity_verified"] = 0.0

    weights["data_integrity_verified"] = 0.20

    # Check 6: Connection pool settings optimized (10%)
    try:
        if pgbouncer_ini:
            # Check if at least 2 of 3 problematic settings were fixed
            fixed_count = 0

            # Check server_lifetime (should be < 3600, original was 7200)
            if re.search(r"server_lifetime\s*=\s*([0-9]+)", pgbouncer_ini):
                lifetime = int(re.search(r"server_lifetime\s*=\s*([0-9]+)", pgbouncer_ini).group(1))
                if lifetime < 3600:
                    fixed_count += 1

            # Check server_idle_timeout (should be < 300, original was 600)
            if re.search(r"server_idle_timeout\s*=\s*([0-9]+)", pgbouncer_ini):
                idle_timeout = int(re.search(r"server_idle_timeout\s*=\s*([0-9]+)", pgbouncer_ini).group(1))
                if idle_timeout < 300:
                    fixed_count += 1

            # Check server_reset_query (should be present, was missing)
            if "server_reset_query" in pgbouncer_ini:
                fixed_count += 1

            if fixed_count >= 2:
                subscores["connection_pool_optimized"] = 1.0
                print(f"✓ Connection pool settings optimized ({fixed_count}/3 settings fixed)")
            else:
                subscores["connection_pool_optimized"] = 0.0
                print(f"✗ Connection pool settings not optimized ({fixed_count}/3 settings fixed)")
        else:
            subscores["connection_pool_optimized"] = 0.0
            print("✗ Cannot verify pool settings (config not found)")
    except Exception as e:
        print(f"✗ Error checking pool optimization: {e}")
        subscores["connection_pool_optimized"] = 0.0

    weights["connection_pool_optimized"] = 0.10

    # Calculate final score
    total_score = sum(subscores[k] * weights[k] for k in subscores) / sum(weights.values())

    # Generate feedback
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
