from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from inbox.models import Conversation
from inbox.services.reply_assist_model import ModelUnavailable
from inbox.services.reply_assistance import assist
from inbox.views import _owned_record, verified_required


@verified_required
@require_POST
def conversation_reply_assist(request, conversation_id):
    conversation = _owned_record(
        request, Conversation.objects.select_related("domain__owner"), id=conversation_id
    )
    try:
        result = assist(
            conversation,
            operation=request.POST.get("operation", ""),
            token=request.POST.get("context_token", ""),
            subject=request.POST.get("subject", ""),
            body_text=request.POST.get("body_text", ""),
            instructions=request.POST.get("instructions", ""),
        )
    except ValidationError as exc:
        code = getattr(exc, "code", "")
        return JsonResponse(
            {"message": " ".join(exc.messages), "stale": code == "stale"},
            status=409 if code == "stale" else 403 if code == "read_only" else 400,
        )
    except ModelUnavailable as exc:
        return JsonResponse({"message": str(exc)}, status=503)
    return JsonResponse(result)
