"""Переходы Red Bin: стадии один раз и одно сообщение на перескок."""

from __future__ import annotations

import config
from bot.keyboards import parse_red_bin_callback
from services.red_bin import (
    advance_rank,
    apply_red_bin,
    browse_tree,
    format_red_bin_report_html,
    red_bin_report_rows,
    zone_rank,
)
from services.zero_sold import make_position_key
from storage.data_store import list_red_bin, upsert_red_bin


def _ball(qty: int) -> dict:
    return {
        "top_category": "Мячи",
        "brand": "Mikasa",
        "name": "Волейбольный мяч Mikasa MV300",
        "quantity": qty,
    }


def test_browse_tree_and_callbacks() -> None:
    """Бренд indent 10 раскрывается в модели indent 12, сам в лимит не попадает."""
    tree = browse_tree(
        [
            {
                "top_category": "Мячи",
                "brand": "Волейбол",
                "name": "Jogel",
                "quantity": 10,
                "indent": 10,
                "line": "Jogel",
            },
            {
                "top_category": "Мячи",
                "brand": "Волейбол",
                "name": "Мяч волейбольный Jogel JV-100",
                "quantity": 5,
                "indent": 12,
                "line": "Jogel",
            },
            {
                "top_category": "Мячи",
                "brand": "Волейбол",
                "name": "Мяч волейбольный Mikasa V200W",
                "quantity": 20,
                "indent": 12,
                "line": "Mikasa",
            },
            {
                "top_category": "Аксессуары",
                "brand": "Nike",
                "name": "Гетры",
                "quantity": 2,
                "indent": 10,
                "line": "Гетры",
            },
        ]
    )
    assert list(tree["Мячи"]["Волейбол"]) == ["Jogel", "Mikasa"]
    jogel = tree["Мячи"]["Волейбол"]["Jogel"]
    assert [row["name"] for row in jogel] == ["Мяч волейбольный Jogel JV-100"]
    assert tree["Аксессуары"]["Nike"]["Гетры"][0]["name"] == "Гетры"

    assert parse_red_bin_callback("rb:g:2") == ("group", 2)
    assert parse_red_bin_callback("rb:b:0") == ("brand", 0)
    assert parse_red_bin_callback("rb:l:3") == ("line", 3)
    assert parse_red_bin_callback("rb:lp:1") == ("page", "l", 1)
    assert parse_red_bin_callback("rb:gp:1") == ("page", "g", 1)
    assert parse_red_bin_callback("rb:pp:3") == ("page", "p", 3)
    assert parse_red_bin_callback("rb:up") == ("up",)
    assert parse_red_bin_callback("rb:4") == ("pick", 4)
    assert parse_red_bin_callback("rb:4:5") == ("limit", 4, 5)


def test_red_bin_report_lists_every_limit() -> None:
    """Выгрузка не режет список: все записи, остаток из снимка, длинный текст пачками."""
    rows = red_bin_report_rows(
        {
            "2": {"key": "b", "group": "Мячи", "brand": "Волейбол", "name": "Б", "limit": 5},
            "1": {"key": "a", "group": "Мячи", "brand": "Волейбол", "name": "А", "limit": 3},
        },
        {"a": {"quantity": 15}},
    )
    assert [row["name"] for row in rows] == ["А", "Б"]
    assert rows[0]["qty"] == 15
    assert rows[1]["qty"] is None
    text = format_red_bin_report_html(rows)
    assert len(text) == 1
    assert "лимит <code>3</code>" in text[0]
    assert "лимит <code>5</code>" in text[0]
    huge = red_bin_report_rows(
        {str(i): {"key": str(i), "group": "Г", "brand": "", "name": "Т" * 200, "limit": 1} for i in range(30)},
        {},
    )
    parts = format_red_bin_report_html(huge)
    assert len(parts) > 1
    assert sum(part.count("лимит") for part in parts) == 30


def test_red_bin_rank_sequence() -> None:
    """15→5→4→1→0, перескок 15→0, сброс выше лимита, тихая база на 5."""
    rank, stages = advance_rank(0, zone_rank(5, 5))
    assert (rank, stages) == (1, ("at",))

    rank, stages = advance_rank(rank, zone_rank(4, 5))
    assert (rank, stages) == (2, ("eating",))

    rank, stages = advance_rank(rank, zone_rank(1, 5))
    assert (rank, stages) == (2, ())

    rank, stages = advance_rank(rank, zone_rank(0, 5))
    assert (rank, stages) == (3, ("gone",))

    rank, stages = advance_rank(0, zone_rank(0, 5))
    assert (rank, stages) == (3, ("at", "eating", "gone"))

    rank, stages = advance_rank(rank, zone_rank(16, 5))
    assert (rank, stages) == (0, ())

    assert zone_rank(5, 5) == 1
    assert zone_rank(None, 5) == 3


def test_apply_red_bin_one_message_and_silent_baseline(monkeypatch, tmp_path) -> None:
    """Перескок 15→0 — одно HTML. Установка при уже 5 ничего не шлёт."""
    monkeypatch.setattr(config, "RED_BIN_JSON_PATH", str(tmp_path / "red_bin.json"))
    sent: list[str] = []
    monkeypatch.setattr(
        "services.red_bin.send_group_limit_alert_html",
        lambda html: sent.append(html),
    )

    ball = _ball(15)
    upsert_red_bin(
        {
            "key": make_position_key(ball),
            "group": "Мячи",
            "brand": "Mikasa",
            "name": ball["name"],
            "last_qty": 15,
        },
        5,
    )
    assert list_red_bin()["1"]["rank"] == 0

    apply_red_bin([_ball(0)])
    assert len(sent) == 1
    assert "дошли до лимита" in sent[0]
    assert "начали продавать" in sent[0]
    assert "продали всё" in sent[0]
    assert list_red_bin()["1"]["rank"] == 3

    apply_red_bin([_ball(0)])
    assert len(sent) == 1

    apply_red_bin([_ball(16)])
    assert list_red_bin()["1"]["rank"] == 0
    assert len(sent) == 1

    apply_red_bin([_ball(5)])
    assert len(sent) == 2
    assert "дошли до лимита" in sent[1]
    assert "начали продавать" not in sent[1]

    quiet = {
        "top_category": "Мячи",
        "brand": "Mikasa",
        "name": "Уже на лимите",
        "quantity": 5,
    }
    upsert_red_bin(
        {
            "key": make_position_key(quiet),
            "group": "Мячи",
            "brand": "Mikasa",
            "name": quiet["name"],
            "last_qty": 5,
        },
        5,
    )
    assert list_red_bin()["2"]["rank"] == 1
    sent.clear()
    apply_red_bin([_ball(5), quiet])
    assert sent == []
