"""Relevés quotidiens d'une station (DPClim) et Météo des forêts (DPMeteoForets).

Les fichiers de `tests/fixtures/` sont de vrais fichiers relevés le 18/11/2026 (CSV de la station 59343001, 10 au 17/11, commande puis fichier) et le 22/05/2024
(l'échantillon `tableau-meteo-foret.csv` de Météo-France sur data.gouv.fr, Licence Ouverte 2.0). Le portail n'est jamais appelé : un `MockTransport` rejoue ses réponses.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache import CacheStore
from app.collectors.dpclim import BusyError, StationDailyCollector, UpstreamError, last_available_day, parse_daily_csv
from app.collectors.forests import ForestsCollector, parse_forests_csv
from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
CLIM_CSV = (FIXTURES / "dpclim_quotidienne_59343001.csv").read_text(encoding="utf-8")
FOREST_CSV = (FIXTURES / "mdf_tableau_meteo_foret_2024-05-22.csv").read_text(encoding="utf-8")
STATION = "59343001"
KEY = "k" * 40


def settings_for(tmp_path: Path, **extra: object) -> Settings:
    options: dict[str, object] = {"METEO_DPCLIM_COMMANDS_PER_MINUTE": 20, **extra}
    return Settings(METEOFRANCE_API_KEY=KEY, METEO_API_KEY=None, METEO_DB_PATH=str(tmp_path / "cache.db"), **options)  # type: ignore[call-arg]


# ------------------------------------------------------------------------------------------------ DPClim : le CSV


def test_parse_the_real_daily_file_of_lille_lesquin() -> None:
    days = parse_daily_csv(CLIM_CSV)
    assert [d.date for d in days] == [date(2026, 11, n) for n in range(10, 18)]
    tenth, last = days[0], days[-1]
    assert (tenth.rain_mm, tenth.tmin_c, tenth.tmax_c, tenth.tmean_c, tenth.wind_mean_ms, tenth.gust_ms) == (
        7.7,
        0.8,
        8.8,
        5.4,
        3.7,
        7.9,
    )
    assert (last.rain_mm, last.tmin_c, last.tmax_c, last.tmean_c, last.wind_mean_ms) == (22.3, 5.3, 15.5, 11.3, 8.3)


def test_a_doubtful_value_is_withheld_and_named() -> None:
    last = parse_daily_csv(CLIM_CSV)[-1]
    assert last.gust_ms is None  # 26,1 m/s au code qualité 2 : « douteuse en cours de vérification »
    assert last.doubtful == ["gust_ms"]
    assert parse_daily_csv(CLIM_CSV)[0].doubtful == []


def test_parse_ignores_a_header_only_file_and_a_row_with_a_bad_date() -> None:
    assert parse_daily_csv("POSTE;DATE;RR;QRR\n") == []
    assert parse_daily_csv("POSTE;DATE;RR;QRR\n1;n/a;1,0;1\n") == []
    one = parse_daily_csv("POSTE;DATE;RR;QRR;TN;QTN\n1;20261117;;;3,5;9\n")
    assert one[0].rain_mm is None and one[0].tmin_c == 3.5


def test_the_last_available_day_waits_for_half_past_eleven_utc() -> None:
    assert last_available_day(datetime(2026, 11, 18, 11, 29, tzinfo=UTC)) == date(2026, 11, 16)
    assert last_available_day(datetime(2026, 11, 18, 11, 30, tzinfo=UTC)) == date(2026, 11, 17)
    assert last_available_day(datetime(2026, 11, 18, 23, 0, tzinfo=UTC)) == date(2026, 11, 17)


# ------------------------------------------------------------------------------------------------ DPClim : le collecteur


class Portal:
    """Rejoue le portail : la commande (202), un 204 puis le fichier (201), ou une erreur choisie."""

    def __init__(self, order: int = 202, file_codes: tuple[int, ...] = (204, 201)) -> None:
        self.order = order
        self.file_codes = list(file_codes)
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == KEY
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/commande-station/quotidienne"):
            assert len(request.url.params["id-station"]) == 8
            assert request.url.params["date-deb-periode"] <= request.url.params["date-fin-periode"]
            if self.order != 202:
                return httpx.Response(self.order, text="Bad Request")
            return httpx.Response(202, json={"elaboreProduitAvecDemandeResponse": {"return": "2026046654166"}})
        if path.endswith("/commande/fichier"):
            code = self.file_codes.pop(0) if len(self.file_codes) > 1 else self.file_codes[0]
            return httpx.Response(code, text=CLIM_CSV if code == 201 else "")
        raise AssertionError(path)


def collector(
    tmp_path: Path, portal: Portal, monkeypatch: pytest.MonkeyPatch, **extra: object
) -> tuple[StationDailyCollector, CacheStore]:
    monkeypatch.setattr("app.collectors.dpclim.POLL_EVERY_S", 0.0)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "app.collectors.dpclim.asyncio.sleep", no_sleep
    )  # l'étalement des commandes est calculé, pas attendu
    cache = CacheStore(str(tmp_path / "cache.db"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(portal))
    return StationDailyCollector(client, cache, settings_for(tmp_path, **extra)), cache


NOW = datetime(2026, 11, 18, 12, 30, tzinfo=UTC)


def test_the_collector_orders_waits_for_the_file_and_asks_the_portal_once_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = Portal()
    dp, cache = collector(tmp_path, portal, monkeypatch)

    async def scenario() -> None:
        rows, last, fetched = await dp.daily(STATION, 3, NOW)
        assert [r.date for r in rows] == [date(2026, 11, 15), date(2026, 11, 16), date(2026, 11, 17)]
        assert last == date(2026, 11, 17) and fetched is not None
        assert portal.calls == [
            "/public/DPClim/v1/commande-station/quotidienne",
            "/public/DPClim/v1/commande/fichier",
            "/public/DPClim/v1/commande/fichier",
        ]
        again, _, _ = await dp.daily(STATION, 7, NOW)  # même jour : le cache répond, quel que soit `days`
        assert len(again) == 7 and len(portal.calls) == 3
        later = datetime(2026, 11, 18, 18, 0, tzinfo=UTC)
        await dp.daily(STATION, 1, later)
        assert len(portal.calls) == 3
        next_day = datetime(
            2026, 11, 19, 12, 0, tzinfo=UTC
        )  # le dernier jour disponible avance : une nouvelle commande
        await dp.daily(STATION, 1, next_day)
        assert len(portal.calls) == 5  # la commande (le portail a déjà le fichier prêt : un seul 201)

    asyncio.run(scenario())
    assert cache.get_cache(f"dpclim:{STATION}") is not None


def test_concurrent_requests_for_one_station_make_one_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    portal = Portal()
    dp, _ = collector(tmp_path, portal, monkeypatch)

    async def scenario() -> None:
        await asyncio.gather(*(dp.daily(STATION, 2, NOW) for _ in range(5)))

    asyncio.run(scenario())
    assert sum(1 for c in portal.calls if c.endswith("commande-station/quotidienne")) == 1


def test_an_unknown_station_or_one_without_data_is_kept_empty_for_the_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = Portal(order=404)
    dp, _ = collector(tmp_path, unknown, monkeypatch)
    silent = Portal(file_codes=(500,))
    (tmp_path / "b").mkdir()
    dp2, _ = collector(tmp_path / "b", silent, monkeypatch)

    async def scenario() -> None:
        rows, _, _ = await dp.daily(STATION, 3, NOW)
        assert rows == []
        await dp.daily(STATION, 3, NOW)
        assert len(unknown.calls) == 1  # pas de nouvelle commande avant demain
        rows, _, _ = await dp2.daily(STATION, 3, NOW)
        assert rows == []

    asyncio.run(scenario())


def test_a_failing_portal_serves_the_old_entry_or_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    portal = Portal()
    dp, cache = collector(tmp_path, portal, monkeypatch)

    async def scenario() -> None:
        await dp.daily(STATION, 3, datetime(2026, 11, 17, 12, 30, tzinfo=UTC))  # entrée d'hier
        portal.order = 503
        rows, last, _ = await dp.daily(STATION, 2, NOW)  # le portail tombe : l'entrée d'hier est servie
        assert len(rows) == 2 and last == date(2026, 11, 17)
        assert dp.last_error is not None and KEY not in dp.last_error
        with pytest.raises(UpstreamError):
            await dp.daily("12345678", 2, NOW)  # aucune entrée à servir

    asyncio.run(scenario())


def test_too_many_orders_in_a_row_are_refused_without_calling_the_portal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = Portal()
    dp, _ = collector(
        tmp_path, portal, monkeypatch, METEO_DPCLIM_COMMANDS_PER_MINUTE=1, METEO_DPCLIM_MAX_WAIT_SECONDS=5
    )

    async def scenario() -> None:
        await dp.daily("11111111", 1, NOW)  # le premier passe tout de suite
        with pytest.raises(BusyError):
            await dp.daily("22222222", 1, NOW)  # le suivant attendrait 60 s

    asyncio.run(scenario())
    assert sum(1 for c in portal.calls if c.endswith("commande-station/quotidienne")) == 1


# ------------------------------------------------------------------------------------------------ Météo des forêts


def test_parse_the_real_sample_of_the_forest_weather() -> None:
    stamp, deps = parse_forests_csv(FOREST_CSV)
    assert stamp == datetime(2024, 5, 22, 14, 50, 5, tzinfo=UTC)
    assert len(deps) == 96
    aude = next(d for d in deps if d.department == "11")
    assert (aude.name, aude.level_d1, aude.level_d2) == (
        "Aude",
        2,
        1,
    )  # le cas d'usage de la documentation de l'API : « 2 (MODÉRÉ) »
    assert {d.level_d1 for d in deps} <= {1, 2, 3, 4}
    assert any(d.department == "2A" for d in deps) and any(d.name == "Ardèche" for d in deps)


def test_parse_accepts_the_comma_separated_yearly_archive_and_rejects_nonsense() -> None:
    stamp, deps = parse_forests_csv(
        "date,num_dep,niveau_j1,niveau_j2,dep_nom\n2025-06-03T14:50:06Z,1,1,3,Ain\n2025-06-03T14:50:06Z,2,4,9,Aisne\n"
    )
    assert stamp == datetime(2025, 6, 3, 14, 50, 6, tzinfo=UTC)
    assert [(d.department, d.level_d1, d.level_d2) for d in deps] == [
        ("01", 1, 3),
        ("02", 4, None),
    ]  # 9 n'est pas un niveau
    assert parse_forests_csv("") == (None, [])
    assert parse_forests_csv("<html>erreur</html>")[1] == []


def forest_app(tmp_path: Path, handler: object) -> tuple[object, TestClient]:
    cfg = Settings(METEOFRANCE_API_KEY=KEY, METEO_API_KEY=None, METEO_DB_PATH=str(tmp_path / "cache.db"))  # type: ignore[call-arg]
    app = create_app(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))  # type: ignore[arg-type]
    return app, TestClient(app)


def test_out_of_season_is_a_quiet_empty_answer_and_not_a_health_error(tmp_path: Path) -> None:
    def portal(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/public/DPMeteoForets/v1/carte/encours" and request.headers["apikey"] == KEY
        return httpx.Response(404, json={"detail": "no matching blob"})

    app, client = forest_app(tmp_path, portal)
    with client:
        assert client.get("/v1/forests").status_code == 503  # pas encore lu : l'API ne prétend rien
        asyncio.run(app.state.forests.check())
        body = client.get("/v1/forests").json()
        assert body["status"] == "out_of_season" and body["departments"] == [] and body["reference_time"] is None
        assert body["attribution"].startswith("Source : Météo-France")
        health = client.get("/v1/health").json()["sources"]["meteofrance-forests"]
        assert health["status"] == "fresh" and health["last_error"] is None


def test_in_season_the_levels_are_served_per_department(tmp_path: Path) -> None:
    app, client = forest_app(
        tmp_path, lambda r: httpx.Response(200, text=FOREST_CSV, headers={"content-type": "text/csv"})
    )
    with client:
        asyncio.run(app.state.forests.check())
        everything = client.get("/v1/forests").json()
        assert everything["status"] == "available" and len(everything["departments"]) == 96
        assert everything["reference_time"].startswith("2024-05-22T14:50:05")
        aude = client.get("/v1/forests", params={"department": "11"}).json()
        assert aude["departments"] == [{"department": "11", "name": "Aude", "level_d1": 2, "level_d2": 1}]
        assert client.get("/v1/forests", params={"department": "99"}).json()["departments"] == []
        assert client.get("/v1/forests", params={"department": "abc"}).status_code == 422


def test_an_unreadable_file_is_an_error_and_never_an_empty_map(tmp_path: Path) -> None:
    app, client = forest_app(tmp_path, lambda r: httpx.Response(200, text="<html>nouveau format</html>"))
    with client:
        asyncio.run(app.state.forests.check())
        assert client.get("/v1/forests").status_code == 503
        health = client.get("/v1/health").json()["sources"]["meteofrance-forests"]
        assert health["status"] == "unavailable" and "aucun département" in health["last_error"]


def test_collector_without_a_key_is_not_configured(tmp_path: Path) -> None:
    cfg = Settings(METEOFRANCE_API_KEY=None, METEO_API_KEY=None, METEO_DB_PATH=str(tmp_path / "cache.db"))  # type: ignore[call-arg]
    forests = ForestsCollector(httpx.AsyncClient(), CacheStore(cfg.db_path), cfg)
    assert not forests.enabled() and forests.health()[0] == "not_configured"


# ------------------------------------------------------------------------------------------------ les routes


def test_station_daily_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.collectors.dpclim.POLL_EVERY_S", 0.0)
    portal = Portal()
    cfg = settings_for(tmp_path)
    app = create_app(cfg, httpx.AsyncClient(transport=httpx.MockTransport(portal)))
    with TestClient(app) as client:
        assert client.get("/v1/station-daily", params={"station": "abc"}).status_code == 422
        assert client.get("/v1/station-daily", params={"station": STATION, "days": 30}).status_code == 422
        reply = client.get("/v1/station-daily", params={"station": STATION, "days": 2})
        assert reply.status_code == 200
        body = reply.json()
        assert (
            body["station"] == STATION
            and body["source_model"] == "meteofrance-dpclim"
            and "Licence Ouverte 2.0" in body["attribution"]
        )
        assert (
            len(body["days"]) == 2
            and body["days"][-1]["date"] == "2026-11-17"
            and body["last_day"] >= body["days"][-1]["date"]
        )
        assert reply.headers["cache-control"] == "public, max-age=3600"
        assert client.get("/v1/health").json()["sources"]["meteofrance-dpclim"]["status"] == "fresh"
        portal.order = 503
        assert client.get("/v1/station-daily", params={"station": "99999999"}).status_code == 503
