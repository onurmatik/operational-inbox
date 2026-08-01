from __future__ import annotations

import uuid
from typing import TypeVar

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Manager, Model, QuerySet
from django.http import Http404, HttpRequest

from inbox.models import Organization, User

T = TypeVar("T", bound=Model)


def owned_organizations(user: User) -> QuerySet[Organization]:
    return Organization.objects.filter(owner=user, is_active=True)


def get_owned_organization(user: User, organization_id: uuid.UUID | str) -> Organization:
    try:
        return owned_organizations(user).get(id=organization_id)
    except (Organization.DoesNotExist, ValueError) as exc:
        raise Http404 from exc


def current_organization(request: HttpRequest) -> Organization:
    if not request.user.is_authenticated:
        raise Http404
    organizations = owned_organizations(request.user)
    selected_id = request.session.get("organization_id")
    selected = organizations.filter(id=selected_id).first() if selected_id else None
    selected = selected or organizations.first()
    if selected is None:
        raise Http404
    if request.session.get("organization_id") != str(selected.id):
        request.session["organization_id"] = str(selected.id)
        request.session.pop("project_id", None)
    return selected


def tenant_get_or_404(
    queryset: Manager[T] | QuerySet[T], *, organization: Organization, **lookup: object
) -> T:
    try:
        return queryset.filter(organization=organization).get(**lookup)
    except (ObjectDoesNotExist, ValueError) as exc:
        raise Http404 from exc
