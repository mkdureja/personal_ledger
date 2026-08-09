from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from enum import StrEnum



class DietEntryMode(StrEnum):
    QUICK = "quick"
    BUILDER = "builder"

class DietItemSourceType(StrEnum):
    FOOD = "food"
    RECIPE = "recipe"
    CATALOG = "catalog"
    FREETEXT = "freetext"


#: The source types that can carry a user preference — a usual amount, a pin, or
#: a hide. Named once because widening it is otherwise a hunt: the same tuple was
#: spelled out at a dozen call sites, and v16 shipped with two of them missed, so
#: the storage accepted a catalog preference that no screen would ever offer and
#: no keyboard would ever render. ``freetext`` is absent on purpose — it has no
#: source row to hold a preference against.
PREFERENCE_SOURCE_TYPES: tuple[str, ...] = ("food", "recipe", "catalog")

class QuickMealStatus(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    REPLAYED_REMOVED = "replayed_removed"
    QUANTITY_REQUIRED = "quantity_required"
    QUANTITY_INVALID = "quantity_invalid"
    DEFAULT_INVALID = "default_invalid"
    SOURCE_UNAVAILABLE = "source_unavailable"

class RepeatStatus(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    EMPTY = "empty"
    REPLAYED_REMOVED = "replayed_removed"

class CurrentCommitStatus(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    REPLAYED_REMOVED = "replayed_removed"
    REVIEW_REQUIRED = "review_required"
    SOURCE_MEAL_REMOVED = "source_meal_removed"

class UndoStatus(StrEnum):
    DELETED = "deleted"
    ALREADY_REMOVED = "already_removed"
    EXPIRED = "expired"

class CurrentValueDecision(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVE_CURRENT = "resolved"
    KEEP_ORIGINAL = "keep_original"
    REMOVE = "remove"

class CurrentValueIssueCode(StrEnum):
    SOURCE_MISSING = "source_missing"
    QUANTITY_MISSING = "quantity_missing"
    QUANTITY_INVALID = "quantity_invalid"
    RECIPE_EMPTY = "recipe_empty"

@dataclass(frozen=True)
class DefaultQuantity:
    amount: float
    unit: str

@dataclass(frozen=True)
class NutrientValues:
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None

@dataclass(frozen=True)
class SourceNutrients:
    calories: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None

@dataclass(frozen=True)
class DietLogItemInput:
    source_type: DietItemSourceType
    source_id: int | None
    source_provider: str | None
    source_revision: str | None
    display_name: str
    entered_amount: float | None
    entered_unit: str | None
    resolved_base_amount: float | None
    resolved_base_unit: str | None
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None

@dataclass(frozen=True)
class DietHeaderSnapshot:
    meal_id: int
    user_id: int
    meal_type: str
    food_items: str
    nutrients: NutrientValues
    logged_at_utc: datetime | None

@dataclass(frozen=True)
class DietItemSnapshot:
    child_id: int
    user_id: int
    meal_id: int
    item_order: int
    source_type: DietItemSourceType
    source_id: int | None
    source_provider: str | None
    source_revision: str | None
    display_name: str
    entered_amount: float | None
    entered_unit: str | None
    resolved_base_amount: float | None
    resolved_base_unit: str | None
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None
    created_at_utc: datetime | None

@dataclass(frozen=True)
class MealReceipt:
    header: DietHeaderSnapshot
    items: tuple[DietItemSnapshot, ...]

@dataclass(frozen=True)
class QuickMealResult:
    status: QuickMealStatus
    receipt: MealReceipt | None

@dataclass(frozen=True)
class RepeatResult:
    status: RepeatStatus
    receipt: MealReceipt | None

@dataclass(frozen=True)
class FoodPortionSnapshot:
    portion_id: int
    name: str
    name_key: str
    base_amount: float

@dataclass(frozen=True)
class FoodResolutionSource:
    food_id: int
    user_id: int
    name: str
    base_unit: str
    basis_amount: float
    nutrients: SourceNutrients
    is_active: bool
    portions: tuple[FoodPortionSnapshot, ...]

@dataclass(frozen=True)
class RecipeIngredientSnapshot:
    ingredient_id: int
    food_id: int
    base_amount: float
    display_amount: float
    display_unit: str
    food: FoodResolutionSource

@dataclass(frozen=True)
class RecipeResolutionSource:
    recipe_id: int
    user_id: int
    name: str
    yield_amount: float
    yield_unit: str
    is_active: bool
    ingredients: tuple[RecipeIngredientSnapshot, ...]

@dataclass(frozen=True)
class CatalogResolutionSource:
    catalog_id: int
    provider: str
    provider_food_id: str
    provider_revision: str | None
    display_name: str
    base_unit: str
    basis_amount: float
    nutrients: SourceNutrients
    is_active: bool
    portions: tuple[FoodPortionSnapshot, ...]

@dataclass(frozen=True)
class CurrentValueSourceBundle:
    header: DietHeaderSnapshot
    items: tuple[DietItemSnapshot, ...]
    foods: tuple[FoodResolutionSource, ...]
    recipes: tuple[RecipeResolutionSource, ...]
    catalogs: tuple[CatalogResolutionSource, ...]

@dataclass(frozen=True)
class CurrentValueIssue:
    source_child_id: int
    code: CurrentValueIssueCode

@dataclass(frozen=True)
class CurrentValueItemProposal:
    source_child_id: int
    decision: CurrentValueDecision
    original: DietItemSnapshot
    persisted_item_order: int | None
    proposed: DietLogItemInput | None
    issue: CurrentValueIssue | None

@dataclass(frozen=True)
class CurrentValuePreview:
    source_meal_id: int
    meal_type: str
    items: tuple[CurrentValueItemProposal, ...]
    original_totals: NutrientValues
    proposed_totals: NutrientValues
    delta: NutrientValues
    digest: str
    can_save: bool

@dataclass(frozen=True)
class CurrentValueCommitResult:
    status: CurrentCommitStatus
    receipt: MealReceipt | None
    preview: CurrentValuePreview | None

@dataclass(frozen=True)
class UndoResult:
    status: UndoStatus
    meal_id: int
    deleted_header: DietHeaderSnapshot | None