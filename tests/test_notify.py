import json

import pytest
import responses

from scout.errors import ConfigError, NotifyError
from scout.monitor.notify import AppriseNotifier

pytest.importorskip("apprise")

HOOK = "http://hooks.example/scout"


@responses.activate
def test_alerts_are_delivered_through_apprise():
    responses.add(responses.POST, HOOK, json={})
    AppriseNotifier(["json://hooks.example/scout"]).send(
        "Scout gpu: 1 alert", "- below 1800 USD: RTX 5090: $1,799"
    )
    payload = json.loads(responses.calls[0].request.body)
    assert payload["title"] == "Scout gpu: 1 alert"
    assert "$1,799" in payload["message"]


@responses.activate
def test_a_failed_delivery_is_an_error_to_retry():
    responses.add(responses.POST, HOOK, status=503)
    with pytest.raises(NotifyError):
        AppriseNotifier(["json://hooks.example/scout"]).send("title", "body")


def test_unknown_urls_are_refused_up_front():
    with pytest.raises(ConfigError, match="nonsense://x"):
        AppriseNotifier(["json://hooks.example/scout", "nonsense://x"])
