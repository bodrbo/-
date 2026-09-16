import datetime as dt
import sqlite3
import time
import unittest

import requests

from modules.notifications import EVENT_SCHEDULE_BAD_WEATHER
from modules.weather import client, repository, schema, services


def _unix(local_dt):
    """Unix timestamp that fromtimestamp() maps back to local_dt exactly,
    regardless of the test machine's own timezone."""
    return time.mktime(local_dt.timetuple())


def _hour_record(local_dt, temp=15.0, wind_speed=3.0, wind_gust=None,
                  wind_deg=180, rain_1h=None, weather_id=800, weather_main="Clear"):
    record = {
        "dt": _unix(local_dt),
        "temp": temp,
        "wind_speed": wind_speed,
        "wind_deg": wind_deg,
        "weather": [{"id": weather_id, "main": weather_main}],
    }
    if wind_gust is not None:
        record["wind_gust"] = wind_gust
    if rain_1h is not None:
        record["rain"] = {"1h": rain_1h}
    return record


class FakeResponse:
    def __init__(self, data, next_url=None, status_code=200):
        self.status_code = status_code
        self._body = {"data": data}
        if next_url:
            self._body["next"] = next_url

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self):
        return self._body


class WeatherClientTests(unittest.TestCase):
    def test_follows_next_pagination_link_once(self):
        calls = []

        def requester(url, params=None, timeout=None):
            calls.append((url, params))
            if len(calls) == 1:
                return FakeResponse([{"dt": 1}], next_url="https://api.example/next")
            return FakeResponse([{"dt": 2}])

        hours = client.fetch_hourly_forecast("key", 1.0, 2.0, requester=requester)
        self.assertEqual([h["dt"] for h in hours], [1, 2])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], "https://api.example/next")
        self.assertIsNone(calls[1][1])

    def test_stops_when_no_next_link(self):
        def requester(url, params=None, timeout=None):
            return FakeResponse([{"dt": 1}])

        hours = client.fetch_hourly_forecast("key", 1.0, 2.0, requester=requester)
        self.assertEqual(len(hours), 1)

    def test_raises_on_http_error(self):
        def requester(url, params=None, timeout=None):
            return FakeResponse([], status_code=500)

        with self.assertRaises(requests.HTTPError):
            client.fetch_hourly_forecast("key", 1.0, 2.0, requester=requester)

    def test_401_is_translated_into_an_actionable_message(self):
        def requester(url, params=None, timeout=None):
            return FakeResponse([], status_code=401)

        with self.assertRaises(RuntimeError) as ctx:
            client.fetch_hourly_forecast("key", 1.0, 2.0, requester=requester)
        self.assertIn("One Call by Call", str(ctx.exception))


class _WeatherFixture:
    CAPTAIN = "Капитан Тестов"
    GUIDE = "Гид Тестов"

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE schedule_items (
                id INTEGER PRIMARY KEY,
                service_name TEXT NOT NULL,
                boat TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE schedule_assignments (
                id INTEGER PRIMARY KEY,
                schedule_item_id INTEGER NOT NULL,
                employee_id INTEGER NOT NULL,
                employee_name TEXT NOT NULL,
                role TEXT NOT NULL
            );
            """
        )
        schema.init_schema(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _create_trip(self, starts_at, ends_at, captain=True, guide=False,
                      service_name="Малый тур", boat="Ларус"):
        cur = self.db.execute(
            "INSERT INTO schedule_items (service_name, boat, starts_at, ends_at) "
            "VALUES (?, ?, ?, ?)",
            (service_name, boat, starts_at, ends_at),
        )
        item_id = cur.lastrowid
        if captain:
            self.db.execute(
                "INSERT INTO schedule_assignments "
                "(schedule_item_id, employee_id, employee_name, role) VALUES (?, 1, ?, 'captain')",
                (item_id, self.CAPTAIN),
            )
        if guide:
            self.db.execute(
                "INSERT INTO schedule_assignments "
                "(schedule_item_id, employee_id, employee_name, role) VALUES (?, 2, ?, 'guide')",
                (item_id, self.GUIDE),
            )
        self.db.commit()
        return item_id


class EvaluateTripWeatherTests(_WeatherFixture, unittest.TestCase):
    def test_returns_none_outside_cached_horizon(self):
        verdict = services.evaluate_trip_weather(
            self.db, "2099-01-01 10:00", "2099-01-01 12:00"
        )
        self.assertIsNone(verdict)

    def test_calm_weather_is_not_bad(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        hour = trip_start.replace(minute=0, second=0, microsecond=0)
        repository.upsert_forecast_hours(
            self.db, [_hour_record(hour, wind_speed=4.0, weather_id=800)], "now"
        )
        starts = trip_start.strftime("%Y-%m-%d %H:%M")
        ends = (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        verdict = services.evaluate_trip_weather(self.db, starts, ends)
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict["is_bad"])
        self.assertEqual(verdict["reasons"], [])

    def test_flags_gust_above_threshold(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        hour = trip_start.replace(minute=0, second=0, microsecond=0)
        repository.upsert_forecast_hours(
            self.db, [_hour_record(hour, wind_speed=5.0, wind_gust=14.0)], "now"
        )
        starts = trip_start.strftime("%Y-%m-%d %H:%M")
        ends = (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        verdict = services.evaluate_trip_weather(self.db, starts, ends)
        self.assertTrue(verdict["is_bad"])
        self.assertIn("ветер", verdict["reasons"][0])

    def test_flags_any_measurable_precipitation(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        hour = trip_start.replace(minute=0, second=0, microsecond=0)
        repository.upsert_forecast_hours(
            self.db, [_hour_record(hour, wind_speed=3.0, rain_1h=0.3, weather_id=500)], "now"
        )
        starts = trip_start.strftime("%Y-%m-%d %H:%M")
        ends = (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        verdict = services.evaluate_trip_weather(self.db, starts, ends)
        self.assertTrue(verdict["is_bad"])
        self.assertIn("осадки", verdict["reasons"])

    def test_flags_thunderstorm_even_without_wind_or_rain(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        hour = trip_start.replace(minute=0, second=0, microsecond=0)
        repository.upsert_forecast_hours(
            self.db, [_hour_record(hour, wind_speed=2.0, weather_id=211, weather_main="Thunderstorm")], "now"
        )
        starts = trip_start.strftime("%Y-%m-%d %H:%M")
        ends = (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        verdict = services.evaluate_trip_weather(self.db, starts, ends)
        self.assertTrue(verdict["is_bad"])
        self.assertIn("гроза", verdict["reasons"])

    def test_checks_every_hour_the_trip_spans_not_just_departure(self):
        trip_start = dt.datetime.now().replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=2)
        calm_hour = trip_start
        bad_hour = trip_start + dt.timedelta(hours=1)
        repository.upsert_forecast_hours(
            self.db,
            [
                _hour_record(calm_hour, wind_speed=3.0, weather_id=800),
                _hour_record(bad_hour, wind_speed=5.0, wind_gust=16.0, weather_id=800),
            ],
            "now",
        )
        starts = trip_start.strftime("%Y-%m-%d %H:%M")
        ends = (trip_start + dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        verdict = services.evaluate_trip_weather(self.db, starts, ends)
        self.assertTrue(verdict["is_bad"])
        # Display still reflects the departure hour, not the later bad one.
        self.assertEqual(verdict["departure"]["wind_speed"], 3.0)


class AttachForecastTests(_WeatherFixture, unittest.TestCase):
    def test_attaches_weather_dict_when_cached_and_none_when_out_of_horizon(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        hour = trip_start.replace(minute=0, second=0, microsecond=0)
        repository.upsert_forecast_hours(
            self.db, [_hour_record(hour, wind_speed=3.0, wind_deg=90)], "now"
        )
        items = [
            {
                "starts_at": trip_start.strftime("%Y-%m-%d %H:%M"),
                "ends_at": (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
            },
            {
                "starts_at": "2099-01-01 10:00",
                "ends_at": "2099-01-01 12:00",
            },
        ]
        services.attach_forecast(self.db, items)
        self.assertIsNotNone(items[0]["weather"])
        self.assertEqual(items[0]["weather"]["wind_dir"], "В")
        self.assertIsNone(items[1]["weather"])


class SendWeatherAlertsTests(_WeatherFixture, unittest.TestCase):
    def _bad_weather_now(self, trip_start):
        hour = trip_start.replace(minute=0, second=0, microsecond=0)
        repository.upsert_forecast_hours(
            self.db, [_hour_record(hour, wind_speed=5.0, wind_gust=15.0)], "now"
        )

    def test_notifies_captain_and_guide_captain_but_not_plain_guide(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        self._bad_weather_now(trip_start)
        item_id = self._create_trip(
            trip_start.strftime("%Y-%m-%d %H:%M"),
            (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
            captain=True, guide=True,
        )
        calls = []
        stats = services.send_weather_alerts(
            self.db, lambda db, name, text: calls.append((name, text)) or "sent"
        )
        self.assertEqual(stats["alerts_sent"], 1)
        notified_names = [name for name, _ in calls]
        self.assertIn(self.CAPTAIN, notified_names)
        self.assertNotIn(self.GUIDE, notified_names)
        self.assertIn("Неблагоприятный прогноз", calls[0][1])

    def test_does_not_resend_once_delivered(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        self._bad_weather_now(trip_start)
        self._create_trip(
            trip_start.strftime("%Y-%m-%d %H:%M"),
            (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
        )
        calls = []
        sender = lambda db, name, text: calls.append(name) or "sent"
        services.send_weather_alerts(self.db, sender)
        services.send_weather_alerts(self.db, sender)
        self.assertEqual(len(calls), 1)

    def test_skips_trip_with_no_captain_assigned(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        self._bad_weather_now(trip_start)
        self._create_trip(
            trip_start.strftime("%Y-%m-%d %H:%M"),
            (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
            captain=False, guide=True,
        )
        calls = []
        stats = services.send_weather_alerts(
            self.db, lambda db, name, text: calls.append(name) or "sent"
        )
        self.assertEqual(stats["alerts_sent"], 0)
        self.assertEqual(calls, [])

    def test_skips_trip_with_good_weather(self):
        trip_start = dt.datetime.now() + dt.timedelta(hours=2)
        hour = trip_start.replace(minute=0, second=0, microsecond=0)
        repository.upsert_forecast_hours(
            self.db, [_hour_record(hour, wind_speed=3.0, weather_id=800)], "now"
        )
        self._create_trip(
            trip_start.strftime("%Y-%m-%d %H:%M"),
            (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
        )
        stats = services.send_weather_alerts(
            self.db, lambda db, name, text: "sent"
        )
        self.assertEqual(stats["alerts_sent"], 0)

    def test_skips_trip_outside_alert_lookahead(self):
        trip_start = dt.datetime.now() + dt.timedelta(days=10)
        self._create_trip(
            trip_start.strftime("%Y-%m-%d %H:%M"),
            (trip_start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
        )
        stats = services.send_weather_alerts(
            self.db, lambda db, name, text: "sent"
        )
        self.assertEqual(stats["alerts_sent"], 0)


if __name__ == "__main__":
    unittest.main()
