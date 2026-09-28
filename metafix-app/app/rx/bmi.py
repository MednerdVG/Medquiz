"""BMI with the Asian-Indian classification (brief §11 rule 6).

Missing or non-positive input returns None — never guess.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

# (lower bound inclusive, label) — checked from the top down.
ASIAN_INDIAN_CLASSES = [
    (35.0, "Obesity III"),
    (30.0, "Obesity II"),
    (25.0, "Obesity I"),
    (23.0, "Overweight"),
    (18.5, "Normal"),
    (0.0, "Underweight"),
]
SUB_LABEL = "(Asian-Indian cut-off)"


def compute_bmi(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[float]:
    if not weight_kg or not height_cm or weight_kg <= 0 or height_cm <= 0:
        return None
    m = height_cm / 100.0
    raw = Decimal(str(weight_kg)) / (Decimal(str(m)) * Decimal(str(m)))
    return float(raw.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def bmi_class(bmi: float) -> str:
    for lower, label in ASIAN_INDIAN_CLASSES:
        if bmi >= lower:
            return label
    return "Underweight"


def bmi_cell(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[dict]:
    bmi = compute_bmi(weight_kg, height_cm)
    if bmi is None:
        return None
    return {"label": "BMI", "value": f"{bmi:.1f} — {bmi_class(bmi)}", "sub": SUB_LABEL}
