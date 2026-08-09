from __future__ import annotations

import uuid
from typing import TypeVar

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Manager, Model, QuerySet
from django.http import Http404, HttpRequest

from inbox.models import Domain, User

T = TypeVar("T", bound=Model)


def owned_domains(user: User) -> QuerySet[Domain]:
    return Domain.objects.filter(owner=user).exclude(status=Domain.Status.DISABLED)


def get_owned_domain(user: User, domain_id: uuid.UUID | str) -> Domain:
    try:
        return owned_domains(user).get(id=domain_id)
    except (Domain.DoesNotExist, ValueError) as exc:
        raise Http404 from exc


def current_domain(request: HttpRequest) -> Domain:
    if not request.user.is_authenticated:
        raise Http404
    domains = owned_domains(request.user)
    selected_id = request.session.get("domain_id")
    selected = domains.filter(id=selected_id).first() if selected_id else None
    selected = selected or domains.first()
    if selected is None:
        raise Http404
    if request.session.get("domain_id") != str(selected.id):
        request.session["domain_id"] = str(selected.id)
    return selected


def domain_get_or_404(queryset: Manager[T] | QuerySet[T], *, domain: Domain, **lookup: object) -> T:
    try:
        return queryset.filter(domain=domain).get(**lookup)
    except (ObjectDoesNotExist, ValueError) as exc:
        raise Http404 from exc
