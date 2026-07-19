"""Rendering regressions for user-controlled chart labels."""

from __future__ import annotations

from datetime import date

import matplotlib.image as mpimg

from bot import charts


def _png_dimensions(buffer) -> tuple[int, int]:
    buffer.seek(0)
    pixels = mpimg.imread(buffer, format="png")
    return pixels.shape[1], pixels.shape[0]


def test_study_chart_handles_mathtext_and_many_subjects() -> None:
    today = date(2026, 7, 19)
    logs = [
        {
            "subject": f"$\\unknown$ Subject {index}" + "x" * 200,
            "duration_min": index + 1,
            "local_date": today,
        }
        for index in range(25)
    ]

    buffer = charts.study_chart(logs, days=7, end_date=today)

    width, height = _png_dimensions(buffer)
    assert width + height <= 10_000
    assert max(width, height) / min(width, height) <= 20


def test_habit_chart_bounds_legacy_labels_to_valid_photo_dimensions() -> None:
    today = date(2026, 7, 19)
    names = [f"$\\unknown$ {'x' * 1_000} {index}" for index in range(49)]
    ids = list(range(1, 50))

    buffer = charts.habits_chart(names, ids, [], days=14, end_date=today)

    width, height = _png_dimensions(buffer)
    assert width + height <= 10_000
    assert max(width, height) / min(width, height) <= 20


def test_empty_charts_generate_valid_png() -> None:
    today = date(2026, 7, 19)
    
    study_buf = charts.study_chart([], days=7, end_date=today)
    assert study_buf is not None
    _png_dimensions(study_buf)
    
    gym_buf = charts.gym_chart([], days=7, end_date=today)
    assert gym_buf is not None
    _png_dimensions(gym_buf)
    
    diet_buf = charts.diet_chart([], days=7, end_date=today)
    assert diet_buf is not None
    _png_dimensions(diet_buf)
    
    habit_buf = charts.habits_chart([], [], [], days=7, end_date=today)
    assert habit_buf is not None
    _png_dimensions(habit_buf)


def test_single_data_point_charts_generate_valid_png() -> None:
    today = date(2026, 7, 19)
    
    study_buf = charts.study_chart(
        [{"subject": "Math", "duration_min": 30, "local_date": today}], 
        days=7, end_date=today
    )
    _png_dimensions(study_buf)
    
    gym_buf = charts.gym_chart(
        [{"exercise": "Squat", "sets": 3, "reps": 5, "weight_kg": 100, "local_date": today}], 
        days=7, end_date=today
    )
    _png_dimensions(gym_buf)
    
    diet_buf = charts.diet_chart(
        [{"calories": 500, "protein_g": 20, "carbs_g": 50, "fat_g": 10, "local_date": today}], 
        days=7, end_date=today
    )
    _png_dimensions(diet_buf)
    
    habit_buf = charts.habits_chart(
        ["Read"], [1], [{"habit_id": 1, "log_date": today.isoformat()}], 
        days=7, end_date=today
    )
    _png_dimensions(habit_buf)
