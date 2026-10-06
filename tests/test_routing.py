from pathlib import Path

from scout.llm import make_backend, model_server
from scout.llm.base import CompletionRequest, Message
from scout.llm.exchange import ExchangeBackend
from scout.llm.openai_compat import OpenAICompatBackend
from scout.llm.routing import RoutedBackend
from scout.settings import Settings
from tests.helpers import ScriptedBackend


def request(purpose: str) -> CompletionRequest:
    return CompletionRequest(messages=(Message("user", "hi"),), purpose=purpose)


class Closing(ScriptedBackend):
    closed = 0

    def close(self) -> None:
        self.closed += 1


def test_each_purpose_goes_to_its_model():
    reader, planner = Closing(["read"]), Closing(["plan"])
    reader.model, planner.model = "big", "small"
    routed = RoutedBackend(reader, {"plan": planner})
    assert routed.complete(request("plan")).text == "plan"
    assert routed.complete(request("extract")).text == "read"
    assert routed.model == "big"  # results name the model that read the pages
    routed.close()
    assert (reader.closed, planner.closed) == (1, 1)


def test_a_planner_model_routes_planning(tmp_path: Path):
    settings = Settings(llm="openai", llm_model="qwen3-32b", planner_model="qwen3-4b")
    routed = make_backend(settings)
    assert isinstance(routed, RoutedBackend)
    assert model_server(routed).model == "qwen3-32b"
    same = make_backend(Settings(llm="openai", llm_model="m", planner_model="m"))
    assert isinstance(same, OpenAICompatBackend)

    recorded = make_backend(Settings(llm=f"record:{tmp_path}", llm_model="m", planner_model="s"))
    assert isinstance(recorded, ExchangeBackend)
    assert model_server(recorded).model == "m"
    assert model_server(make_backend(Settings(llm=f"exchange:{tmp_path}"))) is None
