"""Automation bridge (Zapier / Pipedream / Make).

The bridge maps the Superprofile booking event to the normalised JSON (brief §5)
and posts it to the same endpoint with the same shared-secret signature.
If the bridge forwards Superprofile's own fields instead, fall back to that parser.
"""
from __future__ import annotations

from app.bookings.adapters.base import NormalisedBooking
from app.bookings.adapters.superprofile_webhook import SuperprofileWebhookAdapter


class ZapierAdapter:
    source = "zapier"

    def parse(self, payload: dict) -> NormalisedBooking:
        if "patient" in payload and "slot_start" in payload and "external_id" in payload:
            data = dict(payload)
            data["source"] = self.source
            data.pop("assigned_doctor", None)
            nb = NormalisedBooking.model_validate(data)
            nb.raw = payload
            return nb
        nb = SuperprofileWebhookAdapter().parse(payload)
        nb.source = self.source
        return nb
