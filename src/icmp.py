"""ICMP ping with responseTime / packetLoss parsing (Phase 2.2)."""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class IcmpResult:
    success: bool
    response_time_ms: float | None
    packet_loss_pct: float | None
    checked_at: str
    error: str | None
    raw: str = ""

    @property
    def result_status(self) -> str:
        return "ONLINE" if self.success else "OFFLINE"


def _parse_windows(output: str) -> tuple[bool, float | None, float | None, str | None]:
    loss = None
    rtt = None
    m_loss = re.search(r"Lost\s*=\s*(\d+)\s*\((\d+)%\s*loss\)", output, re.I)
    if m_loss:
        loss = float(m_loss.group(2))
    m_avg = re.search(r"Average\s*=\s*(\d+)\s*ms", output, re.I)
    if m_avg:
        rtt = float(m_avg.group(1))
    else:
        m_time = re.search(r"time[=<](\d+)\s*ms", output, re.I)
        if m_time:
            rtt = float(m_time.group(1))
        elif re.search(r"time<\s*1ms", output, re.I):
            rtt = 0.5

    # Success if received at least one reply
    m_recv = re.search(r"Received\s*=\s*(\d+)", output, re.I)
    if m_recv and int(m_recv.group(1)) > 0:
        return True, rtt, loss if loss is not None else 0.0, None
    if re.search(r"Reply from", output, re.I):
        return True, rtt, loss if loss is not None else 0.0, None

    err = "Request timed out"
    for line in output.splitlines():
        line = line.strip()
        if line and (
            "timed out" in line.lower()
            or "unreachable" in line.lower()
            or "could not find host" in line.lower()
            or "transmit failed" in line.lower()
        ):
            err = line
            break
    return False, None, loss if loss is not None else 100.0, err


def _parse_unix(output: str, returncode: int) -> tuple[bool, float | None, float | None, str | None]:
    loss = None
    rtt = None
    m_loss = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", output, re.I)
    if m_loss:
        loss = float(m_loss.group(1))
    m_rtt = re.search(
        r"rtt [^=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)", output, re.I
    )
    if m_rtt:
        rtt = float(m_rtt.group(2))  # avg
    else:
        m_time = re.search(r"time=([\d.]+)\s*ms", output, re.I)
        if m_time:
            rtt = float(m_time.group(1))
    success = returncode == 0 and (loss is None or loss < 100)
    if success:
        return True, rtt, loss if loss is not None else 0.0, None
    return False, None, loss if loss is not None else 100.0, "ping failed"


def icmp_ping(
    host: str,
    *,
    count: int = 4,
    timeout_ms: int = 1500,
) -> IcmpResult:
    """Universal ICMP health check. Returns metrics for healthChecks storage."""
    system = platform.system().lower()
    try:
        if system == "windows":
            # -n count, -w timeout per reply (ms)
            cmd = ["ping", "-n", str(count), "-w", str(timeout_ms), host]
            timeout_sec = max(10, (count * (timeout_ms / 1000.0)) + 5)
        else:
            sec = max(1, int((timeout_ms + 999) / 1000))
            cmd = ["ping", "-c", str(count), "-W", str(sec), host]
            timeout_sec = max(10, count * sec + 5)

        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if system == "windows":
            ok, rtt, loss, err = _parse_windows(output)
        else:
            ok, rtt, loss, err = _parse_unix(output, proc.returncode)
        finished = utc_now()
        return IcmpResult(
            success=ok,
            response_time_ms=rtt,
            packet_loss_pct=loss,
            checked_at=finished,
            error=None if ok else (err or "ping failed"),
            raw=output[-2000:],
        )
    except subprocess.TimeoutExpired:
        return IcmpResult(
            False, None, 100.0, utc_now(), "ping subprocess timeout", ""
        )
    except Exception as e:
        return IcmpResult(False, None, 100.0, utc_now(), str(e), "")


def icmp_with_confirmation(
    host: str,
    *,
    count: int = 4,
    timeout_ms: int = 1500,
    confirm_count: int = 2,
) -> IcmpResult:
    """
    YES -> ONLINE.
    NO  -> CONFIRMATION (second ping); still fail => OFFLINE.
    """
    first = icmp_ping(host, count=count, timeout_ms=timeout_ms)
    if first.success:
        return first
    # Confirmation pass (user diagram: CONFIRMATION before OFFLINE)
    second = icmp_ping(host, count=confirm_count, timeout_ms=timeout_ms)
    if second.success:
        second.error = None
        return second
    second.error = second.error or first.error or "ping failed after confirmation"
    return second
