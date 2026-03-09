"""Server monitoring: Docker health, system resources, email digest alerts."""

import asyncio
import logging
import shutil
import smtplib
import socket
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

# Track consecutive unhealthy counts per container (module-level state)
_unhealthy_counts: dict[str, int] = {}

# Track previously-seen containers to detect missing ones
_previous_containers: set[str] | None = None


async def check_docker_health() -> dict:
    """Check Docker container health via subprocess calls.

    Returns dict with keys: healthy, unhealthy, missing, restarting, oom_killed.
    """
    global _previous_containers

    result: dict[str, list[dict]] = {
        "healthy": [],
        "unhealthy": [],
        "missing": [],
        "restarting": [],
        "oom_killed": [],
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a",
            "--format", "{{.Names}}\t{{.Status}}\t{{.State}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        logger.warning("Docker CLI not found — skipping container health check")
        return result
    except OSError as e:
        logger.warning("Cannot access Docker: %s — skipping container health check", e)
        return result

    if proc.returncode != 0:
        err_msg = stderr.decode().strip() if stderr else "unknown error"
        logger.warning("docker ps failed (rc=%d): %s", proc.returncode, err_msg)
        return result

    current_containers: set[str] = set()
    lines = stdout.decode().strip().split("\n") if stdout else []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, status, state = parts[0], parts[1], parts[2]
        current_containers.add(name)

        entry = {"name": name, "status": status, "state": state}

        if state == "restarting":
            result["restarting"].append(entry)
        elif "unhealthy" in status.lower():
            result["unhealthy"].append(entry)
        else:
            result["healthy"].append(entry)

    # Detect missing containers (were running before, now gone)
    if _previous_containers is not None:
        missing = _previous_containers - current_containers
        for name in missing:
            result["missing"].append({"name": name, "status": "missing", "state": "missing"})
    _previous_containers = current_containers

    # Check for OOMKilled containers
    for container in result["restarting"] + result["unhealthy"]:
        try:
            inspect_proc = await asyncio.create_subprocess_exec(
                "docker", "inspect",
                "--format", "{{.State.OOMKilled}}",
                container["name"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await inspect_proc.communicate()
            if out and out.decode().strip().lower() == "true":
                result["oom_killed"].append(container)
        except (FileNotFoundError, OSError):
            pass

    return result


async def check_system_resources() -> dict:
    """Check disk, RAM, and GPU VRAM usage. Return warnings for high usage.

    Returns dict with keys: disk, memory, gpu, warnings.
    """
    result: dict = {"disk": {}, "memory": {}, "gpu": {}, "warnings": []}

    # Disk usage
    try:
        usage = shutil.disk_usage("/")
        used_pct = (usage.used / usage.total) * 100
        result["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "used_percent": round(used_pct, 1),
        }
        if used_pct > 85:
            result["warnings"].append(
                f"Disk usage critical: {used_pct:.1f}% "
                f"({result['disk']['free_gb']}GB free)"
            )
    except OSError as e:
        logger.warning("Cannot check disk usage: %s", e)

    # Memory usage from /proc/meminfo
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]  # value in kB
                    meminfo[key] = int(val)

        total_kb = meminfo.get("MemTotal", 0)
        available_kb = meminfo.get("MemAvailable", 0)
        if total_kb > 0:
            used_kb = total_kb - available_kb
            used_pct = (used_kb / total_kb) * 100
            result["memory"] = {
                "total_gb": round(total_kb / (1024**2), 1),
                "used_gb": round(used_kb / (1024**2), 1),
                "available_gb": round(available_kb / (1024**2), 1),
                "used_percent": round(used_pct, 1),
            }
            if used_pct > 90:
                result["warnings"].append(
                    f"RAM usage critical: {used_pct:.1f}% "
                    f"({result['memory']['available_gb']}GB available)"
                )
    except (OSError, ValueError) as e:
        logger.warning("Cannot read /proc/meminfo: %s", e)

    # GPU VRAM via nvidia-smi
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            line = stdout.decode().strip().split("\n")[0]
            parts = line.split(",")
            if len(parts) == 2:
                used_mb = float(parts[0].strip())
                total_mb = float(parts[1].strip())
                used_pct = (used_mb / total_mb) * 100 if total_mb > 0 else 0
                result["gpu"] = {
                    "used_mb": round(used_mb),
                    "total_mb": round(total_mb),
                    "used_percent": round(used_pct, 1),
                }
                if used_pct > 95:
                    result["warnings"].append(
                        f"GPU VRAM critical: {used_pct:.1f}% "
                        f"({round(total_mb - used_mb)}MB free)"
                    )
    except FileNotFoundError:
        pass  # No GPU / nvidia-smi not installed — fine
    except (OSError, ValueError) as e:
        logger.warning("nvidia-smi check failed: %s", e)

    return result


async def monitor_health() -> dict:
    """Scheduler job entry point: check health, store anomalies as memories.

    Returns summary dict for scheduler logging.
    """
    docker = await check_docker_health()
    resources = await check_system_resources()

    anomalies: list[str] = []
    stored_count = 0

    # Process unhealthy containers
    for container in docker["unhealthy"]:
        name = container["name"]
        _unhealthy_counts[name] = _unhealthy_counts.get(name, 0) + 1
        if _unhealthy_counts[name] >= settings.monitoring_unhealthy_threshold:
            anomaly = (
                f"Container '{name}' unhealthy for {_unhealthy_counts[name]} "
                f"consecutive checks. Status: {container['status']}"
            )
            anomalies.append(anomaly)
            logger.warning("Monitoring alert: %s", anomaly)

    # Reset healthy container counts
    for container in docker["healthy"]:
        _unhealthy_counts.pop(container["name"], None)

    # Process restarting containers
    for container in docker["restarting"]:
        anomaly = f"Container '{container['name']}' is restarting. Status: {container['status']}"
        anomalies.append(anomaly)
        logger.warning("Monitoring alert: %s", anomaly)

    # Process missing containers
    for container in docker["missing"]:
        anomaly = f"Container '{container['name']}' has disappeared (was running previously)"
        anomalies.append(anomaly)
        logger.warning("Monitoring alert: %s", anomaly)

    # Process OOM killed containers
    for container in docker["oom_killed"]:
        anomaly = f"Container '{container['name']}' was OOM-killed"
        anomalies.append(anomaly)
        logger.warning("Monitoring alert: %s", anomaly)

    # Process resource warnings
    for warning in resources["warnings"]:
        anomalies.append(warning)
        logger.warning("Monitoring alert: %s", warning)

    # Store anomalies as memories
    if anomalies:
        from nobrainr.services.memory import store_memory_with_extraction

        machine = settings.source_machine or socket.gethostname()
        for anomaly in anomalies:
            try:
                container_tag = _extract_container_name(anomaly)
                tags = ["monitoring", "alert"]
                if container_tag:
                    tags.append(container_tag)

                await store_memory_with_extraction(
                    content=anomaly,
                    category="infrastructure",
                    tags=tags,
                    source_type="monitoring",
                    source_machine=machine,
                    skip_dedup=True,
                )
                stored_count += 1
            except Exception:
                logger.exception("Failed to store monitoring anomaly: %s", anomaly)

    return {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "healthy_containers": len(docker["healthy"]),
        "unhealthy_containers": len(docker["unhealthy"]),
        "restarting_containers": len(docker["restarting"]),
        "missing_containers": len(docker["missing"]),
        "oom_killed": len(docker["oom_killed"]),
        "resource_warnings": len(resources["warnings"]),
        "anomalies_found": len(anomalies),
        "anomalies_stored": stored_count,
    }


def _extract_container_name(anomaly: str) -> str | None:
    """Try to extract a container name from an anomaly message."""
    if "Container '" in anomaly:
        start = anomaly.index("Container '") + len("Container '")
        end = anomaly.index("'", start)
        return anomaly[start:end]
    return None


async def send_email_digest() -> dict:
    """Scheduler job: send daily email digest of monitoring anomalies.

    Uses smtplib via asyncio.to_thread() to avoid blocking the event loop.
    """
    if not settings.monitoring_email_enabled:
        return {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "status": "skipped",
            "reason": "email_disabled",
        }

    if not settings.monitoring_smtp_host or not settings.monitoring_smtp_to:
        return {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "status": "skipped",
            "reason": "smtp_not_configured",
        }

    # Query recent monitoring memories from the last 24 hours
    from nobrainr.db import queries

    recent_anomalies = await queries.query_memories(
        category="infrastructure",
        tags=["monitoring", "alert"],
        limit=100,
    )

    # Filter to last 24 hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = []
    for mem in recent_anomalies:
        created = mem.get("created_at", "")
        if isinstance(created, str) and created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    recent.append(mem)
            except (ValueError, TypeError):
                pass

    machine = settings.source_machine or socket.gethostname()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not recent:
        subject = f"[{machine}] Monitoring Digest - All Clear ({now_str})"
        body = (
            f"Server Monitoring Digest for {machine}\n"
            f"Generated: {now_str}\n\n"
            "No anomalies detected in the last 24 hours.\n\n"
            "All systems operating normally.\n"
        )
    else:
        subject = (
            f"[{machine}] Monitoring Digest - "
            f"{len(recent)} anomalies ({now_str})"
        )
        lines = [
            f"Server Monitoring Digest for {machine}",
            f"Generated: {now_str}",
            f"Anomalies in last 24h: {len(recent)}",
            "",
            "=" * 60,
            "",
        ]
        for i, mem in enumerate(recent, 1):
            lines.append(f"[{i}] {mem.get('created_at', 'unknown time')}")
            lines.append(f"    {mem.get('content', 'no content')}")
            tags = mem.get("tags") or []
            if tags:
                lines.append(f"    Tags: {', '.join(tags)}")
            lines.append("")

        lines.append("=" * 60)
        lines.append("")
        lines.append(
            "This is an automated digest from the nobrainr monitoring system."
        )
        body = "\n".join(lines)

    # Send via smtplib in a thread
    try:
        await asyncio.to_thread(_send_smtp, subject, body)
        return {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "status": "sent",
            "anomaly_count": len(recent),
            "recipients": settings.monitoring_smtp_to,
        }
    except Exception as e:
        logger.exception("Failed to send monitoring email digest")
        return {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "error": str(e),
        }


def _send_smtp(subject: str, body: str) -> None:
    """Send a plain-text email via SMTP (runs in thread executor)."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.monitoring_smtp_from or settings.monitoring_smtp_user
    msg["To"] = settings.monitoring_smtp_to

    recipients = [
        r.strip() for r in settings.monitoring_smtp_to.split(",") if r.strip()
    ]

    with smtplib.SMTP(settings.monitoring_smtp_host, settings.monitoring_smtp_port) as server:
        server.ehlo()
        if settings.monitoring_smtp_port != 25:
            server.starttls()
            server.ehlo()
        if settings.monitoring_smtp_user and settings.monitoring_smtp_password:
            server.login(settings.monitoring_smtp_user, settings.monitoring_smtp_password)
        server.sendmail(msg["From"], recipients, msg.as_string())
