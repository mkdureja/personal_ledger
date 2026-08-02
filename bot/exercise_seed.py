"""The shared starter list of exercises, grouped by muscle group.

Typing an exercise name is the slowest possible way to log one, and it spells it
differently every time — "Chest press", "chest-press", "Chestpress" become three
different exercises in the history and none of them shows a trend. A tappable
list fixes both: two taps to the exercise, one canonical name for ever after.

Deliberately a *starter* list, not a complete one. It covers the movements most
home and commercial-gym sessions are built from; anything missing is added by the
user in the flow itself and lands in the same table under their own ``user_id``.
Nothing here is prescriptive — an unused entry costs one row.

``seed_exercises`` upserts by ``name_key`` among the shared rows, so editing a
name here renames it everywhere without touching a user's own additions or any
logged history (a log stores the exercise name as text at save time).
"""

from __future__ import annotations

#: Group key -> (emoji, label). The key is stored; the label is display-only, so
#: renaming a group never rewrites a row.
MUSCLE_GROUPS: dict[str, tuple[str, str]] = {
    "chest": ("🫀", "Chest"),
    "back": ("🔙", "Back"),
    "legs": ("🦵", "Legs"),
    "shoulders": ("🤸", "Shoulders"),
    "arms": ("💪", "Arms"),
    "core": ("🧘", "Core / Abs"),
    "cardio": ("🏃", "Cardio / HIIT"),
}

#: group_key -> exercise display names, in the order they are shown.
SEED_EXERCISES: dict[str, tuple[str, ...]] = {
    "chest": (
        "Bench press",
        "Incline bench press",
        "Dumbbell press",
        "Incline dumbbell press",
        "Chest press",
        "Cable fly",
        "Push-up",
        "Dips",
    ),
    "back": (
        "Deadlift",
        "Lat pulldown",
        "Pull-up",
        "Barbell row",
        "Dumbbell row",
        "Seated cable row",
        "T-bar row",
        "Face pull",
    ),
    "legs": (
        "Squat",
        "Front squat",
        "Leg press",
        "Lunge",
        "Romanian deadlift",
        "Leg extension",
        "Leg curl",
        "Calf raise",
    ),
    "shoulders": (
        "Overhead press",
        "Dumbbell shoulder press",
        "Lateral raise",
        "Front raise",
        "Rear delt fly",
        "Upright row",
        "Shrug",
    ),
    "arms": (
        "Barbell curl",
        "Dumbbell curl",
        "Hammer curl",
        "Preacher curl",
        "Triceps pushdown",
        "Overhead triceps extension",
        "Skull crusher",
        "Close-grip bench press",
    ),
    "core": (
        "Plank",
        "Crunch",
        "Hanging leg raise",
        "Russian twist",
        "Cable crunch",
        "Mountain climber",
        "Ab wheel rollout",
    ),
    "cardio": (
        "Treadmill run",
        "Cycling",
        "Rowing machine",
        "Elliptical",
        "Stair climber",
        "Jump rope",
        "HIIT circuit",
        "Swimming",
    ),
}


def seed_rows() -> list[tuple[str, str]]:
    """Flatten the seed into ``(group_key, display_name)`` pairs."""
    return [
        (group, name)
        for group, names in SEED_EXERCISES.items()
        for name in names
    ]


def group_label(group_key: str) -> str:
    """"💪 Arms" for a known key, else the key itself (never raises)."""
    emoji, label = MUSCLE_GROUPS.get(group_key, ("🏋️", group_key.title()))
    return f"{emoji} {label}"
