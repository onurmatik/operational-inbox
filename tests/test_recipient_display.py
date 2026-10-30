from __future__ import annotations

import re

import pytest
from django.test import override_settings
from django.urls import reverse

from inbox.models import Message, MessageRecipient
from inbox.services.ingestion import process_sqs_body
from tests.test_ingestion import (
    BUCKET,
    DELIVERY_TOPIC,
    INBOUND_TOPIC,
    FakeS3,
    create_route,
    raw_email_to,
    sns_body,
)


def displayed_recipients(client, message):
    response = client.get(reverse("conversation_detail", args=[message.conversation_id]))
    assert response.status_code == 200
    match = re.search(r"<p[^>]*>To: (.*?)</p>", response.content.decode(), re.DOTALL)
    assert match
    return match.group(1)


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_single_to_header_and_matching_envelope_render_once(client, owner, domain):
    address = "route-abc@inbound.example"
    create_route(domain, domain, address)
    raw = raw_email_to(address)
    assert raw.count(address.encode()) == 1
    process_sqs_body(sns_body([address]), s3_client=FakeS3(raw))
    message = Message.objects.get(domain=domain)
    client.force_login(owner)

    assert displayed_recipients(client, message) == address
    # Both records remain available for routing and typed API consumers.
    detail = client.get(
        f"/api/v1/domains/{domain.id}/conversations/{message.conversation_id}"
    ).json()
    recipients = detail["messages"][0]["recipients"]
    assert {(item["kind"], item["address"], item["routing"]) for item in recipients} == {
        ("ENVELOPE", address, True),
        ("TO", address, False),
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "rows,expected",
    [
        (
            [("ENVELOPE", "support@example.com"), ("TO", "Support@Example.com")],
            {"support@example.com"},
        ),
        (
            [
                ("ENVELOPE", "support@example.com"),
                ("TO", "support@example.com"),
                ("TO", "team@example.com"),
                ("CC", "team@example.com"),
                ("ENVELOPE", "forwarded@example.com"),
            ],
            {"support@example.com", "team@example.com", "forwarded@example.com"},
        ),
        ([("ENVELOPE", "hidden@example.com")], {"hidden@example.com"}),
    ],
)
def test_recipient_display_deduplicates_without_losing_addresses(
    client, owner, inbound_message, rows, expected, django_assert_num_queries
):
    MessageRecipient.objects.bulk_create(
        [
            MessageRecipient(
                domain=inbound_message.domain,
                message=inbound_message,
                kind=kind,
                address=address,
                is_routing_recipient=kind == "ENVELOPE",
            )
            for kind, address in rows
        ]
    )
    message = Message.objects.prefetch_related("recipients").get(id=inbound_message.id)
    with django_assert_num_queries(0):
        addresses = message.display_recipient_addresses
    assert len(addresses) == len(expected)
    assert {address.casefold() for address in addresses} == expected
    client.force_login(owner)
    displayed = displayed_recipients(client, message).casefold().split(", ")
    assert len(displayed) == len(expected)
    assert set(displayed) == expected
    assert message.recipients.count() == len(rows)
