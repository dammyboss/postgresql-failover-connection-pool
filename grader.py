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
        conn_ok = False
        data_ok = False

        stdout, returncode = run_kubectl(
            "exec", "bleater-postgresql-0", "--",
            "psql", "-U", "bleater", "-d", "bleater", "-h", "pgbouncer.bleater.svc.cluster.local",
            "-c", "SELECT 1;",
            timeout=15
        )
        if returncode == 0 and ("1 row" in stdout or "(1 row)" in stdout):
            conn_ok = True

        stdout, returncode = run_kubectl(
            "exec", "bleater-postgresql-0", "--",
            "psql", "-U", "bleater", "-d", "bleater", "-h", "pgbouncer.bleater.svc.cluster.local",
            "-t", "-c", "SELECT COUNT(*) FROM bleats WHERE id=99999;",
            timeout=15
        )
        count = stdout.strip()
        if returncode == 0 and count == "1":
            data_ok = True

        if conn_ok and data_ok:
            subscores["database_accessible"] = 1.0
            print("✓ Connected through PgBouncer and test data is accessible")
        else:
            subscores["database_accessible"] = 0.0
            if not conn_ok:
                print("✗ Cannot connect through PgBouncer")
            else:
                print(f"✗ Connected but test data not found (count={count})")
    except Exception as e:
        print(f"✗ Error checking database access: {e}")
        subscores["database_accessible"] = 0.0

    weights["database_accessible"] = 0.25

    try:
        if pgbouncer_ini:
            lifetime_ok = False
            idle_ok = False

            lifetime_match = re.search(r"server_lifetime\s*=\s*(\d+)", pgbouncer_ini)
            if lifetime_match:
                lifetime = int(lifetime_match.group(1))
                if lifetime <= 3600:
                    lifetime_ok = True

            idle_match = re.search(r"server_idle_timeout\s*=\s*(\d+)", pgbouncer_ini)
            if idle_match:
                idle_timeout = int(idle_match.group(1))
                if idle_timeout <= 60:
                    idle_ok = True

            if lifetime_ok and idle_ok:
                subscores["pool_timeouts_configured"] = 1.0
                print("✓ Backend connection timeouts configured within operational limits")
            else:
                subscores["pool_timeouts_configured"] = 0.0
                if not lifetime_ok:
                    lt_val = lifetime_match.group(1) if lifetime_match else "not set"
                    print(f"✗ server_lifetime exceeds operational limit (value: {lt_val}s, limit: 3600)")
                if not idle_ok:
                    it_val = idle_match.group(1) if idle_match else "not set"
                    print(f"✗ server_idle_timeout exceeds operational limit (value: {it_val}s, limit: 60)")
        else:
            subscores["pool_timeouts_configured"] = 0.0
            print("✗ Cannot verify timeout settings (config not found)")
    except Exception as e:
        print(f"✗ Error checking pool timeouts: {e}")
        subscores["pool_timeouts_configured"] = 0.0

    weights["pool_timeouts_configured"] = 0.25

    try:
        if pgbouncer_ini:
            dns_patterns = [
                r"bleater-postgresql-0\.bleater-postgresql",
            ]
            uses_dns = any(re.search(pattern, pgbouncer_ini) for pattern in dns_patterns)

            ip_pattern = r"host=\d+\.\d+\.\d+\.\d+"
            uses_ip = re.search(ip_pattern, pgbouncer_ini) is not None

            if uses_dns and not uses_ip:
                subscores["uses_stable_backend_ref"] = 1.0
                print("✓ Config uses pod-specific DNS name (resilient to pod restarts)")
            else:
                subscores["uses_stable_backend_ref"] = 0.0
                if uses_ip:
                    print("✗ Config uses IP address (will break on pod restart)")
                else:
                    print("✗ Config does not use pod-specific DNS name")
        else:
            subscores["uses_stable_backend_ref"] = 0.0
            print("✗ Cannot verify backend reference (config not found)")
    except Exception as e:
        print(f"✗ Error checking backend reference: {e}")
        subscores["uses_stable_backend_ref"] = 0.0

    weights["uses_stable_backend_ref"] = 0.25

    try:
        if pgbouncer_ini:
            if "server_reset_query" in pgbouncer_ini:
                subscores["connection_cleanup_configured"] = 1.0
                print("✓ server_reset_query configured for connection cleanup after backend failures")
            else:
                subscores["connection_cleanup_configured"] = 0.0
                print("✗ server_reset_query not configured — connections not cleaned up after failures")
        else:
            subscores["connection_cleanup_configured"] = 0.0
            print("✗ Cannot verify connection cleanup settings (config not found)")
    except Exception as e:
        print(f"✗ Error checking connection cleanup: {e}")
        subscores["connection_cleanup_configured"] = 0.0

    weights["connection_cleanup_configured"] = 0.25

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
