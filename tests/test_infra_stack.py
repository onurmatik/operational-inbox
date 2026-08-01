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


def test_sns_subscriptions_preserve_the_sns_envelope() -> None:
    result = template()
    result.resource_count_is("AWS::SNS::Subscription", 2)
    result.has_resource_properties(
        "AWS::SNS::Subscription",
        assertions.Match.not_(assertions.Match.object_like({"RawMessageDelivery": True})),
    )
