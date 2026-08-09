from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_ses as ses,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sns_subscriptions as subscriptions,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import custom_resources as cr
from constructs import Construct


class OperationalInboxEmailStack(Stack):
    """Dedicated us-east-1 email data plane; the Django app remains on Hetzner."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        system_domain = self.node.try_get_context("system_domain") or "operationalinbox.com"
        inbound_domain = self.node.try_get_context("inbound_domain") or (f"inbound.{system_domain}")
        configuration_set_name = (
            self.node.try_get_context("configuration_set_name") or "operational-inbox"
        )
        receipt_rule_set_name = (
            self.node.try_get_context("receipt_rule_set_name") or "operational-inbox"
        )
        receipt_rule_name = (
            self.node.try_get_context("receipt_rule_name") or "operational-inbox-allowlist"
        )
        receipt_rule_arn = (
            f"arn:{self.partition}:ses:{self.region}:{self.account}:"
            f"receipt-rule-set/{receipt_rule_set_name}:receipt-rule/{receipt_rule_name}"
        )
        configuration_set_arn = (
            f"arn:{self.partition}:ses:{self.region}:{self.account}:"
            f"configuration-set/{configuration_set_name}"
        )

        bucket = s3.Bucket(
            self,
            "EmailDataBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireIngress",
                    enabled=True,
                    prefix="ingress/",
                    expiration=Duration.days(90),
                ),
                s3.LifecycleRule(
                    id="ExpireTenantRaw",
                    enabled=True,
                    prefix="domains/",
                    expiration=Duration.days(90),
                ),
                s3.LifecycleRule(
                    id="ExpireBackups",
                    enabled=True,
                    prefix="backups/",
                    expiration=Duration.days(30),
                ),
                s3.LifecycleRule(
                    id="AbortIncompleteUploads",
                    enabled=True,
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSESReceiptDelivery",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[bucket.arn_for_objects("ingress/*")],
                conditions={
                    "StringEquals": {
                        "AWS:SourceAccount": self.account,
                        "AWS:SourceArn": receipt_rule_arn,
                    }
                },
            )
        )

        inbound_topic = sns.Topic(
            self,
            "InboundTopic",
            display_name="Operational Inbox inbound receipt events",
            topic_name="operational-inbox-inbound",
            enforce_ssl=True,
        )
        delivery_topic = sns.Topic(
            self,
            "DeliveryTopic",
            display_name="Operational Inbox outbound delivery events",
            topic_name="operational-inbox-delivery",
            enforce_ssl=True,
        )
        for topic, source_arn in (
            (inbound_topic, receipt_rule_arn),
            (delivery_topic, configuration_set_arn),
        ):
            topic.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AllowSESPublish",
                    effect=iam.Effect.ALLOW,
                    principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                    actions=["sns:Publish"],
                    resources=[topic.topic_arn],
                    conditions={
                        "StringEquals": {
                            "AWS:SourceAccount": self.account,
                            "AWS:SourceArn": source_arn,
                        }
                    },
                )
            )

        dead_letter_queue = sqs.Queue(
            self,
            "DeadLetterQueue",
            queue_name="operational-inbox-events-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=Duration.days(14),
        )
        event_queue = sqs.Queue(
            self,
            "EventQueue",
            queue_name="operational-inbox-events",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            receive_message_wait_time=Duration.seconds(20),
            visibility_timeout=Duration.minutes(5),
            retention_period=Duration.days(14),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=dead_letter_queue,
            ),
        )
        # Keep the SNS envelope: ingestion validates TopicArn and uses SNS MessageId
        # as its first idempotency boundary.
        inbound_topic.add_subscription(subscriptions.SqsSubscription(event_queue))
        delivery_topic.add_subscription(subscriptions.SqsSubscription(event_queue))

        configuration_set = ses.CfnConfigurationSet(
            self,
            "ConfigurationSet",
            name=configuration_set_name,
            reputation_options={"reputation_metrics_enabled": True},
            sending_options={"sending_enabled": True},
            suppression_options={"suppressed_reasons": ["BOUNCE", "COMPLAINT"]},
        )
        delivery_destination = ses.CfnConfigurationSetEventDestination(
            self,
            "DeliveryEventDestination",
            configuration_set_name=configuration_set_name,
            event_destination=ses.CfnConfigurationSetEventDestination.EventDestinationProperty(
                enabled=True,
                matching_event_types=[
                    "send",
                    "reject",
                    "bounce",
                    "complaint",
                    "delivery",
                    "renderingFailure",
                    "deliveryDelay",
                ],
                name="operational-inbox-sns",
                sns_destination=ses.CfnConfigurationSetEventDestination.SnsDestinationProperty(
                    topic_arn=delivery_topic.topic_arn
                ),
            ),
        )
        delivery_destination.add_resource_dependency(configuration_set)

        receipt_rule_set = ses.CfnReceiptRuleSet(
            self,
            "ReceiptRuleSet",
            rule_set_name=receipt_rule_set_name,
        )
        activate_rule_set = cr.AwsCustomResource(
            self,
            "ActivateReceiptRuleSet",
            on_create=cr.AwsSdkCall(
                service="SES",
                action="setActiveReceiptRuleSet",
                parameters={"RuleSetName": receipt_rule_set_name},
                physical_resource_id=cr.PhysicalResourceId.of(receipt_rule_set_name),
            ),
            on_update=cr.AwsSdkCall(
                service="SES",
                action="setActiveReceiptRuleSet",
                parameters={"RuleSetName": receipt_rule_set_name},
                physical_resource_id=cr.PhysicalResourceId.of(receipt_rule_set_name),
            ),
            on_delete=cr.AwsSdkCall(
                service="SES",
                action="setActiveReceiptRuleSet",
                parameters={},
                physical_resource_id=cr.PhysicalResourceId.of(receipt_rule_set_name),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["ses:SetActiveReceiptRuleSet"],
                        resources=["*"],
                    )
                ]
            ),
            install_latest_aws_sdk=False,
        )
        activate_rule_set.node.add_dependency(receipt_rule_set)

        system_identity = ses.CfnEmailIdentity(
            self,
            "SystemIdentity",
            email_identity=system_domain,
        )
        inbound_identity = ses.CfnEmailIdentity(
            self,
            "InboundIdentity",
            email_identity=inbound_domain,
        )
        system_identity.apply_removal_policy(RemovalPolicy.RETAIN)
        inbound_identity.apply_removal_policy(RemovalPolicy.RETAIN)

        app_user = iam.User(
            self,
            "HetznerApplicationUser",
            user_name="operational-inbox-hetzner",
        )
        event_queue.grant_consume_messages(app_user)
        app_user.add_to_policy(
            iam.PolicyStatement(
                sid="EmailObjectAccess",
                actions=[
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:DeleteObject",
                ],
                resources=[
                    bucket.arn_for_objects("ingress/*"),
                    bucket.arn_for_objects("domains/*"),
                    bucket.arn_for_objects("backups/*"),
                ],
            )
        )
        app_user.add_to_policy(
            iam.PolicyStatement(
                sid="EmailBucketLocation",
                actions=["s3:GetBucketLocation"],
                resources=[bucket.bucket_arn],
            )
        )
        app_user.add_to_policy(
            iam.PolicyStatement(
                sid="EmailBucketList",
                actions=["s3:ListBucket"],
                resources=[bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": ["ingress/*", "domains/*", "backups/*"]}},
            )
        )
        app_user.add_to_policy(
            iam.PolicyStatement(
                sid="SESIdentityLifecycle",
                actions=[
                    "ses:GetIdentityDkimAttributes",
                    "ses:GetIdentityVerificationAttributes",
                    "ses:VerifyDomainDkim",
                    "ses:VerifyDomainIdentity",
                ],
                # These SES v1 identity lifecycle APIs do not support
                # resource-level permissions. Keep the action list narrow.
                resources=["*"],
            )
        )
        app_user.add_to_policy(
            iam.PolicyStatement(
                sid="SESOutboundSending",
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[f"arn:{self.partition}:ses:{self.region}:{self.account}:identity/*"],
            )
        )
        app_user.add_to_policy(
            iam.PolicyStatement(
                sid="SESReceiptRuleReconciliation",
                actions=[
                    "ses:CreateReceiptRule",
                    "ses:DescribeReceiptRule",
                    "ses:DescribeReceiptRuleSet",
                    "ses:UpdateReceiptRule",
                ],
                resources=["*"],
            )
        )

        cloudwatch.Alarm(
            self,
            "DeadLetterMessagesAlarm",
            metric=dead_letter_queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Operational Inbox has an event in its dead-letter queue.",
        )
        cloudwatch.Alarm(
            self,
            "QueueBacklogAlarm",
            metric=event_queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            threshold=100,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Operational Inbox event queue backlog is elevated.",
        )
        cloudwatch.Alarm(
            self,
            "QueueAgeAlarm",
            metric=event_queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            threshold=300,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Operational Inbox event processing is delayed.",
        )

        cdk.CfnOutput(self, "Region", value=self.region)
        cdk.CfnOutput(self, "EmailBucketName", value=bucket.bucket_name)
        cdk.CfnOutput(self, "InboundTopicArn", value=inbound_topic.topic_arn)
        cdk.CfnOutput(self, "DeliveryTopicArn", value=delivery_topic.topic_arn)
        cdk.CfnOutput(self, "EventQueueUrl", value=event_queue.queue_url)
        cdk.CfnOutput(self, "EventQueueArn", value=event_queue.queue_arn)
        cdk.CfnOutput(self, "DeadLetterQueueUrl", value=dead_letter_queue.queue_url)
        cdk.CfnOutput(self, "DeadLetterQueueArn", value=dead_letter_queue.queue_arn)
        cdk.CfnOutput(self, "ConfigurationSetName", value=configuration_set_name)
        cdk.CfnOutput(self, "ReceiptRuleSetName", value=receipt_rule_set_name)
        cdk.CfnOutput(self, "ReceiptRuleName", value=receipt_rule_name)
        cdk.CfnOutput(self, "SystemIdentityName", value=system_domain)
        cdk.CfnOutput(self, "ApplicationIamUser", value=app_user.user_name)
        cdk.CfnOutput(
            self,
            "InboundMxRecord",
            value=f"10 inbound-smtp.{self.region}.amazonaws.com",
        )
        self._output_dkim("System", system_identity)
        self._output_dkim("Inbound", inbound_identity)

    def _output_dkim(self, prefix: str, identity: ses.CfnEmailIdentity) -> None:
        names = [
            identity.attr_dkim_dns_token_name1,
            identity.attr_dkim_dns_token_name2,
            identity.attr_dkim_dns_token_name3,
        ]
        values = [
            identity.attr_dkim_dns_token_value1,
            identity.attr_dkim_dns_token_value2,
            identity.attr_dkim_dns_token_value3,
        ]
        for index, (name, value) in enumerate(zip(names, values, strict=True), start=1):
            cdk.CfnOutput(self, f"{prefix}DkimName{index}", value=name)
            cdk.CfnOutput(self, f"{prefix}DkimValue{index}", value=value)
