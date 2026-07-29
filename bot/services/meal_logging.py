"""Meal logging service."""

from __future__ import annotations

import datetime

def infer_meal_type(local_time: datetime.time) -> str:
    """Infer the current meal type based on local time.

    [04:00, 11:00) breakfast
    [11:00, 16:00) lunch
    [16:00, 22:00) dinner
    otherwise snack
    """
    if datetime.time(4, 0) <= local_time < datetime.time(11, 0):
        return "breakfast"
    if datetime.time(11, 0) <= local_time < datetime.time(16, 0):
        return "lunch"
    if datetime.time(16, 0) <= local_time < datetime.time(22, 0):
        return "dinner"
    return "snack"
