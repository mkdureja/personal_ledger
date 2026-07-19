"""
Chart generation for Ledger bot.

Uses matplotlib with Agg backend (headless-safe) and a dark theme.
Each function returns a BytesIO PNG buffer ready for reply_photo().
"""

from __future__ import annotations

import matplotlib
matplotlib.use("Agg")  # MUST be before pyplot import — headless-safe
matplotlib.rcParams["text.parse_math"] = False

import io
from collections import defaultdict
from datetime import date, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import numpy as np

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
_BG_COLOR = "#1a1a2e"
_FG_COLOR = "#e0e0e0"
_GRID_COLOR = "#2a2a4a"
_ACCENT_COLORS = [
    "#00d2ff", "#7b2ff7", "#ff6b6b", "#ffd93d", "#6bcb77",
    "#4ecdc4", "#ff8a5c", "#a29bfe", "#fd79a8", "#55efc4",
]
_MAX_CHART_LABEL_LENGTH = 50
_MAX_STUDY_LEGEND_LABELS = 10


def _display_label(value: object) -> str:
    """Return a bounded label that Matplotlib cannot parse as math text."""
    label = " ".join(str(value).split())
    if not label:
        return "(unnamed)"
    if len(label) > _MAX_CHART_LABEL_LENGTH:
        return label[: _MAX_CHART_LABEL_LENGTH - 1] + "…"
    return label


def _apply_theme(ax: plt.Axes, fig: plt.Figure) -> None:
    """Apply dark theme to axes and figure."""
    fig.set_facecolor(_BG_COLOR)
    ax.set_facecolor(_BG_COLOR)
    ax.tick_params(colors=_FG_COLOR, which="both")
    ax.xaxis.label.set_color(_FG_COLOR)
    ax.yaxis.label.set_color(_FG_COLOR)
    ax.title.set_color(_FG_COLOR)
    for spine in ax.spines.values():
        spine.set_color(_GRID_COLOR)
    ax.grid(axis="y", color=_GRID_COLOR, linestyle="--", alpha=0.5)


def _to_buffer(fig: plt.Figure) -> io.BytesIO:
    """Render figure to PNG BytesIO buffer."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return buf


# ---------------------------------------------------------------------------
# Study chart — stacked bar (hours per subject per day, last 7 days)
# ---------------------------------------------------------------------------
def study_chart(
    logs: list[dict],
    days: int = 7,
    end_date: date | None = None,
) -> io.BytesIO:
    """Stacked bar chart: study hours by subject per day.

    Args:
        logs: List of dicts with 'subject', 'duration_min', 'local_date' (date).
        days: Number of days to show.
        end_date: The last date to show (default: today).
    """
    if end_date is None:
        from .config import today_local
        end_date = today_local()

    date_range = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]

    # Aggregate: {subject: {date: total_minutes}}
    data: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for log in logs:
        subj = _display_label(log["subject"])
        d = log["local_date"]
        if d in date_range:
            data[subj][d] += log["duration_min"] / 60.0

    if len(data) > _MAX_STUDY_LEGEND_LABELS:
        ranked = sorted(
            data,
            key=lambda subject: (-sum(data[subject].values()), subject.casefold()),
        )
        keep = set(ranked[: _MAX_STUDY_LEGEND_LABELS - 1])
        collapsed: dict[str, dict[date, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        for subject, daily_values in data.items():
            display_subject = subject if subject in keep else "Other subjects"
            for log_date, hours in daily_values.items():
                collapsed[display_subject][log_date] += hours
        data = collapsed

    subjects = sorted(data.keys())
    if not subjects:
        subjects = ["(no data)"]

    fig, ax = plt.subplots(figsize=(10, 5))
    _apply_theme(ax, fig)

    x = np.arange(len(date_range))
    width = 0.6
    bottom = np.zeros(len(date_range))

    for i, subj in enumerate(subjects):
        heights = [data.get(subj, {}).get(d, 0) for d in date_range]
        color = _ACCENT_COLORS[i % len(_ACCENT_COLORS)]
        ax.bar(x, heights, width, bottom=bottom, label=subj, color=color, edgecolor="none")
        bottom += np.array(heights)

    ax.set_xticks(x)
    ax.set_xticklabels([d.strftime("%b %d") for d in date_range], rotation=45, ha="right")
    ax.set_ylabel("Hours")
    ax.set_title("Study — Last 7 Days")
    ax.legend(loc="upper left", fontsize=8, facecolor=_BG_COLOR, edgecolor=_GRID_COLOR,
              labelcolor=_FG_COLOR)

    return _to_buffer(fig)


# ---------------------------------------------------------------------------
# Gym chart — bar (volume per day, weighted vs bodyweight)
# ---------------------------------------------------------------------------
def gym_chart(
    logs: list[dict],
    days: int = 7,
    end_date: date | None = None,
) -> io.BytesIO:
    """Bar chart: daily gym volume split into weighted and bodyweight.

    Args:
        logs: List of dicts with 'sets', 'reps', 'weight_kg', 'local_date'.
    """
    if end_date is None:
        from .config import today_local
        end_date = today_local()

    date_range = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]

    weighted: dict[date, float] = defaultdict(float)
    bodyweight: dict[date, float] = defaultdict(float)

    for log in logs:
        d = log["local_date"]
        if d not in date_range:
            continue
        s, r, w = log["sets"], log["reps"], log["weight_kg"]
        if w is not None:
            weighted[d] += s * r * w
        else:
            bodyweight[d] += s * r

    fig, ax = plt.subplots(figsize=(10, 5))
    _apply_theme(ax, fig)

    x = np.arange(len(date_range))
    width = 0.35

    w_vals = [weighted.get(d, 0) for d in date_range]
    b_vals = [bodyweight.get(d, 0) for d in date_range]

    ax.bar(x - width / 2, w_vals, width, label="Weighted (kg·reps)", color="#7b2ff7")
    ax.bar(x + width / 2, b_vals, width, label="Bodyweight (reps)", color="#00d2ff")

    ax.set_xticks(x)
    ax.set_xticklabels([d.strftime("%b %d") for d in date_range], rotation=45, ha="right")
    ax.set_ylabel("Volume")
    ax.set_title("Gym — Last 7 Days")
    ax.legend(facecolor=_BG_COLOR, edgecolor=_GRID_COLOR, labelcolor=_FG_COLOR)

    return _to_buffer(fig)


# ---------------------------------------------------------------------------
# Diet chart — bar (calories per day, incomplete days hatched)
# ---------------------------------------------------------------------------
def diet_chart(
    logs: list[dict],
    days: int = 7,
    end_date: date | None = None,
) -> io.BytesIO:
    """Bar chart: daily calorie intake. Incomplete days get hatched fill.

    Args:
        logs: List of dicts with 'calories' (int|None), 'local_date'.
    """
    if end_date is None:
        from .config import today_local
        end_date = today_local()

    date_range = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]

    calories: dict[date, int] = defaultdict(int)
    incomplete: set[date] = set()
    has_data: set[date] = set()

    for log in logs:
        d = log["local_date"]
        if d not in date_range:
            continue
        has_data.add(d)
        if log["calories"] is not None:
            calories[d] += log["calories"]
        else:
            incomplete.add(d)

    fig, ax = plt.subplots(figsize=(10, 5))
    _apply_theme(ax, fig)

    x = np.arange(len(date_range))
    vals = [calories.get(d, 0) for d in date_range]
    colors = []
    hatches = []
    for d in date_range:
        if d in incomplete:
            colors.append("#ff6b6b")
            hatches.append("//")
        elif d in has_data:
            colors.append("#6bcb77")
            hatches.append("")
        else:
            colors.append("#4a4a6a")
            hatches.append("")

    bars = ax.bar(x, vals, 0.6, color=colors, edgecolor=_FG_COLOR, linewidth=0.5)
    for bar, hatch in zip(bars, hatches):
        bar.set_hatch(hatch)

    ax.set_xticks(x)
    ax.set_xticklabels([d.strftime("%b %d") for d in date_range], rotation=45, ha="right")
    ax.set_ylabel("Calories")
    ax.set_title("Diet — Last 7 Days")

    # Legend
    complete_patch = mpatches.Patch(facecolor="#6bcb77", label="Complete")
    incomplete_patch = mpatches.Patch(facecolor="#ff6b6b", hatch="//", label="Incomplete (some meals skipped cal)")
    ax.legend(handles=[complete_patch, incomplete_patch], facecolor=_BG_COLOR,
              edgecolor=_GRID_COLOR, labelcolor=_FG_COLOR, fontsize=8)

    return _to_buffer(fig)


# ---------------------------------------------------------------------------
# Habits chart — grid/heatmap (14 days × habits)
# ---------------------------------------------------------------------------
def habits_chart(
    habit_names: list[str],
    habit_ids: list[int],
    logs: list[dict],
    days: int = 14,
    end_date: date | None = None,
) -> io.BytesIO:
    """Grid heatmap: ✅/⬜ per habit per day.

    Args:
        habit_names: List of habit names (rows).
        habit_ids: Corresponding habit IDs.
        logs: List of dicts with 'habit_id', 'log_date' (date).
        days: Number of days to show.
        end_date: Last date to show.
    """
    if end_date is None:
        from .config import today_local
        end_date = today_local()

    date_range = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]

    # Build grid: rows=habits, cols=days
    checked: set[tuple[int, date]] = set()
    for log in logs:
        d = log["log_date"] if isinstance(log["log_date"], date) else date.fromisoformat(log["log_date"])
        checked.add((log["habit_id"], d))

    n_habits = len(habit_names)
    n_days = len(date_range)

    if n_habits == 0:
        # Nothing to show
        fig, ax = plt.subplots(figsize=(8, 2))
        _apply_theme(ax, fig)
        ax.text(0.5, 0.5, "No active habits", ha="center", va="center",
                color=_FG_COLOR, fontsize=14, transform=ax.transAxes)
        ax.axis("off")
        return _to_buffer(fig)

    fig_height = max(2, 0.6 * n_habits + 1.5)
    fig, ax = plt.subplots(figsize=(max(8, n_days * 0.6), fig_height))
    _apply_theme(ax, fig)

    grid = np.zeros((n_habits, n_days))
    for i, hid in enumerate(habit_ids):
        for j, d in enumerate(date_range):
            if (hid, d) in checked:
                grid[i, j] = 1.0

    # Custom colormap: 0=gray, 1=green
    from matplotlib.colors import ListedColormap
    cmap = ListedColormap(["#3a3a5a", "#6bcb77"])
    ax.imshow(grid, cmap=cmap, aspect="auto", vmin=0, vmax=1)

    # Labels
    ax.set_yticks(range(n_habits))
    ax.set_yticklabels([_display_label(name) for name in habit_names], fontsize=9)
    ax.set_xticks(range(n_days))
    ax.set_xticklabels([d.strftime("%d") for d in date_range], fontsize=8)

    # Add ✅/⬜ text in cells
    for i in range(n_habits):
        for j in range(n_days):
            symbol = "✓" if grid[i, j] == 1.0 else "·"
            ax.text(j, i, symbol, ha="center", va="center", fontsize=10,
                    color="white" if grid[i, j] == 0 else "black")

    ax.set_title("Habits — Last 14 Days")
    ax.set_xlabel(f"{date_range[0].strftime('%b %d')} → {date_range[-1].strftime('%b %d')}")

    # Remove grid lines for the heatmap
    ax.grid(False)

    return _to_buffer(fig)
