from __future__ import annotations

import os

import aws_cdk as cdk

from infra.stack import OperationalInboxEmailStack

app = cdk.App()
OperationalInboxEmailStack(
    app,
    "OperationalInboxEmail",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region="us-east-1",
    ),
    description="Operational Inbox SES, S3, SNS, and SQS email data plane",
)
app.synth()
