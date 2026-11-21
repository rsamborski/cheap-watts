"""SMTP email notifier for change-driven Deye TOU updates, EV alerts, and cycle failures."""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from src.config import Settings
from src.engine.diff import CycleDecision
from src.models import ChargingPlan, InverterStatus

logger = logging.getLogger("cheap_watts.notify.email")


def _parse_recipients(raw_to: str) -> list[str]:
    return [addr.strip() for addr in raw_to.split(",") if addr.strip()]


def build_cycle_email(
    plan: ChargingPlan,
    decision: CycleDecision,
    inverter: InverterStatus,
    from_email: str,
    to_email: str,
    tz_name: str = "Europe/Warsaw",
) -> EmailMessage | None:
    """Build an email summarizing actionable Deye TOU changes and/or new EV recommendations.

    Returns `None` strictly when neither Deye TOU schedule nor new EV alerts occurred.
    """
    if not decision.should_send_email:
        return None

    tz = ZoneInfo(tz_name)
    generated_local = plan.generated_at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
    ev_names = ", ".join(r.display_name for r in decision.notifiable_ev_recommendations)

    if decision.schedule_diff.has_changes and decision.notifiable_ev_recommendations:
        topic = f"Deye TOU updated + EV charging recommended ({ev_names})"
    elif decision.schedule_diff.has_changes:
        topic = "Deye TOU schedule updated"
    else:
        topic = f"EV charging recommended ({ev_names})"

    subject = f"{decision.email_subject_prefix}cheap-watts: {topic}"

    body_lines: list[str] = []
    if decision.dry_run:
        body_lines.append(
            "[DRY-RUN] Simulation mode active — NO changes were written to the Deye inverter."
        )
        body_lines.append("")

    body_lines.extend(
        [
            f"cheap-watts Optimization Summary ({generated_local})",
            "=" * 58,
            f"Deye Battery SoC:   {inverter.battery_soc}% (PV now: {inverter.pv_power_w:.0f}W)",
            f"24h Expected PV:    {plan.expected_pv_kwh:.2f} kWh "
            f"(solar_sufficient={plan.solar_sufficient})",
            f"Grid Deficit:       {plan.grid_deficit_kwh:.2f} kWh",
            f"Active EV Charging: {', '.join(plan.ev_actively_charging) or 'none'}",
            "",
        ]
    )

    if decision.notifiable_ev_recommendations:
        body_lines.append("EV Charging Recommendations")
        body_lines.append("-" * 32)
        for rec in decision.notifiable_ev_recommendations:
            avg_price = (
                sum(s.price_pln_kwh for s in rec.slots) / len(rec.slots) if rec.slots else 0.0
            )
            if rec.mode == "solar_slow":
                amps_label = f"{rec.recommended_amps}A slow-charge on PV surplus"
                body_lines.append(
                    f"  * {rec.display_name}: SoC {rec.soc_pct}% (<= {rec.threshold_pct}%) -> "
                    f"Window {rec.recommended_window} ({amps_label}, avg {avg_price:.4f} PLN/kWh)"
                )
            else:
                body_lines.append(
                    f"  * {rec.display_name}: SoC {rec.soc_pct}% (<= {rec.threshold_pct}%) -> "
                    f"Window {rec.recommended_window} (cheapest grid tariff, "
                    f"avg {avg_price:.4f} PLN/kWh)"
                )
        body_lines.append("")

    if decision.schedule_diff.has_changes:
        action_verb = "Planned (DRY-RUN)" if decision.dry_run else "Applied"
        body_lines.append(f"Deye TOU Schedule Changes ({action_verb})")
        body_lines.append("-" * 32)
        # body_lines.append(decision.schedule_diff.format_audit_log())
        # body_lines.append("")
        body_lines.append("Target 6-Slot Deye TOU Schedule:")
        for slot in plan.target_tou_slots:
            gc_str = "ON " if slot.grid_charge else "OFF"
            body_lines.append(
                f"  #{slot.slot_index}: {slot.start_time}-{slot.end_time} | "
                f"target_soc={slot.target_soc}% | grid_charge={gc_str}"
            )
        body_lines.append("")

    body_lines.append(f"Rationale: {plan.rationale}")

    recipients = _parse_recipients(to_email)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email.strip()
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(body_lines) + "\n")
    return msg


def build_failure_email(
    run_id: str,
    error_summary: str,
    from_email: str,
    to_email: str,
    dry_run: bool = False,
    tz_name: str = "Europe/Warsaw",
    now: datetime | None = None,
) -> EmailMessage:
    """Build an alert email for an unhandled cycle failure."""
    tz = ZoneInfo(tz_name)
    ts = (now or datetime.now(tz=UTC)).astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
    prefix = "[DRY-RUN] " if dry_run else ""

    lines: list[str] = []
    if dry_run:
        lines.append("[DRY-RUN] Simulation cycle encountered an error.")
        lines.append("")
    lines.extend(
        [
            f"cheap-watts Cycle Failure ({ts})",
            "=" * 48,
            f"Run ID: {run_id}",
            f"Error:  {error_summary}",
            "",
            "Check logs/cheap-watts.log for the full traceback.",
        ]
    )

    recipients = _parse_recipients(to_email)
    msg = EmailMessage()
    msg["Subject"] = f"{prefix}cheap-watts: Optimization cycle failed (run {run_id})"
    msg["From"] = from_email.strip()
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(lines) + "\n")
    return msg


class SmtpNotifier:
    """Standard SMTP client supporting STARTTLS (`587`) and implicit TLS (`465`)."""

    def __init__(
        self,
        settings: Settings,
        timeout: float = 15.0,
        smtp_factory: Callable[..., Any] | None = None,
        smtp_ssl_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._smtp_ssl_factory = smtp_ssl_factory or smtplib.SMTP_SSL

    @property
    def is_configured(self) -> bool:
        """Return True when SMTP host, sender, and recipient are configured."""
        return bool(
            self._settings.smtp_host
            and self._settings.smtp_from_email
            and self._settings.smtp_to_email
        )

    def send_cycle_notification(
        self,
        plan: ChargingPlan,
        decision: CycleDecision,
        inverter: InverterStatus,
    ) -> bool:
        """Send change-driven optimization email; returns True if delivered."""
        if not decision.should_send_email:
            return False
        if not self.is_configured:
            logger.warning(
                "Actionable changes detected, but SMTP is not configured; skipping email",
                extra={"event": "smtp_skipped_unconfigured", "dry_run": decision.dry_run},
            )
            return False

        assert self._settings.smtp_from_email is not None
        assert self._settings.smtp_to_email is not None
        msg = build_cycle_email(
            plan=plan,
            decision=decision,
            inverter=inverter,
            from_email=self._settings.smtp_from_email,
            to_email=self._settings.smtp_to_email,
            tz_name=self._settings.timezone,
        )
        if msg is None:  # pragma: no cover - guarded above
            return False

        self._deliver(msg)
        logger.info(
            "Sent cheap-watts notification email",
            extra={
                "event": "smtp_email_sent",
                "dry_run": decision.dry_run,
                "subject": str(msg["Subject"]),
            },
        )
        return True

    def send_failure_alert(
        self,
        run_id: str,
        error_summary: str,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Send an error alert email when configured, suppressing secondary SMTP failures."""
        if not self.is_configured:
            return False
        assert self._settings.smtp_from_email is not None
        assert self._settings.smtp_to_email is not None
        msg = build_failure_email(
            run_id=run_id,
            error_summary=error_summary,
            from_email=self._settings.smtp_from_email,
            to_email=self._settings.smtp_to_email,
            dry_run=dry_run,
            tz_name=self._settings.timezone,
            now=now,
        )
        try:
            self._deliver(msg)
            logger.info(
                "Sent cycle failure alert email",
                extra={"event": "smtp_failure_alert_sent", "run_id": run_id, "dry_run": dry_run},
            )
            return True
        except Exception as exc:
            logger.warning(
                "Failed to deliver cycle failure alert email",
                extra={"event": "smtp_failure_alert_error", "error": exc.__class__.__name__},
            )
            return False

    def _deliver(self, msg: EmailMessage) -> None:
        assert self._settings.smtp_host is not None
        host = self._settings.smtp_host.strip()
        port = self._settings.smtp_port
        use_tls = self._settings.smtp_use_tls
        ssl_ctx = ssl.create_default_context()

        if use_tls and port == 465:
            with self._smtp_ssl_factory(
                host,
                port,
                timeout=self._timeout,
                context=ssl_ctx,
            ) as server:
                self._authenticate_and_send(server, msg)
            return

        with self._smtp_factory(host, port, timeout=self._timeout) as server:
            server.ehlo()
            if use_tls:
                server.starttls(context=ssl_ctx)
                server.ehlo()
            self._authenticate_and_send(server, msg)

    def _authenticate_and_send(self, server: Any, msg: EmailMessage) -> None:
        if self._settings.smtp_username and self._settings.smtp_password is not None:
            server.login(
                self._settings.smtp_username.strip(),
                self._settings.smtp_password.get_secret_value(),
            )
        server.send_message(msg)
