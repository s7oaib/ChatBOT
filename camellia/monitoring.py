import logging
import os
import sys

from camellia.config import config

_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def get_logger(name: str) -> logging.Logger:
    """Returns a configured logger. Replaces bare print() so a real log
    aggregator (or `docker compose logs`) can filter by severity."""
    logger = logging.getLogger(name)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=getattr(logging, _LEVEL, logging.INFO),
            format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            stream=sys.stdout,
        )
        # httpx logs a line per outbound Meta call at INFO, which drowns out ours.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    return logger


log = get_logger("camellia")


def classify_instagram_delivery(
    *,
    http_status=None,
    meta_code=None,
    meta_subcode=None,
    exception_class=None,
    exception_message=None,
    success=False,
) -> str:
    """Classify one Instagram send without conflating recipient and provider failures."""
    if success:
        return "SUCCESS"
    code = str(meta_code) if meta_code is not None else ""
    subcode = str(meta_subcode) if meta_subcode is not None else ""
    if code == "100" and subcode == "2534014":
        return "PERMANENT_RECIPIENT_FAILURE"
    if http_status == 429 or code in {"4", "613"}:
        return "RATE_LIMIT"
    if http_status in {401, 403} or code in {"10", "190", "102"}:
        return "AUTH_FAILURE"
    if http_status is not None and int(http_status) >= 500:
        return "TRANSIENT_PROVIDER_FAILURE"
    exception_text = " ".join(str(value or "") for value in (exception_class, exception_message)).lower()
    if any(marker in exception_text for marker in ("timeout", "network", "connect", "transport", "dns")):
        return "NETWORK_FAILURE"
    return "OTHER_PROVIDER_FAILURE"


class DeliveryHealthMonitor:
    """
    Watches outbound Meta API calls and alerts the owner once when sending
    starts failing consistently.

    The failure this exists for is a silently expired WHATSAPP_ACCESS_TOKEN: the
    bot keeps accepting webhooks and looks healthy, but every reply is dropped,
    and nobody notices until a customer complains. Counts consecutive failures
    per channel so a single network blip does not page anyone.
    """

    def __init__(self, failure_threshold: int = 3):
        self.failure_threshold = failure_threshold
        self._consecutive_failures = {}
        self._alerted = set()

    def record_success(self, channel: str) -> None:
        if self._consecutive_failures.get(channel):
            log.info("Outbound delivery on %s recovered.", channel)
        self._consecutive_failures[channel] = 0
        self._alerted.discard(channel)

    def record_failure(
        self,
        channel: str,
        detail: str,
        *,
        classification: str = "OTHER_PROVIDER_FAILURE",
    ) -> None:
        if classification == "PERMANENT_RECIPIENT_FAILURE":
            log.warning(
                "Outbound delivery failed for one recipient on %s; provider health unchanged: %s",
                channel, detail,
            )
            return
        count = self._consecutive_failures.get(channel, 0) + 1
        self._consecutive_failures[channel] = count
        log.error("Outbound delivery failed on %s (%s consecutive): %s", channel, count, detail)

        if count >= self.failure_threshold and channel not in self._alerted:
            self._alerted.add(channel)
            self._send_owner_alert(channel, count, detail)

    def _send_owner_alert(self, channel: str, count: int, detail: str) -> None:
        message = (
            f"[CAMELLIA BOT ALERT] {count} consecutive send failures on {channel}. "
            f"Customers are NOT receiving replies. Most likely cause: expired "
            f"WHATSAPP_ACCESS_TOKEN. Last error: {detail[:200]}"
        )
        log.critical(message)

        # The log above is the monitoring record. Do not create a second
        # WhatsApp Cloud API dependency unless the separately approved human
        # escalation notification feature is explicitly enabled.
        if not config.HUMAN_ESCALATION_WHATSAPP_API_ENABLED:
            log.info(
                "Owner delivery alert not sent: external WhatsApp API notification is disabled."
            )
            return

        # Best-effort WhatsApp alert. If the credential itself is what broke,
        # this cannot get through — the CRITICAL log line above is the fallback.
        if not (config.WHATSAPP_ACCESS_TOKEN and config.WHATSAPP_PHONE_NUMBER_ID):
            return
        try:
            import httpx

            url = (
                f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/"
                f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
            )
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    url,
                    headers={"Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": config.HUMAN_ESCALATION_PHONE.lstrip("+"),
                        "type": "text",
                        "text": {"body": message},
                    },
                )
        except Exception as exc:
            log.error("Could not deliver health alert to owner: %s", exc)

    def snapshot(self) -> dict:
        return {
            "consecutive_failures": dict(self._consecutive_failures),
            "channels_alerting": sorted(self._alerted),
            "healthy": not self._alerted,
        }


delivery_monitor = DeliveryHealthMonitor()


class ResponseLatencyMonitor:
    """
    Tracks how long the bot takes to produce each reply.

    This exists so the bot can notice it has become slow and tell the customer,
    instead of leaving them watching an empty chat. Two things count as slow:
    the reply the customer is waiting on right now took too long, or the recent
    average has drifted above the threshold (a sustained slowdown, e.g. the AI
    provider degrading, rather than one unlucky request).
    """

    def __init__(self, slow_threshold_ms: float = 3000.0, window: int = 10):
        from collections import deque

        self.slow_threshold_ms = slow_threshold_ms
        self._samples = deque(maxlen=window)
        self._last_ms = 0.0

    def record(self, elapsed_ms: float) -> None:
        self._last_ms = elapsed_ms
        self._samples.append(elapsed_ms)
        if elapsed_ms >= self.slow_threshold_ms:
            log.warning(
                "Slow reply: %.0fms (threshold %.0fms)", elapsed_ms, self.slow_threshold_ms
            )

    def is_degraded(self) -> bool:
        """True if the customer is currently experiencing a slow bot."""
        if self._last_ms >= self.slow_threshold_ms:
            return True
        if not self._samples:
            return False
        return (sum(self._samples) / len(self._samples)) >= self.slow_threshold_ms

    def snapshot(self) -> dict:
        avg = (sum(self._samples) / len(self._samples)) if self._samples else 0.0
        return {
            "last_reply_ms": round(self._last_ms, 1),
            "avg_reply_ms": round(avg, 1),
            "samples": len(self._samples),
            "slow_threshold_ms": self.slow_threshold_ms,
            "degraded": self.is_degraded(),
        }


latency_monitor = ResponseLatencyMonitor(
    slow_threshold_ms=config.SLOW_REPLY_THRESHOLD_MS
)
