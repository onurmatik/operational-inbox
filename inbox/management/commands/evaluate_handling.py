import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from inbox.models import User
from inbox.services.handling_model import ModelUnavailable, classify


class Command(BaseCommand):
    help = "Opt-in live classification of synthetic fixtures only; never sends email."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true")
        parser.add_argument(
            "--owner", required=True, help="Account email whose usage budget applies"
        )

    def handle(self, *args, **options):
        if not options["live"]:
            raise CommandError("Pass --live to use the service API key on synthetic fixtures.")
        owner = User.objects.filter(email=options["owner"]).first()
        if owner is None:
            raise CommandError("Evaluation account was not found.")
        cases = json.loads(
            (Path(__file__).resolve().parents[2] / "evals" / "handling.json").read_text()
        )
        failures = 0
        for case in cases:
            try:
                result = classify(
                    owner,
                    {
                        "domain_knowledge": "Fictional app: select language in Settings.",
                        "messages": [
                            {
                                "direction": "INBOUND",
                                "sender": "customer@example.invalid",
                                "body": case["body"],
                            }
                        ],
                        "security": {
                            "spf_verdict": "PASS",
                            "dkim_verdict": "PASS",
                            "dmarc_verdict": "PASS",
                            "virus_verdict": "PASS",
                        },
                        "headers": {},
                        "tags": [],
                    },
                )
            except ModelUnavailable as exc:
                raise CommandError(str(exc)) from exc
            passed = (
                result["type"] in case["types"]
                and result["risk"] in case["risks"]
                and result["missing_information"] >= case.get("missing_information_min", 0)
            )
            failures += int(not passed)
            self.stdout.write(json.dumps({"case": case["id"], "passed": passed, **result}))
        if failures:
            raise CommandError(
                f"{failures} synthetic cases need review; do not lower thresholds automatically."
            )
