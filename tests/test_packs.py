import pytest
from click.testing import CliRunner

from scout.cli import main
from scout.errors import ConfigError
from scout.monitor.packs import available
from scout.monitor.watches import WatchBook
from scout.settings import load_settings

SAMPLES = {
    "product": "RTX 5090",
    "below": "1800 USD",
    "project": "llama.cpp",
    "topic": "open-source LLMs",
    "keyword": "Llama",
}


def test_every_shipped_pack_makes_valid_watches():
    packs = available()
    assert {"price", "release", "news", "restock"} <= set(packs)
    for pack in packs.values():
        assert set(pack.params) <= set(SAMPLES), f"add samples for pack {pack.name}"
        watches = pack.expand({param: SAMPLES[param] for param in pack.params})
        assert watches, pack.name  # each one checked like any watch: schedule, alert rules


def test_left_out_optional_parameters_drop_what_needs_them():
    price = available()["price"]
    (with_limit,) = price.expand({"product": "RTX 5090", "below": "1800 USD"})
    assert with_limit.name == "rtx-5090-price"
    assert with_limit.goal == "cheapest RTX 5090 price right now"
    assert with_limit.alerts == ("drop 5%", "price below 1800 USD")
    (without,) = price.expand({"product": "RTX 5090"})
    assert without.alerts == ("drop 5%",)


def test_pack_parameters_are_checked():
    price = available()["price"]
    with pytest.raises(ConfigError, match="needs product="):
        price.expand({"below": "1800"})
    with pytest.raises(ConfigError, match="no parameter colour"):
        price.expand({"product": "RTX 5090", "colour": "red"})


def test_the_users_packs_add_to_and_replace_the_shipped_ones(tmp_path):
    (tmp_path / "price.yaml").write_text(
        "description: mine\nparams: {item: what}\n"
        "watches: [{name: '{slug}-deal', goal: 'deals on {item}', every: 1d}]\n",
        encoding="utf-8",
    )
    (tmp_path / "broken.yaml").write_text("watches: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        available(tmp_path)
    (tmp_path / "broken.yaml").unlink()
    packs = available(tmp_path)
    assert packs["price"].description == "mine"
    (watch,) = packs["price"].expand({"item": "Steam Deck"})
    assert watch.name == "steam-deck-deal"


def test_scout_pack_add(workspace):
    runner = CliRunner()
    added = runner.invoke(main, ["pack", "add", "price", "product=RTX 5090", "below=1800 USD"])
    assert added.exit_code == 0, added.output
    assert "Watching rtx-5090-price every 6h" in added.output
    (watch,) = WatchBook(load_settings().watches_path).load()
    assert watch.alerts == ("drop 5%", "price below 1800 USD")

    again = runner.invoke(main, ["pack", "add", "price", "product=RTX 5090"])
    assert "already a watch named rtx-5090-price" in again.output
    assert runner.invoke(main, ["pack", "add", "price", "RTX 5090"]).exit_code == 2
    assert "no pack 'gpu'" in runner.invoke(main, ["pack", "add", "gpu"]).output
    assert "restock" in runner.invoke(main, ["pack", "list"]).output
