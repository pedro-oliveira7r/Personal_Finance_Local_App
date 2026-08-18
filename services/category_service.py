"""Categories and subcategories."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from constants import ALLOCATION_KINDS, CategoryKind
from database.models import BudgetLine, Category, RecurringRule, Transaction
from schemas.validation import CategoryIn
from services.common import (
    ConflictError,
    NotFoundError,
    ServiceError,
    apply_fields,
    send_to_recycle_bin,
)


@dataclass
class CategoryNode:
    category: Category
    children: list["CategoryNode"] = field(default_factory=list)

    @property
    def id(self) -> int:
        return self.category.id

    @property
    def name(self) -> str:
        return self.category.name


def list_categories(
    session: Session,
    *,
    kind: Optional[str] = None,
    kinds: Optional[Sequence[str]] = None,
    include_archived: bool = False,
    parents_only: bool = False,
) -> list[Category]:
    stmt = select(Category).order_by(Category.sort_order, Category.name)
    if kind:
        stmt = stmt.where(Category.kind == kind)
    if kinds:
        stmt = stmt.where(Category.kind.in_(list(kinds)))
    if not include_archived:
        stmt = stmt.where(Category.is_archived.is_(False))
    if parents_only:
        stmt = stmt.where(Category.parent_id.is_(None))
    return list(session.execute(stmt).scalars())


def allocation_categories(session: Session, *, include_archived: bool = False) -> list[Category]:
    """Every category that can receive a budget allocation."""
    return list_categories(session, kinds=ALLOCATION_KINDS, include_archived=include_archived)


def category_tree(session: Session, *, kind: Optional[str] = None,
                  include_archived: bool = False) -> list[CategoryNode]:
    categories = list_categories(session, kind=kind, include_archived=include_archived)
    nodes = {cat.id: CategoryNode(category=cat) for cat in categories}
    roots: list[CategoryNode] = []
    for cat in categories:
        node = nodes[cat.id]
        parent = nodes.get(cat.parent_id) if cat.parent_id else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)
    return roots


def get_category(session: Session, category_id: int) -> Category:
    category = session.get(Category, category_id)
    if category is None:
        raise NotFoundError(f"Category #{category_id} was not found.")
    return category


def find_by_name(session: Session, name: str, *, parent_id: Optional[int] = None,
                 kind: Optional[str] = None) -> Optional[Category]:
    stmt = select(Category).where(func.lower(Category.name) == (name or "").strip().lower())
    if parent_id is not None:
        stmt = stmt.where(Category.parent_id == parent_id)
    if kind:
        stmt = stmt.where(Category.kind == kind)
    return session.execute(stmt).scalars().first()


def resolve_path(session: Session, path: str, *, kind: Optional[str] = None,
                 create_missing: bool = False) -> Optional[Category]:
    """Resolve ``"Food › Groceries"`` or ``"Food/Groceries"`` to a category."""
    if not path:
        return None
    separators = ["›", ">", "/", ":", "|"]
    text = path
    for sep in separators:
        text = text.replace(sep, "\x00")
    parts = [part.strip() for part in text.split("\x00") if part.strip()]
    if not parts:
        return None

    parent: Optional[Category] = None
    for index, part in enumerate(parts):
        found = find_by_name(session, part, parent_id=parent.id if parent else None, kind=kind)
        if found is None and parent is None and index == 0:
            # Allow a leaf name to match anywhere before giving up.
            found = find_by_name(session, part, kind=kind)
        if found is None:
            if not create_missing:
                return parent
            found = create_category(session, {
                "name": part,
                "kind": kind or CategoryKind.EXPENSE.value,
                "parent_id": parent.id if parent else None,
            })
        parent = found
    return parent


def create_category(session: Session, payload: dict[str, Any]) -> Category:
    data = CategoryIn(**payload)
    if data.parent_id:
        parent = get_category(session, data.parent_id)
        if parent.parent_id is not None:
            raise ServiceError(
                "Categories go two levels deep — pick a top-level category as the parent."
            )
        if parent.kind != data.kind:
            data.kind = parent.kind  # keep a subcategory in its parent's family
    existing = find_by_name(session, data.name, parent_id=data.parent_id, kind=data.kind)
    if existing is not None:
        raise ConflictError(f"A category called “{data.name}” already exists here.")

    category = Category(**data.model_dump())
    session.add(category)
    session.flush()
    return category


def update_category(session: Session, category_id: int, payload: dict[str, Any]) -> Category:
    category = get_category(session, category_id)
    merged = {
        "name": category.name,
        "kind": category.kind,
        "parent_id": category.parent_id,
        "color": category.color,
        "icon": category.icon,
        "notes": category.notes,
        "sort_order": category.sort_order,
    }
    merged.update(payload)
    if merged.get("parent_id") == category_id:
        raise ServiceError("A category cannot be its own parent.")
    data = CategoryIn(**merged)

    clash = find_by_name(session, data.name, parent_id=data.parent_id, kind=data.kind)
    if clash is not None and clash.id != category_id:
        raise ConflictError(f"Another category called “{data.name}” already exists here.")

    apply_fields(category, data.model_dump())
    # Keep children in the same kind family.
    for child in list(category.children):
        if child.kind != category.kind:
            child.kind = category.kind
    session.flush()
    return category


def usage_count(session: Session, category_id: int) -> dict[str, int]:
    """How many records point at this category (and its children)."""
    child_ids = [
        row[0] for row in session.execute(
            select(Category.id).where(Category.parent_id == category_id)
        ).all()
    ]
    ids = [category_id, *child_ids]
    return {
        "transactions": session.execute(
            select(func.count(Transaction.id)).where(Transaction.category_id.in_(ids))
        ).scalar() or 0,
        "budget_lines": session.execute(
            select(func.count(BudgetLine.id)).where(BudgetLine.category_id.in_(ids))
        ).scalar() or 0,
        "rules": session.execute(
            select(func.count(RecurringRule.id)).where(RecurringRule.category_id.in_(ids))
        ).scalar() or 0,
        "subcategories": len(child_ids),
    }


def archive_category(session: Session, category_id: int, archived: bool = True) -> Category:
    category = get_category(session, category_id)
    category.is_archived = archived
    for child in list(category.children):
        child.is_archived = archived
    session.flush()
    return category


def delete_category(session: Session, category_id: int, *, force: bool = False) -> dict[str, Any]:
    """Delete a category, refusing when it is in use unless ``force``.

    Deleting is always recoverable: a JSON snapshot goes to the recycle bin
    first, and transactions keep their history with ``category_id`` set to NULL
    rather than disappearing.
    """
    category = get_category(session, category_id)
    usage = usage_count(session, category_id)
    in_use = usage["transactions"] + usage["budget_lines"] + usage["rules"]
    if in_use and not force:
        raise ConflictError(
            f"“{category.full_name}” is used by {usage['transactions']} transaction(s), "
            f"{usage['budget_lines']} budget line(s) and {usage['rules']} rule(s). "
            "Archive it instead, or confirm to delete and leave those records uncategorised."
        )
    send_to_recycle_bin(session, "category", category, label=category.full_name)
    for child in list(category.children):
        send_to_recycle_bin(session, "category", child, label=child.full_name)
        session.delete(child)
    session.delete(category)
    session.flush()
    return {"deleted": category_id, "usage": usage}


def counts_by_kind(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Category.kind, func.count(Category.id))
        .where(Category.is_archived.is_(False))
        .group_by(Category.kind)
    ).all()
    return {row[0]: row[1] for row in rows}


def options_for_select(
    session: Session,
    *,
    kinds: Optional[Sequence[str]] = None,
    include_archived: bool = False,
    include_parents: bool = True,
) -> list[tuple[int, str]]:
    """``[(id, "Food › Groceries"), ...]`` sorted for a selectbox."""
    categories = list_categories(session, kinds=kinds, include_archived=include_archived)
    by_id = {cat.id: cat for cat in categories}
    options: list[tuple[int, str]] = []
    for cat in categories:
        if cat.parent_id is None and not include_parents:
            has_children = any(c.parent_id == cat.id for c in categories)
            if has_children:
                continue
        parent = by_id.get(cat.parent_id) if cat.parent_id else None
        label = f"{parent.name} › {cat.name}" if parent else cat.name
        options.append((cat.id, label))
    options.sort(key=lambda item: item[1].lower())
    return options
