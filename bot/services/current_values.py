"""Pure helpers for the `Use current values` replay.

The signature below answers one question: *is the meal I am about to save still
the meal that was shown?* Between rendering a preview and tapping Save, a food's
nutrition can be edited or a portion renamed, which would silently change what
gets logged. Re-resolving inside the commit transaction and comparing signatures
turns that into a visible "review this again" instead.

It is a plain readable string, not a cryptographic digest: both sides are this
process, and the only adversary is a race with the user's own edits.
"""

from __future__ import annotations

from ..meal_models import CurrentValueDecision, CurrentValuePreview


def _number(value: float | None) -> str:
    """Render a nutrient/amount so equal values always compare equal."""
    if value is None:
        return "-"
    number = float(value)
    # Normalize -0.0 and integral floats to one spelling.
    if number == 0:
        return "0"
    return f"{number:.6g}"


def preview_signature(preview: CurrentValuePreview) -> str:
    """Fingerprint exactly the fields that would be persisted, in order."""
    parts = [preview.meal_type]
    for proposal in preview.items:
        item = proposal.proposed
        if proposal.decision is CurrentValueDecision.REMOVE or item is None:
            parts.append(f"{proposal.source_child_id}:{proposal.decision}:-")
            continue
        parts.append(
            ":".join(
                (
                    str(proposal.source_child_id),
                    str(proposal.decision),
                    str(item.source_type),
                    str(item.source_id),
                    str(item.source_provider or ""),
                    str(item.source_revision or ""),
                    item.display_name,
                    _number(item.entered_amount),
                    str(item.entered_unit or ""),
                    _number(item.resolved_base_amount),
                    str(item.resolved_base_unit or ""),
                    _number(item.calories),
                    _number(item.protein_g),
                    _number(item.carbs_g),
                    _number(item.fat_g),
                )
            )
        )
    return "|".join(parts)
