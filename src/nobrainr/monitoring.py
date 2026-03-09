"""Server monitoring: Docker health, system resources, email digest alerts."""

import asyncio
import logging
import shutil
import smtplib
import socket
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from html import escape

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

# Track consecutive unhealthy counts per container (module-level state)
_unhealthy_counts: dict[str, int] = {}

# Track previously-seen containers to detect missing ones
_previous_containers: set[str] | None = None


async def check_docker_health(*, track_state: bool = True) -> dict:
    """Check Docker container health via subprocess calls.

    Args:
        track_state: When True (default), track previously-seen containers and
            detect missing ones.  Set to False for stateless API calls that
            should not mutate module-level state.

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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("docker ps timed out after 10s — skipping container health check")
        return result
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
    if track_state:
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
            out, _ = await asyncio.wait_for(inspect_proc.communicate(), timeout=10)
            if out and out.decode().strip().lower() == "true":
                result["oom_killed"].append(container)
        except asyncio.TimeoutError:
            logger.warning("docker inspect timed out for container '%s'", container["name"])
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
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
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
    except asyncio.TimeoutError:
        logger.warning("nvidia-smi timed out after 10s")
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
        if _unhealthy_counts[name] == settings.monitoring_unhealthy_threshold:
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


def _build_html_digest(
    *,
    machine: str,
    now_str: str,
    anomalies: list[dict],
    resources: dict,
    docker: dict,
) -> str:
    """Build an HTML email body for the monitoring digest."""
    healthy_count = len(docker.get("healthy", []))
    unhealthy_count = len(docker.get("unhealthy", []))
    restarting_count = len(docker.get("restarting", []))
    disk = resources.get("disk", {})
    memory = resources.get("memory", {})
    gpu = resources.get("gpu", {})

    def _pct_color(pct: float, warn: float = 75, crit: float = 90) -> str:
        if pct >= crit:
            return "#e74c3c"
        if pct >= warn:
            return "#f39c12"
        return "#2ecc71"

    def _bar(pct: float, warn: float = 75, crit: float = 90) -> str:
        color = _pct_color(pct, warn, crit)
        return (
            f'<div style="background:#2a2a3e;border-radius:4px;height:8px;width:100%;">'
            f'<div style="background:{color};border-radius:4px;height:8px;width:{min(pct, 100):.0f}%;"></div>'
            f'</div>'
        )

    # Status badge
    if anomalies:
        status_color = "#e74c3c"
        status_text = f"{len(anomalies)} anomalies"
        status_icon = "&#9888;"  # ⚠
    else:
        status_color = "#2ecc71"
        status_text = "All systems normal"
        status_icon = "&#10004;"  # ✔

    # Anomaly rows
    anomaly_rows = ""
    if anomalies:
        for mem in anomalies:
            created = mem.get("created_at", "")
            if isinstance(created, str) and len(created) > 19:
                created = created[:19].replace("T", " ")
            content = escape(mem.get("content", "no content"))
            tags = mem.get("tags") or []
            tag_badges = " ".join(
                f'<span style="background:#2a2a3e;color:#8a8aa0;padding:1px 6px;border-radius:3px;font-size:11px;">{escape(t)}</span>'
                for t in tags if t not in ("monitoring", "alert")
            )
            anomaly_rows += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #1e1e2e;color:#8a8aa0;font-size:12px;white-space:nowrap;vertical-align:top;">{escape(str(created))}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #1e1e2e;color:#e0e0e0;font-size:13px;">{content}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #1e1e2e;vertical-align:top;">{tag_badges}</td>
            </tr>"""

    # Container list
    container_rows = ""
    for c in docker.get("healthy", []):
        container_rows += f'<span style="display:inline-block;background:#1a3a2a;color:#2ecc71;padding:2px 8px;border-radius:3px;font-size:11px;margin:2px;">{escape(c["name"])}</span> '
    for c in docker.get("unhealthy", []):
        container_rows += f'<span style="display:inline-block;background:#3a1a1a;color:#e74c3c;padding:2px 8px;border-radius:3px;font-size:11px;margin:2px;">{escape(c["name"])}</span> '
    for c in docker.get("restarting", []):
        container_rows += f'<span style="display:inline-block;background:#3a2a1a;color:#f39c12;padding:2px 8px;border-radius:3px;font-size:11px;margin:2px;">{escape(c["name"])}</span> '

    disk_pct = disk.get("used_percent", 0)
    mem_pct = memory.get("used_percent", 0)
    gpu_pct = gpu.get("used_percent", 0)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0e0e16;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:20px;">

  <!-- Header -->
  <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:8px;padding:20px 24px;margin-bottom:16px;">
    <div style="display:flex;align-items:center;justify-content:space-between;">
      <div>
        <div style="font-size:18px;font-weight:600;color:#e0e0e0;">nobrainr monitoring</div>
        <div style="font-size:12px;color:#6a6a80;margin-top:4px;">{escape(machine)} &middot; {escape(now_str)}</div>
      </div>
      <div style="background:{status_color}22;color:{status_color};padding:6px 14px;border-radius:6px;font-size:13px;font-weight:500;">
        {status_icon} {status_text}
      </div>
    </div>
  </div>

  <!-- Resource Gauges -->
  <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:8px;padding:20px 24px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:600;color:#8a8aa0;text-transform:uppercase;letter-spacing:1px;margin-bottom:16px;">System Resources</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;width:80px;color:#8a8aa0;font-size:12px;">Disk</td>
        <td style="padding:6px 12px;">{_bar(disk_pct, 75, 85)}</td>
        <td style="padding:6px 0;width:90px;text-align:right;color:{_pct_color(disk_pct, 75, 85)};font-size:13px;font-weight:500;">{disk_pct:.0f}%&ensp;<span style="color:#6a6a80;font-weight:400;font-size:11px;">{disk.get('free_gb', '?')} GB free</span></td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#8a8aa0;font-size:12px;">RAM</td>
        <td style="padding:6px 12px;">{_bar(mem_pct, 80, 90)}</td>
        <td style="padding:6px 0;text-align:right;color:{_pct_color(mem_pct, 80, 90)};font-size:13px;font-weight:500;">{mem_pct:.0f}%&ensp;<span style="color:#6a6a80;font-weight:400;font-size:11px;">{memory.get('available_gb', '?')} GB avail</span></td>
      </tr>
      {"" if not gpu else f'''<tr>
        <td style="padding:6px 0;color:#8a8aa0;font-size:12px;">GPU</td>
        <td style="padding:6px 12px;">{_bar(gpu_pct, 85, 95)}</td>
        <td style="padding:6px 0;text-align:right;color:{_pct_color(gpu_pct, 85, 95)};font-size:13px;font-weight:500;">{gpu_pct:.0f}%&ensp;<span style="color:#6a6a80;font-weight:400;font-size:11px;">{gpu.get("total_mb", 0) - gpu.get("used_mb", 0):.0f} MB free</span></td>
      </tr>'''}
    </table>
  </div>

  <!-- Containers -->
  <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:8px;padding:20px 24px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:600;color:#8a8aa0;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">
      Containers
      <span style="color:#6a6a80;font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px;font-size:12px;">
        <span style="color:#2ecc71;">{healthy_count}</span> healthy{f' &middot; <span style="color:#e74c3c;">{unhealthy_count}</span> unhealthy' if unhealthy_count else ''}{f' &middot; <span style="color:#f39c12;">{restarting_count}</span> restarting' if restarting_count else ''}
      </span>
    </div>
    <div>{container_rows}</div>
  </div>

  <!-- Anomalies (only if any) -->
  {"" if not anomalies else f'''
  <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:8px;padding:20px 24px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:600;color:#e74c3c;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">
      Anomalies &middot; Last 24h
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <th style="text-align:left;padding:6px 12px;color:#6a6a80;font-size:11px;border-bottom:1px solid #1e1e2e;">Time</th>
        <th style="text-align:left;padding:6px 12px;color:#6a6a80;font-size:11px;border-bottom:1px solid #1e1e2e;">Details</th>
        <th style="text-align:left;padding:6px 12px;color:#6a6a80;font-size:11px;border-bottom:1px solid #1e1e2e;">Tags</th>
      </tr>
      {anomaly_rows}
    </table>
  </div>
  '''}

  <!-- Footer -->
  <div style="text-align:center;color:#4a4a60;font-size:11px;padding:8px 0;">
    nobrainr monitoring &middot; {escape(machine)}
  </div>

</div>
</body>
</html>"""


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

    # Post-filter: query_memories uses tag overlap (&&), so ensure BOTH tags present
    required_tags = {"monitoring", "alert"}
    recent_anomalies = [
        m for m in recent_anomalies
        if required_tags.issubset(set(m.get("tags") or []))
    ]

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

    # Gather current resource snapshot for the email
    resources = await check_system_resources()
    docker = await check_docker_health(track_state=False)

    # Only send email when there are actual problems
    has_problems = bool(
        recent
        or resources.get("warnings")
        or docker.get("unhealthy")
        or docker.get("restarting")
        or docker.get("missing")
        or docker.get("oom_killed")
    )
    if not has_problems:
        return {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "status": "skipped",
            "reason": "all_clear",
        }

    problem_count = len(recent) + len(resources.get("warnings", []))
    subject = f"[{machine}] {problem_count} issue{'s' if problem_count != 1 else ''} ({now_str})"

    body = _build_html_digest(
        machine=machine,
        now_str=now_str,
        anomalies=recent,
        resources=resources,
        docker=docker,
    )

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
    """Send an HTML email via SMTP (runs in thread executor).

    Port handling:
    - 25: plain SMTP (no TLS)
    - 465: implicit TLS (SMTP_SSL)
    - 587: explicit TLS (SMTP + STARTTLS)
    """
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.monitoring_smtp_from or settings.monitoring_smtp_user
    msg["To"] = settings.monitoring_smtp_to

    recipients = [
        r.strip() for r in settings.monitoring_smtp_to.split(",") if r.strip()
    ]

    port = settings.monitoring_smtp_port

    if port == 465:
        # Implicit TLS
        with smtplib.SMTP_SSL(settings.monitoring_smtp_host, port) as server:
            if settings.monitoring_smtp_user and settings.monitoring_smtp_password:
                server.login(settings.monitoring_smtp_user, settings.monitoring_smtp_password)
            server.sendmail(msg["From"], recipients, msg.as_string())
    else:
        # Port 25 (plain) or 587 (STARTTLS)
        with smtplib.SMTP(settings.monitoring_smtp_host, port) as server:
            server.ehlo()
            if port != 25:
                server.starttls()
                server.ehlo()
            if settings.monitoring_smtp_user and settings.monitoring_smtp_password:
                server.login(settings.monitoring_smtp_user, settings.monitoring_smtp_password)
            server.sendmail(msg["From"], recipients, msg.as_string())
