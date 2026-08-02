from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from infra.stack import OperationalInboxEmailStack


def template() -> assertions.Template:
    app = cdk.App()
    stack = OperationalInboxEmailStack(
        app,
        "TestEmailDataPlane",
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


def app_user_policy_statements(result: assertions.Template) -> list[dict]:
    policies = result.find_resources("AWS::IAM::Policy")
    app_policy = next(
        resource
        for resource in policies.values()
        if resource["Properties"]["PolicyName"].startswith("HetznerApplicationUserDefaultPolicy")
    )
    return app_policy["Properties"]["PolicyDocument"]["Statement"]


def test_private_bucket_and_durable_queue_contract() -> None:
    result = template()
    result.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    result.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "operational-inbox-events",
            "ReceiveMessageWaitTimeSeconds": 20,
            "VisibilityTimeout": 300,
            "MessageRetentionPeriod": 1209600,
            "SqsManagedSseEnabled": True,
            "RedrivePolicy": {
                "maxReceiveCount": 5,
                "deadLetterTargetArn": assertions.Match.any_value(),
            },
        },
    )


def test_receipt_rule_is_deliberately_owned_by_django() -> None:
    result = template()
    result.resource_count_is("AWS::SES::ReceiptRuleSet", 1)
    result.resource_count_is("AWS::SES::ReceiptRule", 0)
    result.has_resource_properties(
        "Custom::AWS",
        assertions.Match.object_like(
            {"Create": assertions.Match.string_like_regexp("setActiveReceiptRuleSet")}
        ),
    )


def test_no_access_key_and_required_observability() -> None:
    result = template()
    result.resource_count_is("AWS::IAM::User", 1)
    result.resource_count_is("AWS::IAM::AccessKey", 0)
    result.resource_count_is("AWS::CloudWatch::Alarm", 3)
    result.resource_count_is("AWS::SES::ConfigurationSet", 1)
    result.resource_count_is("AWS::SES::ConfigurationSetEventDestination", 1)


def test_ses_identity_lifecycle_uses_required_wildcard_without_broad_sending() -> None:
    result = template()
    statements = app_user_policy_statements(result)

    lifecycle = next(
        statement for statement in statements if statement.get("Sid") == "SESIdentityLifecycle"
    )
    assert lifecycle["Resource"] == "*"
    assert set(lifecycle["Action"]) == {
        "ses:GetIdentityDkimAttributes",
        "ses:GetIdentityVerificationAttributes",
        "ses:VerifyDomainDkim",
        "ses:VerifyDomainIdentity",
    }

    sending = next(
        statement for statement in statements if statement.get("Sid") == "SESOutboundSending"
    )
    assert sending["Resource"] == {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":ses:us-east-1:111122223333:identity/*",
            ],
        ]
    }
    assert set(sending["Action"]) == {"ses:SendEmail", "ses:SendRawEmail"}
    for statement in statements:
        if statement.get("Resource") == "*":
            assert not {"ses:SendEmail", "ses:SendRawEmail"}.intersection(
                set(statement.get("Action", []))
            )


def test_bucket_location_is_not_constrained_by_an_object_prefix() -> None:
    statements = app_user_policy_statements(template())
    location = next(
        statement for statement in statements if statement.get("Sid") == "EmailBucketLocation"
    )
    assert location["Action"] == "s3:GetBucketLocation"
    assert "Condition" not in location

    listing = next(
        statement for statement in statements if statement.get("Sid") == "EmailBucketList"
    )
    assert listing["Action"] == "s3:ListBucket"
    assert listing["Condition"] == {
        "StringLike": {"s3:prefix": ["ingress/*", "domains/*", "backups/*"]}
    }


def test_sns_subscriptions_preserve_the_sns_envelope() -> None:
    result = template()
    result.resource_count_is("AWS::SNS::Subscription", 2)
    result.has_resource_properties(
        "AWS::SNS::Subscription",
        assertions.Match.not_(assertions.Match.object_like({"RawMessageDelivery": True})),
    )
