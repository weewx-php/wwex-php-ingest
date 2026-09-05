"""Bounded fair batching and explicit, durable per-event acknowledgement."""

import json
import logging
import random
import time

from .config import ConfigError
from .protocol import ProtocolError, decode_response, envelope
from .transport import TransportError

log = logging.getLogger(__name__)


def backoff(attempt, base, maximum):
    ceiling = min(maximum, base * 2 ** min(attempt, 20))
    return random.uniform(ceiling / 2, ceiling)


class Uploader:
    def __init__(self, config, spools, client):
        self.config, self.spools, self.client = config, spools, client
        self.cursor = 0
        self.packet_limit = min(
            [config.max_packets]
            + [s.get_meta("upload_max_packets", config.max_packets) for s in self.spools]
        )
        self.byte_limit = min(
            [config.max_bytes]
            + [s.get_meta("upload_max_bytes", config.max_bytes) for s in self.spools]
        )
        self.failures = 0
        self.next_attempt = max((s.get_meta("upload_next_attempt", 0) for s in spools), default=0)

    def _batch(self, now):
        if not self.spools:
            return []
        ordered = self.spools[self.cursor :] + self.spools[: self.cursor]
        self.cursor = (self.cursor + 1) % len(self.spools)
        queues = [
            (s, iter(s.candidates(now, self.packet_limit, s.get_meta("prefer_newest", False))))
            for s in ordered
        ]
        chosen, packets = [], []
        while queues and len(chosen) < self.packet_limit:
            remaining = []
            for spool, rows in queues:
                row = next(rows, None)
                if row is None:
                    continue
                remaining.append((spool, rows))
                packet = json.loads(row["payload"])
                if len(envelope(self.config.collector_id, [*packets, packet])) > self.byte_limit:
                    if len(envelope(self.config.collector_id, [packet])) > self.byte_limit:
                        spool.defer(row["event_id"], "payload_too_large", 0, quarantine=True)
                    continue
                chosen.append((spool, row, packet))
                packets.append(packet)
                if len(chosen) == self.packet_limit:
                    break
            queues = remaining
        for spool in {item[0] for item in chosen}:
            spool.set_meta("prefer_newest", not spool.get_meta("prefer_newest", False))
        return chosen

    def _global_delay(self, reason, now, minimum=0):
        self.failures += 1
        delay = max(
            minimum,
            backoff(self.failures, max(self.config.send_interval, 1), self.config.backoff_max),
        )
        self.next_attempt = now + delay
        for spool in self.spools:
            with spool.transaction():
                spool.set_meta("upload_error", reason)
                spool.set_meta("upload_next_attempt", self.next_attempt)
                spool.set_meta("upload_max_packets", self.packet_limit)
                spool.set_meta("upload_max_bytes", self.byte_limit)
        log.warning("upload: %s; retry in %.1fs", reason, delay)

    def tick(self, now=None):
        from .sensor_sources import discover_spools

        discover_spools(self.config, self.spools)
        now = time.time() if now is None else now
        if now < self.next_attempt:
            return 0
        batch = self._batch(now)
        if not batch:
            self.next_attempt = now + self.config.send_interval
            return 0
        packets = [x[2] for x in batch]
        try:
            response = self.client.send(envelope(self.config.collector_id, packets))
        except (TransportError, ConfigError) as exc:
            self._global_delay(str(exc), now)
            return 0
        if response.status != 200:
            if response.status == 413:
                if len(batch) > 1:
                    self.packet_limit = max(1, len(batch) // 2)
                else:
                    batch[0][0].defer(batch[0][1]["event_id"], "payload_too_large", 0, True)
            permanent = (
                response.status in (400, 401, 403, 404, 405, 415) or 300 <= response.status < 400
            )
            self._global_delay(
                f"http_{response.status}", now, max(response.retry_after, 300 if permanent else 0)
            )
            return 0
        try:
            results, limits = decode_response(response.body, packets)
        except ProtocolError as exc:
            self._global_delay(str(exc), now)
            return 0
        self.failures = 0
        self.next_attempt = now + max(self.config.send_interval, response.retry_after)
        if isinstance(limits, dict):
            for key, attr, configured in (
                ("max_packets", "packet_limit", self.config.max_packets),
                ("max_bytes", "byte_limit", self.config.max_bytes),
            ):
                if type(limits.get(key)) is int and 0 < limits[key] <= configured:
                    setattr(self, attr, min(getattr(self, attr), limits[key]))
        confirmed = 0
        for spool, row, packet in batch:
            eid = packet["event_id"]
            result = results.get(eid, {"status": "missing", "reason": "missing_ack"})
            status = result["status"]
            if status in ("stored", "duplicate"):
                spool.acknowledge(eid, now)
                confirmed += 1
            else:
                reason = result["reason"] or status
                delay = max(
                    response.retry_after,
                    60 if status == "pending" else 1,
                    backoff(
                        row["attempts"] + 1, self.config.send_interval, self.config.backoff_max
                    ),
                )
                # Rejected events remain immutable and visible; an explicit retry can requeue them.
                spool.defer(eid, reason, now + delay, quarantine=status == "rejected")
                if status == "pending" or reason == "station_blocked":
                    spool.set_meta(
                        "station_upload_after",
                        max(spool.get_meta("station_upload_after", 0), now + max(delay, 60)),
                    )
                log.warning("station %s: %s", spool.station.key, reason)
        for spool in self.spools:
            spool.set_meta("upload_next_attempt", self.next_attempt)
            spool.set_meta("upload_max_packets", self.packet_limit)
            spool.set_meta("upload_max_bytes", self.byte_limit)
            if isinstance(limits, dict):
                # Persist only defined integer limits, never arbitrary receiver-provided text.
                spool.set_meta(
                    "receiver_max_age_seconds",
                    limits.get("max_age_seconds")
                    if type(limits.get("max_age_seconds")) is int
                    and 0 < limits["max_age_seconds"] <= 604800
                    else None,
                )
        return confirmed
