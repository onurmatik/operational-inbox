from __future__ import annotations

import uuid
from typing import TypeVar

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Manager, Model, QuerySet
from django.http import Http404

from inbox.models import Domain, User

T = TypeVar("T", bound=Model)


def owned_domains(user: User) -> QuerySet[Domain]:
    return Domain.objects.filter(owner=user).exclude(status=Domain.Status.DISABLED)


def get_owned_domain(user: User, domain_id: uuid.UUID | str) -> Domain:
    try:
        return owned_domains(user).get(id=domain_id)
    except (Domain.DoesNotExist, ValueError, ValidationError) as exc:
        raise Http404 from exc


def domain_get_or_404(queryset: Manager[T] | QuerySet[T], *, domain: Domain, **lookup: object) -> T:
    try:
        return queryset.filter(domain=domain).get(**lookup)
    except (ObjectDoesNotExist, ValueError) as exc:
        raise Http404 from exc
