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
                if used_pct > 99:
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


async def check_pipeline_anomalies() -> list[str]:
    """Detect memory-pipeline pathologies that container health checks miss.

    Added 2026-06-11 after the chatgpt_distill timeout loop ran for ~4 weeks
    unnoticed: the job timed out on 80 of 94 runs while re-inserting ~10k
    duplicate memories per week, and every container stayed "healthy".

    Two signals:
    - A scheduler job whose recent runs are mostly timeouts (>=4 of last 5).
      One timeout is long-tail variance; a streak means the job never
      completes and is likely redoing (and re-storing) the same work.
    - Insert volume per source_type far above its 30-day norm (>3x the daily
      median and >500/day), measured on inserted_at so backdated created_at
      can't mask a flood.
    """
    from nobrainr.db.pool import get_pool

    warnings: list[str] = []
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            timeout_rows = await conn.fetch(
                """
                SELECT task_name,
                       count(*) FILTER (WHERE status = 'timeout') AS timeouts,
                       count(*) AS runs
                FROM (
                    SELECT task_name, status,
                           row_number() OVER (PARTITION BY task_name ORDER BY created_at DESC) AS rn
                    FROM scheduler_runs
                    WHERE status IN ('ok', 'timeout', 'error')
                ) recent
                WHERE rn <= 5
                GROUP BY task_name
                HAVING count(*) FILTER (WHERE status = 'timeout') >= 4
                """,
            )
            for row in timeout_rows:
                warnings.append(
                    f"Scheduler job '{row['task_name']}' timed out on "
                    f"{row['timeouts']} of its last {row['runs']} runs — it is "
                    f"likely looping without completing (check for re-processed work)"
                )

            flood_rows = await conn.fetch(
                """
                WITH daily AS (
                    SELECT source_type, inserted_at::date AS day, count(*) AS n
                    FROM memories
                    WHERE inserted_at > now() - interval '30 days'
                    GROUP BY 1, 2
                ),
                norm AS (
                    SELECT source_type,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY n) AS median_n
                    FROM daily
                    WHERE day < current_date
                    GROUP BY source_type
                )
                SELECT d.source_type, d.n, norm.median_n
                FROM daily d
                JOIN norm USING (source_type)
                WHERE d.day = current_date
                  AND d.n > 500
                  AND d.n > 3 * norm.median_n
                """,
            )
            for row in flood_rows:
                warnings.append(
                    f"Insert flood: source_type '{row['source_type']}' added "
                    f"{row['n']} memories today vs 30d median {int(row['median_n'])}/day"
                )
    except Exception:
        logger.exception("Pipeline anomaly check failed")
    return warnings


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

    # Process memory-pipeline anomalies (job timeout loops, insert floods)
    for warning in await check_pipeline_anomalies():
        anomalies.append(warning)
        logger.warning("Monitoring alert: %s", warning)

    # Anomalies go to logs and email digest only — not to the knowledge base.
    # System health metrics are operational noise, not knowledge worth recalling.
    if anomalies:
        for anomaly in anomalies:
            logger.warning("Monitoring anomaly (not stored): %s", anomaly)

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


async def send_knowledge_digest() -> dict:
    """Daily knowledge digest — the wonderful one.

    Distinct from send_email_digest (which is alerting-only and silent when
    all-clear). This one always sends once a day with:
      - Top 3 synthesis insights from last 24h
      - Memory of the day (high-quality random pick)
      - Stale memories needing re-verification (count + top 3 oldest)
      - Distillation/extraction progress
      - Bridge connections discovered (new cross-community links)
      - Trending topics from interest_signals
    """
    if not settings.monitoring_email_enabled:
        return {"status": "skipped", "reason": "email_disabled"}
    if not settings.monitoring_smtp_host or not settings.monitoring_smtp_to:
        return {"status": "skipped", "reason": "smtp_not_configured"}

    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        recent_insights = await conn.fetch(
            """
            SELECT id, content, summary, confidence, quality_score, created_at, metadata
            FROM memories
            WHERE source_type = 'synthesis'
              AND created_at > NOW() - INTERVAL '24 hours'
              AND superseded_by IS NULL
              AND category != '_archived'
            ORDER BY COALESCE(quality_score, 0.5) DESC, confidence DESC
            LIMIT 3
            """
        )
        memory_of_day = await conn.fetchrow(
            """
            SELECT id, content, summary, source_type, quality_score, created_at
            FROM memories
            WHERE quality_score >= 0.6 AND superseded_by IS NULL
              AND category != '_archived'
              AND source_type IN ('manual','affine_memos','docx','session','agent_learning','synthesis')
            ORDER BY (quality_score * (0.5 + 0.5 * random())) DESC
            LIMIT 1
            """
        )
        stale_count = await conn.fetchval("SELECT COUNT(*) FROM stale_memories")
        stale_top = await conn.fetch(
            "SELECT id, summary_excerpt, claim_kind, staleness_days FROM stale_memories LIMIT 3"
        )
        progress = await conn.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM memories WHERE extraction_status='pending') AS extr_pending,
              (SELECT COUNT(*) FROM memories WHERE quality_score IS NULL AND category != '_archived') AS unscored,
              (SELECT COUNT(*) FROM conversations_raw WHERE embedding IS NULL) AS conv_unembedded,
              (SELECT COUNT(*) FROM memories) AS total
            """
        )
        # entity_relations columns are source_entity_id / target_entity_id /
        # relationship_type — the older source_id / target_id / relation_type
        # names were renamed in the 2026-04 schema refactor and never updated
        # here, causing 1-3 failures per day on this digest since.
        new_bridges = await conn.fetch(
            """
            SELECT er.source_entity_id AS source_id,
                   er.target_entity_id AS target_id,
                   er.relationship_type AS relation_type,
                   er.created_at,
                   se.canonical_name AS src_name, te.canonical_name AS tgt_name
            FROM entity_relations er
            JOIN entities se ON se.id = er.source_entity_id
            JOIN entities te ON te.id = er.target_entity_id
            WHERE er.created_at > NOW() - INTERVAL '24 hours'
              AND se.community_id IS NOT NULL
              AND te.community_id IS NOT NULL
              AND se.community_id != te.community_id
            ORDER BY er.created_at DESC
            LIMIT 5
            """
        )
        trending = await conn.fetch(
            """
            SELECT topic, COUNT(*) AS hits, MAX(created_at) AS last
            FROM interest_signals
            WHERE created_at > NOW() - INTERVAL '7 days'
            GROUP BY topic
            ORDER BY hits DESC, last DESC
            LIMIT 8
            """
        )

    machine = settings.source_machine or socket.gethostname()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    parts = [f"<h2 style='color:#222'>Knowledge digest · {today}</h2>"]
    if recent_insights:
        parts.append("<h3>✦ Insights from the last 24h</h3><ul>")
        for r in recent_insights:
            text = (r["summary"] or r["content"])[:300]
            parts.append(f"<li>{escape(text)}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p style='color:#888'>No new syntheses today. The graph is digesting.</p>")
    if memory_of_day:
        text = (memory_of_day["summary"] or memory_of_day["content"])[:400]
        parts.append(
            f"<h3>📓 Memory of the day · {escape(memory_of_day['source_type'] or '')}</h3>"
            f"<blockquote style='border-left:3px solid #999;padding-left:10px;color:#444'>{escape(text)}</blockquote>"
        )
    parts.append(f"<h3>🌱 Progress</h3><ul>"
                 f"<li>Total memories: <b>{progress['total']:,}</b></li>"
                 f"<li>Extraction pending: {progress['extr_pending']:,}</li>"
                 f"<li>Quality unscored: {progress['unscored']:,}</li>"
                 f"<li>Raw conversations awaiting embed: {progress['conv_unembedded']:,}</li>"
                 f"</ul>")
    if stale_count and stale_count > 0:
        parts.append(f"<h3>⚠ {stale_count} memories due for re-verification</h3><ul>")
        for s in stale_top:
            parts.append(
                f"<li><code>{escape(s['claim_kind'] or '?')}</code> · {s['staleness_days']}d window · "
                f"{escape((s['summary_excerpt'] or '')[:120])}</li>"
            )
        parts.append("</ul>")
    if new_bridges:
        parts.append("<h3>🔗 New cross-community connections</h3><ul>")
        for b in new_bridges:
            parts.append(
                f"<li>{escape(b['src_name'] or '')} <i>→ {escape(b['relation_type'] or '')} →</i> "
                f"{escape(b['tgt_name'] or '')}</li>"
            )
        parts.append("</ul>")
    if trending:
        parts.append("<h3>📈 Trending interests (7d)</h3><ul>")
        for t in trending:
            parts.append(f"<li>{escape(t['topic'] or '')} · {t['hits']} signals</li>")
        parts.append("</ul>")

    body = (
        "<html><body style='font-family:Inter,system-ui,sans-serif;max-width:680px;line-height:1.5'>"
        + "".join(parts)
        + f"<p style='color:#aaa;font-size:12px;margin-top:24px'>{machine} · {today} · nobrainr</p>"
        "</body></html>"
    )
    subject = f"[{machine}] Knowledge digest · {today}"

    try:
        await asyncio.to_thread(_send_smtp, subject, body)
        return {"status": "sent", "insights": len(recent_insights),
                "stale": int(stale_count or 0), "bridges": len(new_bridges)}
    except Exception as exc:
        logger.exception("send_knowledge_digest: SMTP send failed")
        return {"status": "error", "error": str(exc)}


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
