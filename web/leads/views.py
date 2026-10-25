from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from pipeline.dashboard import clear_review, connect, leads, save_review

VERDICT_CHOICES = (
    ("", "Unreviewed"),
    ("worth_call", "Worth a call"),
    ("wrong_size", "Wrong size"),
    ("hospital_employee", "Hospital employee"),
    ("not_buyer", "Not the buyer"),
)


def _filters(request) -> dict[str, str]:
    return {
        "q": request.GET.get("q", ""),
        "specialty": request.GET.get("specialty", ""),
        "verdict": request.GET.get("verdict", ""),
        "limit": "20",
        "offset": "0",
    }


@login_required
def board(request):
    with connect(settings.DATABASE_URL) as conn:
        payload = leads(conn, _filters(request))
    payload["verdict_choices"] = VERDICT_CHOICES
    payload["query"] = request.GET.get("q", "")
    return render(request, "leads/board.html", payload)


@login_required
@require_POST
def review(request):
    body = {
        "segment_id": request.POST.get("segment_id"),
        "org_key": request.POST.get("org_key"),
        "contact_key": request.POST.get("contact_key"),
        "verdict": request.POST.get("verdict") or "",
    }
    with connect(settings.DATABASE_URL) as conn:
        if body["verdict"]:
            save_review(conn, body)
        else:
            clear_review(conn, body)
    return redirect("board")
